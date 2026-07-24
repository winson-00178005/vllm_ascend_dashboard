"""
测试覆盖率同步服务

两类覆盖率数据源：
1. E2E 特性覆盖 — 从本地 git clone 读取 tests/e2e/coverage.html，解析内嵌 DATA/ALLOWED
2. PR 流水线覆盖 — 从华为云 OBS 下载 coverage.tar（原始 coverage.py covdata），
   方案1 直接读 SQLite 汇总覆盖广度；方案2 用 coverage 包 combine 生成行覆盖率%

数据存入 ProjectDashboardConfig（JSON），不新建表。
"""
import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import ProjectDashboardConfig
from app.services.github_cache import get_github_cache

logger = logging.getLogger(__name__)

# config_key 常量
E2E_KEY = "e2e_feature_coverage"
PR_BREADTH_KEY = "pr_pipeline_coverage_breadth"
PR_LINES_KEY = "pr_pipeline_coverage_lines"
SYNC_STATUS_KEY = "coverage_sync_status"

# 进程内同步锁，避免手动+定时并发竞态
_coverage_sync_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _coverage_sync_lock
    if _coverage_sync_lock is None:
        _coverage_sync_lock = asyncio.Lock()
    return _coverage_sync_lock


def is_coverage_syncing() -> bool:
    """是否有覆盖率同步正在进行（供 API 同步检查返回 409）。"""
    lock = _coverage_sync_lock
    return lock is not None and lock.locked()

# 保留后台 Task 引用防止被 GC 回收（Python 官方警告）
_bg_tasks: set[asyncio.Task] = set()

# chompjs 可选（C 扩展在部分平台 DLL 加载失败，回退到字符串感知平衡括号计数）
try:
    import chompjs as _chompjs
    _CHOMPJS_OK = True
except Exception:  # pragma: no cover - 环境相关
    _chompjs = None
    _CHOMPJS_OK = False

# 维度固定标签兜底（已确认 coverage.html 中存在 DIM_LABELS_JS）
DIM_LABELS_FALLBACK = {
    "arch": "Architecture", "feature": "Feature", "parallel": "Parallel",
    "deploy": "Deploy", "hardware": "Hardware",
    "quantization": "Quantization", "graph_mode": "Graph Mode",
}

GITHUB_ACTIONS_PREFIX = "/__w/vllm-ascend/vllm-ascend/"


