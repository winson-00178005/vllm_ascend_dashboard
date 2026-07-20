"""本地代码度量采集器 — 在 Dashboard 服务器上直接运行 cloc/lizard/jscpd"""
import asyncio
import json
import logging
import os
import subprocess
from datetime import date
from pathlib import Path

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CodeMetricsSnapshot, CodeComplexityDetail, CodeDuplicationDetail,
    CodeSecurityDetail,
)

logger = logging.getLogger(__name__)


class CodeMetricsCollector:
    """本地代码度量采集器"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def collect(self, branch: str = "main") -> dict:
        """执行采集，返回结果摘要"""
        # 1. 获取仓库路径
        repo_path = await self._get_repo_path()
        if not repo_path:
            return {"status": "failed", "message": "无法获取仓库路径"}

        # 2. 运行采集工具
        results = {"collection_status": "complete", "collection_tools": []}

        # cloc — 代码规模
        cloc_data = await self._run_cloc(repo_path)
        if cloc_data:
            results.update(cloc_data)
            results["collection_tools"].append("cloc")
        else:
            results["collection_status"] = "partial"

        # lizard — 圈复杂度
        lizard_data = await self._run_lizard(repo_path)
        if lizard_data:
            results.update(lizard_data["summary"])
            results["complexity_details"] = lizard_data["details"]
            results["collection_tools"].append("lizard")
        else:
            results["collection_status"] = "partial"

        # jscpd — 重复率
        jscpd_data = await self._run_jscpd(repo_path)
        if jscpd_data:
            results.update(jscpd_data["summary"])
            results["duplication_details"] = jscpd_data["details"]
            results["collection_tools"].append("jscpd")
        else:
            results["collection_status"] = "partial"

        # grep — 技术债务
        debt_data = await self._count_tech_debt(repo_path)
        if debt_data:
            results.update(debt_data)
            results["collection_tools"].append("grep")

        # 3. 计算健康度
        from app.api.v1.code_metrics import _calculate_health_score
        health = _calculate_health_score(results)
        results.update(health)

        # 4. 模块级 LOC
        results["module_loc"] = await self._count_module_loc(repo_path)
        results["language_loc"] = self._extract_language_loc(cloc_data)

        # 5. 保存到数据库
        snapshot_id = await self._save_snapshot(results, branch)

        return {
            "status": "success",
            "snapshot_id": snapshot_id,
            "tools": results["collection_tools"],
            "collection_status": results["collection_status"],
            "health_score": health["health_score"],
            "total_loc": results.get("total_loc", 0),
        }

    async def _get_repo_path(self) -> str | None:
        """获取 vllm-ascend 仓库本地路径"""
        try:
            from app.services.github_cache import get_github_cache_for_repo
            cache = get_github_cache_for_repo("vllm-project", "vllm-ascend")
            if hasattr(cache, 'repo_path'):
                return cache.repo_path
            elif hasattr(cache, '_repo_path'):
                return cache._repo_path
            elif hasattr(cache, 'cache_dir'):
                return str(cache.cache_dir)
            else:
                import app.core.config as config
                base = getattr(config.settings, 'GITHUB_CACHE_DIR', '/app/data/repos')
                return os.path.join(base, "vllm-project_vllm-ascend")
        except Exception as e:
            logger.warning(f"Failed to get repo path from cache: {e}")
            path = "/app/data/repos/vllm-project_vllm-ascend"
            if os.path.exists(path):
                return path
            return None

    async def _run_cloc(self, repo_path: str) -> dict | None:
        """运行 cloc 统计代码行数"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "cloc", "--json", "--quiet", repo_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            data = json.loads(stdout.decode())

            total = data.get("SUM", {})
            lang_loc = {}
            total_loc = 0
            total_files = 0
            for key, val in data.items():
                if key == "SUM" or key == "header":
                    continue
                if isinstance(val, dict):
                    lang_loc[key] = val.get("code", 0)
                    total_loc += val.get("code", 0)
                    total_files += val.get("nFiles", 0)

            return {
                "total_loc": total_loc or total.get("code", 0),
                "total_raw_lines": total.get("nLines", 0),
                "total_files": total_files or total.get("nFiles", 0),
                "loc_python": lang_loc.get("Python", 0),
                "loc_cpp": lang_loc.get("C++", 0),
                "loc_c": lang_loc.get("C", 0),
                "loc_cmake": lang_loc.get("CMake", 0),
                "loc_shell": lang_loc.get("Bourne Shell", 0) + lang_loc.get("Shell", 0),
                "lang_loc": lang_loc,
            }
        except FileNotFoundError:
            logger.warning("cloc not installed, skipping code size metrics")
            return None
        except Exception as e:
            logger.warning(f"cloc failed: {e}")
            return None

    async def _run_lizard(self, repo_path: str) -> dict | None:
        """运行 lizard 分析圈复杂度（使用 Python API，不依赖 CLI --json）"""
        try:
            import lizard as lizard_lib

            def _analyze():
                import lizard as lizard_lib
                results = []
                for root, dirs, files in os.walk(repo_path):
                    dirs[:] = [d for d in dirs if d not in ['.git', 'build', '__pycache__', 'node_modules', '.venv', 'dist']]
                    for fname in files:
                        if fname.endswith(('.py', '.cpp', '.cc', '.h', '.hpp', '.c')):
                            fpath = os.path.join(root, fname)
                            try:
                                info = lizard_lib.analyze_file(fpath)
                                for func in info.function_list:
                                    results.append({
                                        "file_path": fpath,
                                        "function_name": func.name,
                                        "cyclomatic_complexity": func.cyclomatic_complexity,
                                        "max_nesting_depth": getattr(func, "max_nesting_depth", 0),
                                        "nloc": func.nloc,
                                        "start_line": func.start_line,
                                    })
                            except Exception:
                                pass
                return results

            functions = await asyncio.to_thread(_analyze)

            total_functions = len(functions)
            if total_functions == 0:
                return None

            cc_values = [f["cyclomatic_complexity"] for f in functions]
            cc_total = sum(cc_values)
            cc_maximum = max(cc_values) if cc_values else 0
            cc_per_method = cc_total / total_functions if total_functions > 0 else 0
            cc_huge_count = sum(1 for c in cc_values if c > 15)
            cc_huge_ratio = (cc_huge_count / total_functions * 100) if total_functions > 0 else 0
            cc_adequacy = ((total_functions - cc_huge_count) / total_functions * 100) if total_functions > 0 else 0

            depths = [f["max_nesting_depth"] for f in functions]
            max_depth = max(depths) if depths else 0
            depth_huge_count = sum(1 for d in depths if d > 5)
            depth_huge_ratio = (depth_huge_count / total_functions * 100) if total_functions > 0 else 0

            method_lines = [f["nloc"] for f in functions]
            method_lines_total = sum(method_lines)
            lines_per_method = method_lines_total / total_functions if total_functions > 0 else 0
            huge_method_count = sum(1 for l in method_lines if l > 80)
            huge_method_ratio = (huge_method_count / total_functions * 100) if total_functions > 0 else 0

            # Detail: top 500 by complexity
            details = []
            for f in sorted(functions, key=lambda x: x["cyclomatic_complexity"], reverse=True)[:500]:
                details.append({
                    "file_path": f["file_path"],
                    "function_name": f["function_name"],
                    "language": "Python" if f["file_path"].endswith(".py") else "C++",
                    "cyclomatic_complexity": f["cyclomatic_complexity"],
                    "max_nesting_depth": f["max_nesting_depth"],
                    "function_lines": f["nloc"],
                    "start_line": f["start_line"],
                })

            return {
                "summary": {
                    "total_functions": total_functions,
                    "cc_total": cc_total,
                    "cc_per_method": round(cc_per_method, 2),
                    "cc_maximum": cc_maximum,
                    "cc_huge_count": cc_huge_count,
                    "cc_huge_ratio": round(cc_huge_ratio, 2),
                    "cc_adequacy": round(cc_adequacy, 2),
                    "max_depth": max_depth,
                    "depth_huge_count": depth_huge_count,
                    "depth_huge_ratio": round(depth_huge_ratio, 2),
                    "method_lines_total": method_lines_total,
                    "lines_per_method": round(lines_per_method, 2),
                    "huge_method_count": huge_method_count,
                    "huge_method_ratio": round(huge_method_ratio, 2),
                },
                "details": details,
            }
        except ImportError:
            logger.warning("lizard not installed, skipping complexity metrics")
            return None
        except Exception as e:
            logger.warning(f"lizard failed: {e}")
            return None

    async def _run_jscpd(self, repo_path: str) -> dict | None:
        """运行 jscpd 检测重复代码"""
        try:
            import shutil

            output_dir = "/tmp/jscpd_output"
            os.makedirs(output_dir, exist_ok=True)

            # Find jscpd binary — try PATH, then common install locations, then npx
            jscpd_bin = shutil.which("jscpd") or "/usr/bin/jscpd"
            if not os.path.exists(jscpd_bin):
                args = ["npx", "jscpd", "--reporters", "json", "--output", output_dir, repo_path]
            else:
                args = [jscpd_bin, "--reporters", "json", "--output", output_dir, repo_path]

            env = {**os.environ, "PATH": os.environ.get("PATH", "") + ":/usr/bin:/usr/local/bin"}
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

            # jscpd outputs to a JSON report file; fall back to stdout for some versions
            data = None
            for candidate in [
                os.path.join(output_dir, "jscpd-report.json"),
                os.path.join(output_dir, "json", "jscpd-report.json"),
                os.path.join(output_dir, "jscpd.json"),
            ]:
                if os.path.exists(candidate):
                    try:
                        with open(candidate) as f:
                            data = json.load(f)
                        break
                    except Exception as e:
                        logger.warning(f"Failed to read jscpd report {candidate}: {e}")

            if data is None:
                # Try parsing stdout (some jscpd versions print JSON to stdout)
                try:
                    data = json.loads(stdout.decode())
                except Exception:
                    logger.warning(
                        f"jscpd produced no parseable JSON output. stderr: {stderr.decode()[:500]}"
                    )
                    return None

            # jscpd 4.x/5.x — duplicates and statistics may be at top-level or nested under "report"
            duplicates = data.get("duplicates", data.get("report", {}).get("duplicates", []))
            statistics = data.get("statistics", data.get("report", {}).get("statistics", {}))

            # jscpd 5.x: statistics.total is a dict with {lines, duplicatedLines, ...}
            if isinstance(statistics, dict):
                total = statistics.get("total", {})
                if isinstance(total, dict):
                    total_lines = total.get("lines", 0)
                    dup_lines_stat = total.get("duplicatedLines", 0)
                    dup_percentage = total.get("percentageDuplicatedLines", 0)
                else:
                    total_lines = total
                    dup_lines_stat = 0
                    dup_percentage = 0
            else:
                total_lines = 0
                dup_lines_stat = 0
                dup_percentage = 0

            dup_blocks = len(duplicates) if isinstance(duplicates, list) else 0
            dup_lines = sum(d.get("lines", 0) for d in duplicates) if isinstance(duplicates, list) else 0
            if dup_percentage:
                dup_ratio = dup_percentage
            else:
                dup_ratio = (dup_lines / total_lines * 100) if total_lines and isinstance(total_lines, (int, float)) and total_lines > 0 else 0

            details = []
            if isinstance(duplicates, list):
                for d in sorted(duplicates, key=lambda x: x.get("lines", 0) if isinstance(x.get("lines", 0), (int, float)) else 0, reverse=True)[:200]:
                    # jscpd 5.x: firstFile/secondFile are dicts with "name" field
                    file_a = d.get("firstFile", "")
                    file_b = d.get("secondFile", "")
                    if isinstance(file_a, dict):
                        file_a = file_a.get("name", "")
                    if isinstance(file_b, dict):
                        file_b = file_b.get("name", "")
                    details.append({
                        "file_a": file_a,
                        "file_b": file_b,
                        "lines": d.get("lines", 0),
                        "fragment": (d.get("fragment", "") or "")[:500],
                    })

            return {
                "summary": {
                    "dup_blocks": dup_blocks,
                    "dup_lines": dup_lines,
                    "dup_ratio": round(dup_ratio, 2),
                },
                "details": details,
            }
        except FileNotFoundError:
            logger.warning("jscpd not installed, skipping duplication metrics")
            return None
        except Exception as e:
            logger.warning(f"jscpd failed: {e}")
            return None

    async def _count_tech_debt(self, repo_path: str) -> dict | None:
        """统计 TODO/FIXME/HACK 注释"""
        try:
            counts = {"todo_count": 0, "fixme_count": 0, "hack_count": 0}
            for pattern, key in [("TODO", "todo_count"), ("FIXME", "fixme_count"), ("HACK", "hack_count")]:
                proc = await asyncio.create_subprocess_exec(
                    "grep", "-rn", "--include=*.py", "--include=*.cpp", "--include=*.h",
                    "--include=*.hpp", "--include=*.cc", "--include=*.cmake",
                    "--include=CMakeLists.txt", "--include=*.sh",
                    pattern, repo_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
                counts[key] = len(stdout.decode().strip().split("\n")) if stdout.decode().strip() else 0
            return counts
        except Exception as e:
            logger.warning(f"Tech debt count failed: {e}")
            return None

    async def _count_module_loc(self, repo_path: str) -> dict:
        """统计各模块代码行数"""
        modules = {}
        for module in ["vllm_ascend", "csrc", "tests", "benchmarks", "tools", "docs"]:
            module_path = os.path.join(repo_path, module)
            if not os.path.exists(module_path):
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    "find", module_path, "-name", "*.py", "-o", "-name", "*.cpp", "-o", "-name", "*.h",
                    "-o", "-name", "*.hpp", "-o", "-name", "*.cc",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                files = stdout.decode().strip().split("\n") if stdout.decode().strip() else []
                total = 0
                for f in files:
                    if f and os.path.exists(f):
                        try:
                            with open(f, errors="ignore") as fh:
                                total += sum(1 for _ in fh)
                        except Exception:
                            pass
                modules[module] = total
            except Exception:
                modules[module] = 0
        return modules

    def _extract_language_loc(self, cloc_data: dict | None) -> dict:
        """从 cloc 数据提取语言分布。

        优先使用 cloc 返回的完整 lang_loc（包含所有语言），
        确保语言分布求和等于 total_loc。回退到 5 种主要语言。
        """
        if not cloc_data:
            return {}
        lang_loc = cloc_data.get("lang_loc")
        if lang_loc and isinstance(lang_loc, dict):
            return dict(lang_loc)
        return {
            "Python": cloc_data.get("loc_python", 0),
            "C++": cloc_data.get("loc_cpp", 0),
            "C": cloc_data.get("loc_c", 0),
            "CMake": cloc_data.get("loc_cmake", 0),
            "Shell": cloc_data.get("loc_shell", 0),
        }

    async def _save_snapshot(self, data: dict, branch: str) -> int:
        """保存采集结果到数据库"""
        snapshot_date = date.today().isoformat()
        repo = "vllm-ascend"

        # 幂等：先删旧数据
        old = await self.db.execute(
            select(CodeMetricsSnapshot).where(
                CodeMetricsSnapshot.snapshot_date == snapshot_date,
                CodeMetricsSnapshot.repo == repo,
                CodeMetricsSnapshot.branch == branch,
            )
        )
        for old_snap in old.scalars().all():
            await self.db.execute(delete(CodeComplexityDetail).where(CodeComplexityDetail.snapshot_id == old_snap.id))
            await self.db.execute(delete(CodeDuplicationDetail).where(CodeDuplicationDetail.snapshot_id == old_snap.id))
            await self.db.execute(delete(CodeSecurityDetail).where(CodeSecurityDetail.snapshot_id == old_snap.id))
            await self.db.delete(old_snap)
        await self.db.flush()

        # 创建快照
        snapshot = CodeMetricsSnapshot(
            repo=repo, branch=branch, snapshot_date=snapshot_date,
            collection_status=data.get("collection_status", "partial"),
            collection_duration_seconds=0,
            total_loc=data.get("total_loc", 0),
            total_raw_lines=data.get("total_raw_lines", 0),
            total_functions=data.get("total_functions", 0),
            total_files=data.get("total_files", 0),
            loc_python=data.get("loc_python", 0),
            loc_cpp=data.get("loc_cpp", 0),
            loc_c=data.get("loc_c", 0),
            loc_cmake=data.get("loc_cmake", 0),
            loc_shell=data.get("loc_shell", 0),
            cc_total=data.get("cc_total", 0),
            cc_per_method=data.get("cc_per_method", 0),
            cc_maximum=data.get("cc_maximum", 0),
            cc_huge_count=data.get("cc_huge_count", 0),
            cc_huge_ratio=data.get("cc_huge_ratio", 0),
            cc_adequacy=data.get("cc_adequacy", 0),
            max_depth=data.get("max_depth", 0),
            depth_huge_count=data.get("depth_huge_count", 0),
            depth_huge_ratio=data.get("depth_huge_ratio", 0),
            method_lines_total=data.get("method_lines_total", 0),
            lines_per_method=data.get("lines_per_method", 0),
            huge_method_count=data.get("huge_method_count", 0),
            huge_method_ratio=data.get("huge_method_ratio", 0),
            dup_blocks=data.get("dup_blocks", 0),
            dup_lines=data.get("dup_lines", 0),
            dup_ratio=data.get("dup_ratio", 0),
            todo_count=data.get("todo_count", 0),
            fixme_count=data.get("fixme_count", 0),
            hack_count=data.get("hack_count", 0),
            health_score=data.get("health_score", 0),
            health_score_complexity=data.get("health_score_complexity", 0),
            health_score_security=data.get("health_score_security", 0),
            health_score_duplication=data.get("health_score_duplication", 0),
            health_score_method_size=data.get("health_score_method_size", 0),
            health_score_tech_debt=data.get("health_score_tech_debt", 0),
            health_score_lint=data.get("health_score_lint", 0),
            module_loc=data.get("module_loc"),
            language_loc=data.get("language_loc"),
        )
        self.db.add(snapshot)
        await self.db.flush()

        # 明细
        db_add_all_items = []
        for item in data.get("complexity_details", [])[:500]:
            db_add_all_items.append(CodeComplexityDetail(
                snapshot_id=snapshot.id,
                file_path=item.get("file_path", ""),
                function_name=item.get("function_name", ""),
                language=item.get("language"),
                cyclomatic_complexity=item.get("cyclomatic_complexity"),
                max_nesting_depth=item.get("max_nesting_depth"),
                function_lines=item.get("function_lines"),
                start_line=item.get("start_line"),
            ))
        for item in data.get("duplication_details", [])[:200]:
            db_add_all_items.append(CodeDuplicationDetail(
                snapshot_id=snapshot.id,
                file_a=item.get("file_a", ""),
                file_b=item.get("file_b", ""),
                lines=item.get("lines", 0),
                fragment=item.get("fragment"),
            ))
        self.db.add_all(db_add_all_items)
        await self.db.commit()
        return snapshot.id