# ---------------------------------------------------------------------------
# JS 字面量提取
# ---------------------------------------------------------------------------
def _find_assignment_end(content: str, start: int) -> int:
    """从字面量起始字符位置开始，用字符串感知的平衡括号计数找到结束位置。

    跳过引号内的 ]/}，处理转义。start 指向字面量第一个字符（[ 或 {）。
    返回结束字符的索引（含）。
    """
    open_ch = content[start]
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    i = start
    in_str = False
    str_ch = ""
    n = len(content)
    while i < n:
        c = content[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == str_ch:
                in_str = False
            i += 1
            continue
        if c in ("'", '"', "`"):
            in_str = True
            str_ch = c
            i += 1
            continue
        if c == "/" and i + 1 < n and content[i + 1] == "/":
            # 行注释
            nl = content.find("\n", i)
            if nl == -1:
                return -1
            i = nl + 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def extract_js_literal(content: str, var_name: str, fallback: Any = None) -> Any:
    """从 JS 源码中提取赋给 var_name 的数组/对象字面量，转为 Python 对象。

    主解析器 chompjs（容错强）；不可用或失败时回退到字符串感知平衡括号计数 + json.loads。
    """
    # 匹配 `var NAME =` / `let NAME =` / `const NAME =`
    pat = re.compile(r"(?:const|let|var)\s+" + re.escape(var_name) + r"\s*=\s*([\[{])")
    m = pat.search(content)
    if not m:
        logger.warning("JS variable %s not found in content", var_name)
        return fallback
    literal_start = m.start(1)
    end = _find_assignment_end(content, literal_start)
    if end == -1:
        logger.warning("Failed to find end of %s literal", var_name)
        return fallback
    literal = content[literal_start:end + 1]

    # 主：chompjs
    if _CHOMPJS_OK:
        try:
            return _chompjs.parse_js_object(literal)
        except Exception as e:  # noqa: BLE001
            logger.debug("chompjs failed for %s: %s, falling back to bracket+json", var_name, e)
    # 备：json.loads（JS 字面量与 JSON 高度兼容；此文件无尾逗号/函数）
    try:
        return json.loads(literal)
    except json.JSONDecodeError:
        # 尝试去除尾逗号再解析
        try:
            cleaned = re.sub(r",\s*([}\]])", r"\1", literal)
            return json.loads(cleaned)
        except json.JSONDecodeError as e2:
            logger.warning("Failed to parse %s literal: %s", var_name, e2)
            return fallback


# ---------------------------------------------------------------------------
# 存储辅助
# ---------------------------------------------------------------------------
async def _load_config(db: AsyncSession, key: str) -> dict | None:
    cfg = (
        await db.execute(select(ProjectDashboardConfig).where(ProjectDashboardConfig.config_key == key))
    ).scalar_one_or_none()
    if cfg and cfg.config_value:
        return cfg.config_value
    return None


async def _save_config(db: AsyncSession, key: str, value: dict, description: str) -> None:
    cfg = (
        await db.execute(select(ProjectDashboardConfig).where(ProjectDashboardConfig.config_key == key))
    ).scalar_one_or_none()
    if cfg:
        cfg.config_value = value
        cfg.description = description
    else:
        db.add(ProjectDashboardConfig(config_key=key, config_value=value, description=description))


# ---------------------------------------------------------------------------
# E2E 特性覆盖解析器
# ---------------------------------------------------------------------------
class E2ECoverageParser:
    """从本地 git clone 读取 tests/e2e/coverage.html，解析 DATA 与 ALLOWED。"""

    HTML_REL_PATH = "tests/e2e/coverage.html"

    def parse(self) -> dict:
        cache = get_github_cache()
        # 不在此 pull —— 依赖已有 project_dashboard_cache_update hourly job（避免并发竞态）
        html_path = cache.cache_dir / self.HTML_REL_PATH
        if not html_path.exists():
            raise FileNotFoundError(f"coverage.html not found in local clone: {html_path}")
        content = html_path.read_text(encoding="utf-8")

        data = extract_js_literal(content, "DATA", fallback=[])
        allowed = extract_js_literal(content, "ALLOWED", fallback={})
        dim_labels = extract_js_literal(content, "DIM_LABELS_JS", fallback=DIM_LABELS_FALLBACK)
        if not dim_labels:
            dim_labels = DIM_LABELS_FALLBACK

        if not isinstance(data, list) or not data:
            raise ValueError("Failed to extract DATA from coverage.html (empty/invalid)")

        tests = [self._decorate(t) for t in data]
        summary = self._build_summary(tests)
        repo_commit = self._safe_latest_commit(cache)

        return {
            "summary": summary,
            "taxonomy": allowed if isinstance(allowed, dict) else {},
            "dim_labels": dim_labels if isinstance(dim_labels, dict) else DIM_LABELS_FALLBACK,
            "tests": tests,
            "source_file_hash": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "repo_commit": repo_commit,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _decorate(t: dict) -> dict:
        cov = t.get("coverage") or {}
        is_marked = any(isinstance(v, list) and len(v) > 0 for v in cov.values())
        return {
            "filepath": t.get("filepath", ""),
            "test_name": t.get("test_name", ""),
            "card_count": t.get("card_count", 1),
            "models": t.get("models") or [],
            "coverage": cov,
            "is_marked": is_marked,
        }

    @staticmethod
    def _build_summary(tests: list[dict]) -> dict:
        total = len(tests)
        marked = sum(1 for t in tests if t["is_marked"])
        by_card: dict[str, int] = defaultdict(int)
        for t in tests:
            by_card[str(t["card_count"])] += 1
        return {
            "total_tests": total,
            "marked_tests": marked,
            "marked_ratio": round(marked / total, 4) if total else 0.0,
            "by_card": dict(by_card),
        }

    @staticmethod
    def _safe_latest_commit(cache: Any) -> str | None:
        try:
            info = cache.get_latest_commit()
            return info.get("sha") if isinstance(info, dict) else info
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# PR 覆盖：路径/硬件解析
# ---------------------------------------------------------------------------
def decode_job_dir(job_dir: str) -> dict:
    """tests__e2e__pull_request__one_card___310p__test_x → 解码测试路径/类型/函数。

    `__`→`/`，`___`→`/_`。
    """
    # 先用占位符保护 `___`，再替换 `__`
    sentinel = "\x00"
    s = job_dir.replace("___", sentinel).replace("__", "/").replace(sentinel, "/_")
    test_func = None
    if "--" in s:
        base, test_func = s.split("--", 1)
        s = base
    test_type = "ut" if s.startswith("tests/ut/") else ("e2e" if s.startswith("tests/e2e/") else "other")
    return {
        "job_dir": job_dir,
        "test_path": s,
        "test_type": test_type,
        "test_func": test_func,
    }


def parse_hw_from_filename(filename: str) -> tuple[str, int]:
    """coverage.linux-aarch64-a2b3-1-runner... → ('A2', 1)。"""
    # 提取 linux-aarch64- 之后的 token
    m = re.search(r"linux-aarch64-([^-]+)-(\d+)-", filename)
    if not m:
        return ("unknown", 0)
    hw_tok = m.group(1).lower()
    cards = int(m.group(2))
    hw_map = {"a2b3": "A2", "a2": "A2", "a3": "A3", "310p": "310P", "a5": "A5"}
    return (hw_map.get(hw_tok, hw_tok.upper()), cards)


def clean_source_path(path: str) -> str:
    """/__w/vllm-ascend/vllm-ascend/vllm_ascend/platform.py → vllm_ascend/platform.py。"""
    if path.startswith(GITHUB_ACTIONS_PREFIX):
        return path[len(GITHUB_ACTIONS_PREFIX):]
    # 兜底：从 vllm_ascend/ 或 csrc/ 起截取
    for prefix in ("vllm_ascend/", "csrc/", "tests/"):
        idx = path.find(prefix)
        if idx >= 0:
            return path[idx:]
    return path


def module_of(path: str) -> str:
    """vllm_ascend/patch/platform/x.py → vllm_ascend/patch（取目录部分前两段）。"""
    parts = path.split("/")
    dirs = parts[:-1] if (len(parts) > 1 and "." in parts[-1]) else parts
    if len(dirs) >= 2:
        return "/".join(dirs[:2])
    return dirs[0] if dirs else path


# ---------------------------------------------------------------------------
# PR 覆盖广度矩阵（方案1：直接读 SQLite）
# ---------------------------------------------------------------------------
def read_covdata_sqlite(path: Path) -> dict | None:
    """只读读取单个 covdata SQLite，返回 file/path、arc 数、meta。损坏返回 None。"""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            files = [r[0] for r in con.execute("SELECT path FROM file LIMIT 10000").fetchall()]
            arcs = con.execute("SELECT COUNT(*) FROM arc").fetchone()[0]
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        logger.warning("Skip corrupt covdata %s: %s", path, e)
        return None
    cleaned = [clean_source_path(f) for f in files]
    return {
        "files": cleaned,
        "arcs": arcs,
        "when": meta.get("when"),
        "sys_argv": meta.get("sys_argv"),
        "version": meta.get("version"),
    }


def _is_covdata_member(member: tarfile.TarInfo) -> bool:
    """安全判定 tar 成员是否为可读的 covdata 文件。

    仅允许常规文件（isfile 仅对 REGTYPE 为真，自动排除符号链接/硬链接），
    并显式排除 sym/lnk 防止 extractfile 跟随链接读取敏感文件。
    """
    return (
        member.isfile()
        and not member.issym()
        and not member.islnk()
        and "/covdata/coverage." in member.name
    )


async def _head_signature(client: httpx.AsyncClient) -> str:
    """HTTP HEAD 获取 tar 签名（Content-Length + ETag + Last-Modified）。"""
    r = await client.head(settings.PR_COVERAGE_TAR_URL, timeout=30.0)
    r.raise_for_status()
    cl = r.headers.get("Content-Length", "")
    etag = r.headers.get("ETag", "")
    lm = r.headers.get("Last-Modified", "")
    return f"len:{cl};etag:{etag};last_modified:{lm}"


async def _download_tar(client: httpx.AsyncClient, dest: Path) -> None:
    """流式下载 tar 到 dest，带超时与重试。"""
    last_err: Exception | None = None
    for attempt in range(settings.PR_COVERAGE_DOWNLOAD_RETRIES):
        try:
            timeout = httpx.Timeout(settings.PR_COVERAGE_DOWNLOAD_TIMEOUT_SECONDS, connect=30.0)
            async with client.stream("GET", settings.PR_COVERAGE_TAR_URL, timeout=timeout) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_raw():
                        f.write(chunk)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("tar download attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(min(30 * (2 ** attempt), 300))
    raise RuntimeError(f"tar download failed after {settings.PR_COVERAGE_DOWNLOAD_RETRIES} retries: {last_err}")


def _process_tar_breadth(tar_path: Path, signature: str) -> dict:
    """流式读取 tar，逐个 covdata SQLite 汇总。"""
    jobs: dict[str, dict] = {}
    file_jobs: dict[str, set[str]] = defaultdict(set)
    file_hw: dict[str, set[str]] = defaultdict(set)
    module_files: dict[str, set[str]] = defaultdict(set)
    job_files: dict[str, set[str]] = defaultdict(set)  # 每作业去重文件并集
    total_covdata = 0
    total_arcs = 0
    all_source_files: set[str] = set()
    by_type: dict[str, int] = defaultdict(int)
    by_hw: dict[str, int] = defaultdict(int)
    latest_when: str | None = None
    task_id = ""

    with tarfile.open(tar_path, "r") as tar:
        for member in tar:
            if not _is_covdata_member(member):
                continue
            # 作业目录：vllm-ascend/VLLM-ASCEND@task_xxx/<job_dir>/covdata/coverage...
            parts = member.name.split("/")
            # 找到 VLLM-ASCEND@ 后取 task_id 和 job_dir
            try:
                va_idx = next(i for i, p in enumerate(parts) if p.startswith("VLLM-ASCEND@"))
                task_id = parts[va_idx].split("@", 1)[1]
                job_dir = parts[va_idx + 1]
            except (StopIteration, IndexError):
                continue

            # 解压单个成员到临时文件
            f = tar.extractfile(member)
            if f is None:
                continue
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".covdata")
            try:
                shutil.copyfileobj(f, tmp)
                tmp.close()
                stats = read_covdata_sqlite(Path(tmp.name))
            finally:
                try:
                    Path(tmp.name).unlink()
                except OSError:
                    pass

            if stats is None:
                continue
            total_covdata += 1
            total_arcs += stats["arcs"]
            hw, cards = parse_hw_from_filename(member.name)
            jd = decode_job_dir(job_dir)

            # 紬取文件级聚合
            for fp in stats["files"]:
                all_source_files.add(fp)
                file_jobs[fp].add(job_dir)
                file_hw[fp].add(hw)
                module_files[module_of(fp)].add(fp)
                job_files[job_dir].add(fp)  # 作业内去重并集

            # 作业级聚合
            if job_dir not in jobs:
                jobs[job_dir] = {
                    "job_dir": job_dir,
                    "test_path": jd["test_path"],
                    "test_type": jd["test_type"],
                    "test_func": jd["test_func"],
                    "hardware": hw,
                    "card_count": cards,
                    "covdata_count": 0,
                    "source_files_covered": 0,
                    "arcs": 0,
                    "latest_when": stats["when"],
                    "sys_argv": stats["sys_argv"],
                }
                by_type[jd["test_type"]] += 1
                by_hw[hw] += 1
            j = jobs[job_dir]
            j["covdata_count"] += 1
            j["arcs"] += stats["arcs"]
            j["source_files_covered"] = len(job_files[job_dir])  # 并集，非覆盖
            if stats["when"] and (j["latest_when"] is None or stats["when"] > j["latest_when"]):
                j["latest_when"] = stats["when"]
            if stats["when"] and (latest_when is None or stats["when"] > latest_when):
                latest_when = stats["when"]

    file_matrix = [
        {
            "source_path": fp,
            "module": module_of(fp),
            "covered_by_jobs": len(file_jobs[fp]),
            "covered_by_hardware": sorted(file_hw[fp]),
        }
        for fp in sorted(all_source_files)
    ]
    by_module = [
        {"module": m, "files": len(fs), "jobs_touching": len({job for fp in fs for job in file_jobs.get(fp, set())})}
        for m, fs in sorted(module_files.items())
    ]

    return {
        "summary": {
            "task_id": task_id,
            "total_jobs": len(jobs),
            "total_covdata_files": total_covdata,
            "total_source_files_covered": len(all_source_files),
            "total_arcs": total_arcs,
            "by_test_type": dict(by_type),
            "by_hardware": dict(by_hw),
            "generated_when": latest_when,
        },
        "jobs": list(jobs.values()),
        "file_matrix": file_matrix,
        "by_module": by_module,
        "tar_signature": signature,
        "updated_at": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# PR 行覆盖率%（方案2：coverage 包 combine）
# ---------------------------------------------------------------------------
def _write_coveragerc(rc_path: Path, source_dir: Path | None = None) -> None:
    """生成 .coveragerc，[paths] 将 CI 路径映射到本地 clone。"""
    canonical = str(source_dir / "vllm_ascend") if source_dir else "vllm_ascend"
    rc_path.write_text(
        "[run]\nsource = vllm_ascend\n"
        "[paths]\nvllm_ascend =\n"
        f"    {canonical}\n"
        "    /__w/vllm-ascend/vllm-ascend/vllm_ascend\n"
        "    */vllm_ascend\n"
        "[report]\nexclude_lines =\n"
        "    pragma: no cover\n"
        "    if __name__ == .__main__.:\n"
        "    raise NotImplementedError\n",
        encoding="utf-8",
    )


def _run(cmd: list[str], cwd: Path, timeout: int) -> None:
    """运行子进程，超时抛 RuntimeError。"""
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{' '.join(cmd)} timed out after {timeout}s") from e
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "")[:500]
        raise RuntimeError(f"{' '.join(cmd)} failed: {msg}")


def _infer_covdata_commit(cache: Any, covdata_when: str | None) -> str | None:
    """从 covdata.when 用 git log 推断对应 commit。"""
    if not covdata_when:
        return None
    try:
        r = subprocess.run(
            ["git", "log", "-1", f"--before={covdata_when}", "--format=%H"],
            cwd=str(cache.cache_dir), capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            sha = r.stdout.strip()
            return sha or None
    except Exception:  # noqa: BLE001
        pass
    return None


def _version_gap(cache: Any, covdata_commit: str | None, source_commit: str | None) -> int | None:
    if not covdata_commit or not source_commit:
        return None
    try:
        r = subprocess.run(
            ["git", "rev-list", "--count", f"{covdata_commit}..{source_commit}"],
            cwd=str(cache.cache_dir), capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return int(r.stdout.strip())
    except Exception:  # noqa: BLE001
        pass
    return None


def _parse_coverage_json(report_path: Path, tar_signature: str, cache: Any,
                         covdata_when: str | None, covdata_version: str | None) -> dict:
    data = json.loads(report_path.read_text(encoding="utf-8"))
    totals = data.get("totals", {})
    files_in = data.get("files", {})

    files_out = []
    by_module: dict[str, dict] = defaultdict(lambda: {"statements": 0, "covered": 0, "branches": 0, "covered_branches": 0, "files": 0})
    for path, finfo in files_in.items():
        clean = clean_source_path(path)
        mod = module_of(clean)
        summ = finfo.get("summary", {})
        files_out.append({
            "path": clean,
            "module": mod,
            "statements": summ.get("num_statements", 0),
            "missing": summ.get("missing_lines", 0),
            "covered": summ.get("covered_lines", 0),
            "percent_covered": round(summ.get("percent_covered", 0), 2),
            "has_branches": summ.get("num_branches", 0) > 0,
        })
        m = by_module[mod]
        m["statements"] += summ.get("num_statements", 0)
        m["covered"] += summ.get("covered_lines", 0)
        m["branches"] += summ.get("num_branches", 0)
        m["covered_branches"] += summ.get("covered_branches", 0)
        m["files"] += 1

    by_module_out = [
        {
            "module": m,
            "statements": v["statements"],
            "covered": v["covered"],
            "percent": round(v["covered"] / v["statements"] * 100, 2) if v["statements"] else 0.0,
            "branches": v["branches"],
            "covered_branches": v["covered_branches"],
            "files": v["files"],
        }
        for m, v in sorted(by_module.items())
    ]

    source_commit = None
    try:
        commit_info = cache.get_latest_commit()
        source_commit = commit_info.get("sha") if isinstance(commit_info, dict) else commit_info
    except Exception:  # noqa: BLE001
        pass
    covdata_commit = _infer_covdata_commit(cache, covdata_when)
    gap = _version_gap(cache, covdata_commit, source_commit)

    # 状态判定
    status = "ok"
    status_reason = None
    warning = None
    threshold = settings.PR_COVERAGE_VERSION_GAP_THRESHOLD
    if covdata_version and settings.PR_COVERAGE_LINE_ENABLED:
        installed = _installed_coverage_version()
        if installed and not installed.startswith(covdata_version.rsplit(".", 1)[0]):
            # 主/次版本不一致
            status = "partial"
            status_reason = "tool_version"
            warning = f"coverage 工具版本不一致：covdata={covdata_version}, installed={installed}"
    if gap is not None and gap > threshold:
        status = "partial"
        status_reason = "version_mismatch"
        warning = (f"近似值：分母源码(HEAD {source_commit[:7] if source_commit else '?'})与覆盖率数据"
                   f"(commit {covdata_commit[:7] if covdata_commit else '?'})存在 {gap} 个 commit 偏差，行覆盖率仅供参考")
    elif gap is None:
        status = "partial"
        status_reason = "version_mismatch"
        warning = "无法推断 covdata 对应 commit，行覆盖率可能存在版本偏差，仅供参考"

    return {
        "totals": {
            "num_statements": totals.get("num_statements", 0),
            "covered_lines": totals.get("covered_lines", 0),
            "missing_lines": totals.get("missing_lines", 0),
            "percent_covered": round(totals.get("percent_covered", 0), 2),
            "percent_statements_covered": round(totals.get("percent_statements_covered", totals.get("percent_covered", 0)), 2),
            "num_branches": totals.get("num_branches", 0),
            "covered_branches": totals.get("covered_branches", 0),
            "missing_branches": totals.get("missing_branches", 0),
            "num_partial_branches": totals.get("num_partial_branches", 0),
            "percent_branches_covered": round(totals.get("percent_branches_covered", 0), 2),
            "num_files": len(files_out),
        },
        "by_module": by_module_out,
        "files": files_out,
        "tar_signature": tar_signature,
        "source_commit": source_commit,
        "covdata_commit": covdata_commit,
        "covdata_when": covdata_when,
        "version_gap_commits": gap,
        "coverage_tool_version": covdata_version,
        "installed_coverage_version": _installed_coverage_version(),
        "updated_at": datetime.now(UTC).isoformat(),
        "status": status,
        "status_reason": status_reason,
        "warning": warning,
    }


def _installed_coverage_version() -> str | None:
    try:
        import coverage
        return coverage.__version__
    except Exception:  # noqa: BLE001
        return None


def _safe_commit(cache: Any) -> str | None:
    try:
        info = cache.get_latest_commit()
        return info.get("sha") if isinstance(info, dict) else info
    except Exception:  # noqa: BLE001
        return None


def _validate_path_mapping(tar_path: Path, cache: Any, sample_size: int = 5) -> bool:
    """[paths] 预校验：采样多个 covdata 的 file.path，验证映射后在本地 clone 存在。

    采样最多 5 个 covdata 文件，聚合所有路径计算命中率。
    返回 True 表示映射生效（多数采样文件存在），False 表示映射失败（应降级跳过 combine）。
    """
    sampled = 0
    found = 0
    covdata_checked = 0
    max_covdata = 5
    try:
        with tarfile.open(tar_path, "r") as tar:
            for member in tar:
                if covdata_checked >= max_covdata:
                    break
                if not _is_covdata_member(member):
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".covdata")
                try:
                    shutil.copyfileobj(f, tmp)
                    tmp.close()
                    stats = read_covdata_sqlite(Path(tmp.name))
                finally:
                    Path(tmp.name).unlink(missing_ok=True)
                if not stats or not stats["files"]:
                    continue
                covdata_checked += 1
                for raw in stats["files"][:sample_size]:
                    sampled += 1
                    if (cache.cache_dir / raw).exists():
                        found += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("path mapping pre-validation failed: %s", e)
        return False
    if sampled == 0:
        return True  # 无文件可采样，不阻断
    return (found / sampled) >= 0.5


def process_line_coverage(tar_path: Path, tar_signature: str, covdata_when: str | None,
                          covdata_version: str | None) -> dict:
    """方案2：展开 covdata → coverage combine → coverage json → 解析。"""
    if not settings.PR_COVERAGE_LINE_ENABLED:
        return {
            "status": "failed", "status_reason": "disabled",
            "warning": "方案2 已通过配置关闭", "totals": {}, "by_module": [], "files": [],
            "tar_signature": tar_signature, "updated_at": datetime.now(UTC).isoformat(),
        }
    cache = get_github_cache()
    work_dir = Path(tempfile.mkdtemp(prefix="covline_"))
    covdata_dir = work_dir / "covdata"
    covdata_dir.mkdir(parents=True)
    try:
        # 展开所有 covdata 文件
        with tarfile.open(tar_path, "r") as tar:
            for member in tar:
                if not _is_covdata_member(member):
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                # coverage combine 仅识别 .coverage.* 文件名模式
                safe_name = ".coverage." + hashlib.sha1(member.name.encode()).hexdigest()[:16]
                outp = covdata_dir / safe_name
                with open(outp, "wb") as wf:
                    shutil.copyfileobj(f, wf)

        rc = work_dir / ".coveragerc"
        _write_coveragerc(rc, cache.cache_dir)

        # [paths] 预校验：采样 covdata file.path，验证映射后在本地 clone 存在；
        # 失败则降级为 partial/path_mapping 并跳过昂贵的 combine（检视意见 #4）
        if not _validate_path_mapping(tar_path, cache):
            logger.warning("path mapping pre-validation failed, skipping combine")
            return {
                "totals": {}, "by_module": [], "files": [],
                "tar_signature": tar_signature,
                "source_commit": _safe_commit(cache), "covdata_commit": None,
                "covdata_when": covdata_when, "version_gap_commits": None,
                "coverage_tool_version": covdata_version,
                "installed_coverage_version": _installed_coverage_version(),
                "updated_at": datetime.now(UTC).isoformat(),
                "status": "partial", "status_reason": "path_mapping",
                "warning": "[paths] 路径映射预校验失败：covdata 源码路径在本地 clone 未找到，已跳过行覆盖率计算",
            }

        # combine + json（cwd 设为本地 clone 目录，让 coverage json 能找到源码）
        _run(["python", "-m", "coverage", "combine", "--rcfile", str(rc), str(covdata_dir)], cache.cache_dir,
             settings.PR_COVERAGE_LINE_TIMEOUT_SECONDS)
        report_json = work_dir / "coverage.json"
        _run(["python", "-m", "coverage", "json", "-o", str(report_json), "--rcfile", str(rc)],
             cache.cache_dir, settings.PR_COVERAGE_LINE_TIMEOUT_SECONDS)

        if not report_json.exists():
            raise RuntimeError("coverage.json not generated")

        result = _parse_coverage_json(report_json, tar_signature, cache, covdata_when, covdata_version)

        # 保留 coverage.json 供代码浏览器按需读取
        data_dir = Path(settings.DATA_DIR) / "coverage"
        data_dir.mkdir(parents=True, exist_ok=True)
        sig_short = hashlib.sha1(tar_signature.encode()).hexdigest()[:12]
        keep = data_dir / f"coverage_{sig_short}.json"
        shutil.copy2(report_json, keep)
        # 清理旧签名文件
        for old in data_dir.glob("coverage_*.json"):
            if old != keep:
                old.unlink()
        result["coverage_json_path"] = str(keep)
        return result
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 读取源码（代码浏览器）
# ---------------------------------------------------------------------------
_PATH_RE = re.compile(r"^[a-zA-Z0-9_./-]+$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def get_source_at_commit(path: str, commit: str | None) -> tuple[str, bool]:
    """返回 (source, aligned)。用 git show {commit}:{path}；失败回退 HEAD 文件。"""
    # 安全校验：路径仅允许字母/数字/_./-（拦截 null byte、空格、..、特殊字符）；
    # commit 必须为 40 字符 hex SHA（拦截 --all、HEAD~1 等 git ref 注入）
    if not path.startswith("vllm_ascend/") and not path.startswith("csrc/"):
        raise ValueError("path must be under vllm_ascend/ or csrc/")
    if ".." in path or path.startswith("/") or not _PATH_RE.match(path):
        raise ValueError("invalid path")
    if commit is not None and not _COMMIT_RE.match(commit):
        raise ValueError("invalid commit (must be 40-char hex SHA)")
    cache = get_github_cache()
    if commit:
        try:
            r = subprocess.run(
                ["git", "show", f"{commit}:{path}"],
                cwd=str(cache.cache_dir), capture_output=True, timeout=15,
            )
            if r.returncode == 0:
                return (r.stdout.decode("utf-8", errors="replace"), True)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    # 回退 HEAD 文件
    fp = cache.cache_dir / path
    if fp.exists():
        return (fp.read_text(encoding="utf-8", errors="replace"), False)
    raise FileNotFoundError(f"source not found: {path}")


def read_file_coverage_from_json(coverage_json_path: str, path: str) -> dict | None:
    """从磁盘 coverage.json 读取单文件的逐行 + 分支覆盖数据。"""
    p = Path(coverage_json_path).resolve()
    allowed_dir = Path(settings.DATA_DIR).resolve() / "coverage"
    if not p.is_relative_to(allowed_dir):
        logger.warning("coverage_json_path escapes allowed directory: %s", coverage_json_path)
        return None
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    files = data.get("files", {})
    finfo = files.get(path) or files.get(path.rstrip("/"))
    if not finfo:
        # 尝试带前缀匹配
        for k, v in files.items():
            if clean_source_path(k) == path:
                finfo = v
                break
    if not finfo:
        return None
    return {
        "executed_lines": finfo.get("executed_lines", []),
        "missing_lines": finfo.get("missing_lines", []),
        "excluded_lines": finfo.get("excluded_lines", []),
        "executed_branches": finfo.get("executed_branches", []),
        "missing_branches": finfo.get("missing_branches", []),
        "summary": finfo.get("summary", {}),
    }


# ---------------------------------------------------------------------------
# 统一同步入口
# ---------------------------------------------------------------------------
async def sync_e2e(db: AsyncSession) -> dict:
    try:
        parser = E2ECoverageParser()
        result = parser.parse()
        # 变更检测：source_file_hash 与存储一致则跳过写库（设计承诺）
        existing = await _load_config(db, E2E_KEY)
        if existing and existing.get("source_file_hash") == result["source_file_hash"]:
            logger.info("E2E coverage skipped (source_file_hash unchanged)")
            return {"success": True, "skipped": True, "updated_at": existing.get("updated_at"),
                    "repo_commit": result["repo_commit"]}
        await _save_config(db, E2E_KEY, result, "E2E 特性覆盖率数据")
        await db.commit()
        logger.info("E2E coverage synced: %s tests", result["summary"]["total_tests"])
        return {"success": True, "updated_at": result["updated_at"], "repo_commit": result["repo_commit"]}
    except Exception as e:  # noqa: BLE001
        logger.error("E2E coverage sync failed: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


async def sync_pr_breadth(db: AsyncSession) -> dict:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            sig = await _head_signature(client)
        except Exception as e:  # noqa: BLE001
            logger.error("PR coverage HEAD failed: %s", e)
            return {"success": False, "error": f"HEAD failed: {e}"}
        existing = await _load_config(db, PR_BREADTH_KEY)
        if existing and existing.get("tar_signature") == sig:
            logger.info("PR coverage breadth skipped (signature unchanged)")
            return {"success": True, "skipped": True, "tar_signature": sig}

        # 磁盘空间预检
        tmp = Path(tempfile.gettempdir())
        usage = shutil.disk_usage(tmp)
        if usage.free < 1024 * 1024 * 1024:
            return {"success": False, "error": f"insufficient disk space: {usage.free // (1024*1024)}MB free"}

        tar_path = tmp / "coverage_download.tar"
        try:
            await _download_tar(client, tar_path)
            result = await asyncio.to_thread(_process_tar_breadth, tar_path, sig)
            await _save_config(db, PR_BREADTH_KEY, result, "PR 流水线覆盖广度矩阵")
            await db.commit()
            logger.info("PR coverage breadth synced: %s jobs", result["summary"]["total_jobs"])
            # 记录 tar 路径供方案2复用
            return {"success": True, "tar_signature": sig, "tar_path": str(tar_path),
                    "covdata_when": result["summary"].get("generated_when")}
        finally:
            # 方案2 会在同一调度内复用 tar；breadth 完成后由调用方决定是否删除
            pass


async def sync_pr_lines(db: AsyncSession, tar_path: str | None, tar_signature: str | None,
                        covdata_when: str | None) -> dict:
    if not settings.PR_COVERAGE_LINE_ENABLED:
        return {"success": False, "skipped": True, "reason": "disabled"}
    # 变更检测：tar 签名与存储一致则跳过昂贵的 combine（设计承诺 + 避免每小时 600s 阻塞）
    if tar_signature:
        existing_lines = await _load_config(db, PR_LINES_KEY)
        if existing_lines and existing_lines.get("tar_signature") == tar_signature:
            logger.info("PR coverage lines skipped (tar_signature unchanged)")
            return {"success": True, "skipped": True, "tar_signature": tar_signature,
                    "status": existing_lines.get("status")}
    # 复用 breadth 已下载的 tar；若无则重新下载
    sig = tar_signature
    tp = Path(tar_path) if tar_path else None
    if tp is None or not tp.exists():
        async with httpx.AsyncClient(follow_redirects=True) as client:
            try:
                sig = await _head_signature(client)
            except Exception as e:  # noqa: BLE001
                return {"success": False, "error": f"HEAD failed: {e}"}
            tp = Path(tempfile.gettempdir()) / "coverage_download_lines.tar"
            await _download_tar(client, tp)

    # covdata 版本（从 breadth 或采样）
    covdata_version = None
    try:
        with tarfile.open(tp, "r") as tar:
            for member in tar:
                if _is_covdata_member(member):
                    f = tar.extractfile(member)
                    if f is None:
                        continue
                    tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".covdata")
                    shutil.copyfileobj(f, tmpf)
                    tmpf.close()
                    stats = read_covdata_sqlite(Path(tmpf.name))
                    Path(tmpf.name).unlink(missing_ok=True)
                    if stats and stats.get("version"):
                        covdata_version = stats["version"]
                    break
    except Exception as e:  # noqa: BLE001
        logger.warning("sample covdata version failed: %s", e)

    try:
        result = await asyncio.to_thread(process_line_coverage, tp, sig or "", covdata_when, covdata_version)
        await _save_config(db, PR_LINES_KEY, result, "PR 流水线行覆盖率%")
        await db.commit()
        logger.info("PR coverage lines synced: status=%s percent=%s",
                    result.get("status"), result.get("totals", {}).get("percent_covered"))
        return {"success": result.get("status") != "failed",
                "status": result.get("status"), "tar_signature": sig}
    except Exception as e:  # noqa: BLE001
        logger.error("PR coverage lines sync failed: %s", e, exc_info=True)
        # 保留上次结果，标记 failed
        return {"success": False, "error": str(e)}
    finally:
        if tar_path is None and tp is not None:
            try:
                tp.unlink()
            except OSError:
                pass


async def sync_all_coverage(db: AsyncSession, source: str = "all") -> dict:
    """调度器/手动触发入口。进程内 asyncio.Lock 串行化。"""
    lock = _get_lock()
    if lock.locked():
        raise RuntimeError("coverage sync in progress")
    async with lock:
        status: dict[str, Any] = {"last_check_at": datetime.now(UTC).isoformat()}
        tar_path: str | None = None
        tar_sig: str | None = None
        covdata_when: str | None = None

        if source in ("all", "e2e"):
            status["e2e"] = await sync_e2e(db)

        if source in ("all", "pr_breadth"):
            r = await sync_pr_breadth(db)
            status["pr_breadth"] = {k: v for k, v in r.items() if k != "tar_path"}
            # 始终捕获 tar 签名（即使 breadth 跳过），供 pr_lines 变更检测
            tar_sig = r.get("tar_signature") or tar_sig
            if r.get("success") and not r.get("skipped"):
                tar_path = r.get("tar_path")
                covdata_when = r.get("covdata_when")

        if source in ("all", "pr_lines"):
            # 方案2 变更检测：签名与存储一致则跳过；变更则作为独立后台任务不阻塞调度循环
            existing_lines = await _load_config(db, PR_LINES_KEY)
            if existing_lines and tar_sig and existing_lines.get("tar_signature") == tar_sig:
                status["pr_lines"] = {"success": True, "skipped": True, "tar_signature": tar_sig,
                                      "status": existing_lines.get("status")}
            else:
                # 后台执行（独立 session + 自管 tar 清理），不阻塞本 job 返回
                task = asyncio.create_task(_pr_lines_bg(tar_path, tar_sig, covdata_when))
                _bg_tasks.add(task)
                task.add_done_callback(_bg_tasks.discard)
                # breadth 下载的 tar 交给 bg 任务清理，此处不删除
                tar_path = None
                status["pr_lines"] = {"success": True, "status": "scheduled",
                                      "tar_signature": tar_sig}

        await _save_sync_status(db, status)
        await db.commit()
        # 清理 breadth 下载的 tar（若未被 pr_lines bg 接管）
        if tar_path:
            try:
                Path(tar_path).unlink()
            except OSError:
                pass
        return status


async def _pr_lines_bg(tar_path: str | None, tar_signature: str | None, covdata_when: str | None) -> None:
    """方案2 行覆盖率后台任务：独立 session，自管 tar 清理，不阻塞调度。完成后回写同步状态。"""
    from app.db.base import SessionLocal
    try:
        async with SessionLocal() as db:
            result = await sync_pr_lines(db, tar_path, tar_signature, covdata_when)
            logger.info("PR coverage lines (bg) done: %s", result)
            # 回写 SYNC_STATUS_KEY，让前端看到完成状态
            sync_status = await _load_config(db, SYNC_STATUS_KEY)
            if sync_status:
                sync_status["pr_lines"] = {
                    "success": True,
                    "status": result.get("status", "ok"),
                    "tar_signature": tar_signature,
                }
                await _save_sync_status(db, sync_status)
                await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.error("PR coverage lines (bg) failed: %s", e, exc_info=True)
        # 回写失败状态
        try:
            async with SessionLocal() as db:
                sync_status = await _load_config(db, SYNC_STATUS_KEY)
                if sync_status:
                    sync_status["pr_lines"] = {
                        "success": False,
                        "status": "failed",
                        "error": str(e)[:200],
                        "tar_signature": tar_signature,
                    }
                    await _save_sync_status(db, sync_status)
                    await db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        if tar_path:
            try:
                Path(tar_path).unlink()
            except OSError:
                pass


async def _save_sync_status(db: AsyncSession, status: dict) -> None:
    await _save_config(db, SYNC_STATUS_KEY, status, "测试覆盖率同步状态")


async def get_sync_status(db: AsyncSession) -> dict:
    cfg = await _load_config(db, SYNC_STATUS_KEY)
    return cfg or {"last_check_at": None}


# ---------------------------------------------------------------------------
# 查询接口（供 API 调用）
# ---------------------------------------------------------------------------
async def get_e2e_coverage(db: AsyncSession) -> dict:
    return await _load_config(db, E2E_KEY) or {"summary": {}, "taxonomy": {}, "tests": [], "updated_at": None}


def _paginate(items: list, page: int, per_page: int) -> tuple[list, int]:
    total = len(items)
    start = (page - 1) * per_page
    return items[start:start + per_page], total


async def get_pr_breadth(db: AsyncSession, page: int = 1, per_page: int = 50,
                         module: str | None = None, sort: str | None = None,
                         order: str = "desc", fmt: str | None = None) -> dict:
    data = await _load_config(db, PR_BREADTH_KEY)
    if not data:
        return {"summary": {}, "jobs": [], "file_matrix": [], "by_module": [], "updated_at": None}
    if fmt == "csv":
        return {"csv": _breadth_csv(data, module)}
    file_matrix = list(data.get("file_matrix", []))
    if module:
        file_matrix = [f for f in file_matrix if f.get("module") == module]
    if sort == "covered_by_jobs":
        file_matrix = sorted(file_matrix, key=lambda x: x.get("covered_by_jobs", 0), reverse=(order == "desc"))
    paged, total = _paginate(file_matrix, page, per_page)
    return {
        "summary": data.get("summary", {}),
        "jobs": data.get("jobs", []),
        "file_matrix": paged,
        "file_matrix_total": total,
        "by_module": data.get("by_module", []),
        "tar_signature": data.get("tar_signature"),
        "updated_at": data.get("updated_at"),
    }


def _csv_safe(val: Any) -> Any:
    """防止 CSV 公式注入：以 =/+/-/@ 开头的单元格前缀 ' """
    s = str(val) if val is not None else ""
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return val


def _breadth_csv(data: dict, module: str | None) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["source_path", "module", "covered_by_jobs", "covered_by_hardware"])
    for f in data.get("file_matrix", []):
        if module and f.get("module") != module:
            continue
        w.writerow([_csv_safe(f["source_path"]), _csv_safe(f["module"]), f["covered_by_jobs"],
                    ";".join(f.get("covered_by_hardware", []))])
    return out.getvalue()


async def get_pr_lines(db: AsyncSession, page: int = 1, per_page: int = 50,
                       sort: str | None = None, order: str = "desc", fmt: str | None = None) -> dict:
    data = await _load_config(db, PR_LINES_KEY)
    if not data:
        return {"totals": {}, "by_module": [], "files": [], "updated_at": None, "status": "unknown"}
    data.pop("coverage_json_path", None)  # 不暴露服务器路径给前端
    if fmt == "csv":
        return {"csv": _lines_csv(data)}
    files = list(data.get("files", []))
    if sort == "percent_covered":
        files = sorted(files, key=lambda x: x.get("percent_covered", 0), reverse=(order == "desc"))
    paged, total = _paginate(files, page, per_page)
    return {
        "totals": data.get("totals", {}),
        "by_module": data.get("by_module", []),
        "files": paged,
        "files_total": total,
        "tar_signature": data.get("tar_signature"),
        "source_commit": data.get("source_commit"),
        "covdata_commit": data.get("covdata_commit"),
        "covdata_when": data.get("covdata_when"),
        "version_gap_commits": data.get("version_gap_commits"),
        "coverage_tool_version": data.get("coverage_tool_version"),
        "installed_coverage_version": data.get("installed_coverage_version"),
        "status": data.get("status", "unknown"),
        "status_reason": data.get("status_reason"),
        "warning": data.get("warning"),
        "updated_at": data.get("updated_at"),
    }


def _lines_csv(data: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["path", "module", "statements", "missing", "covered", "percent_covered", "has_branches"])
    for f in data.get("files", []):
        w.writerow([_csv_safe(f["path"]), _csv_safe(f["module"]), f["statements"], f["missing"], f["covered"],
                    f["percent_covered"], f.get("has_branches", False)])
    return out.getvalue()


async def get_pr_source(db: AsyncSession, path: str) -> dict:
    lines_cfg = await _load_config(db, PR_LINES_KEY)
    if not lines_cfg:
        raise FileNotFoundError("PR line coverage not synced yet")
    commit = lines_cfg.get("covdata_commit") or lines_cfg.get("source_commit")
    # 兼容旧数据：commit 可能是 dict {sha, subject, ...}
    if isinstance(commit, dict):
        commit = commit.get("sha")
    source, aligned = get_source_at_commit(path, commit)
    cov = read_file_coverage_from_json(lines_cfg.get("coverage_json_path", ""), path)
    github_url = f"https://github.com/{settings.GITHUB_OWNER}/{settings.GITHUB_REPO}/blob/{commit}/{path}" if commit else None
    return {
        "path": path,
        "commit": commit,
        "source": source,
        "executed_lines": cov.get("executed_lines", []) if cov else [],
        "missing_lines": cov.get("missing_lines", []) if cov else [],
        "excluded_lines": cov.get("excluded_lines", []) if cov else [],
        "executed_branches": cov.get("executed_branches", []) if cov else [],
        "missing_branches": cov.get("missing_branches", []) if cov else [],
        "summary": cov.get("summary", {}) if cov else {},
        "github_url": github_url,
        "source_aligned": aligned,
    }
