"""Issues found derivator.

从 CI 构建流水线记录和社区 PR 数据自动推导每个测试用例的"发现问题数"。

判定规则（与用户确认的方案一致）：
  auto_issues_found
    = 该用例历史失败执行（TestRun.result='failed'）中，能关联到 BugFix PR 的去重数量
    关联方式 1（主信号）：TestRun.head_sha == PullRequest.head_sha（PR 触发的 CI 运行）
    关联方式 2（辅信号）：夜间 schedule 运行失败后 N 天内合入的 BugFix PR（按模块/关键词弱关联）
  auto_suspected_test_issue_count
    = 该用例失败执行中，关联到 Test PR（[Test]/[TEST] 前缀）的去重数量
    说明：在测试 PR 上失败，通常是用例自身问题而非产品 bug

BugFix PR 识别：title 以 [BugFix]/[Bugfix]/[bugfix] 开头（vllm-ascend 社区约定）
Test   PR 识别：title 以 [Test]/[TEST]/[test] 开头

人工覆盖：当 TestCase.issues_found_override=True 时，不更新 auto_* 字段（人工值优先）
"""
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PullRequest
from app.models.test_board import TestCase, TestRun

logger = logging.getLogger(__name__)

# BugFix PR 标题正则（vllm-ascend 社区约定：[BugFix]/[Bugfix]/[bugfix]）
BUGFIX_TITLE_RE = re.compile(r"^\s*\[bugfix\]", re.IGNORECASE)
# Test PR 标题正则（[Test]/[TEST]/[test]）
TEST_TITLE_RE = re.compile(r"^\s*\[test\]", re.IGNORECASE)
# schedule 运行失败后，N 天内合入的 BugFix PR 视为弱关联
WEAK_LINK_WINDOW_DAYS = 7


class IssuesFoundDerivator:
    __test__ = False  # Production service, not a pytest test class.

    def __init__(self, db: AsyncSession):
        self.db = db

    async def derive_all(self) -> dict[str, int]:
        """为所有测试用例推导 auto_issues_found / auto_suspected_test_issue_count。

        Returns:
            统计字典：{updated, issues_total, suspected_total, skipped_override}
        """
        # 1. 加载所有 BugFix / Test PR，构建 head_sha -> 类别 映射
        pr_index = await self._build_pr_index()

        # 2. 加载所有 BugFix PR 的合入时间，用于 schedule 运行的弱关联
        bugfix_merged_at = await self._load_bugfix_merged_timeline()

        # 3. 加载所有测试用例
        cases = list((await self.db.execute(select(TestCase))).scalars().all())
        updated = 0
        skipped_override = 0
        issues_total = 0
        suspected_total = 0

        for case in cases:
            # 人工覆盖标记为 True 时，跳过自动推导（人工值优先）
            if case.issues_found_override:
                skipped_override += 1
                continue

            # 4. 查询该用例所有失败执行
            failed_runs = await self._get_failed_runs(case.id)
            if not failed_runs:
                case.auto_issues_found = 0
                case.auto_suspected_test_issue_count = 0
                continue

            # 5. 主信号：head_sha 直接匹配
            linked_bugfix_shas: set[str] = set()
            linked_test_shas: set[str] = set()
            schedule_failures: list[datetime] = []

            for run in failed_runs:
                sha = (run.head_sha or "").strip()
                event = (run.event or "").strip()
                if sha and sha in pr_index:
                    categories = pr_index[sha]
                    if "bugfix" in categories:
                        linked_bugfix_shas.add(sha)
                    if "test" in categories:
                        linked_test_shas.add(sha)
                # 收集 schedule/workflow_dispatch/push 运行的失败时间，用于弱关联
                if event in ("schedule", "workflow_dispatch", "push") and run.started_at:
                    schedule_failures.append(run.started_at)

            # 6. 辅信号：schedule 运行失败后 N 天内合入的 BugFix PR（按模块关键词弱关联）
            if schedule_failures and bugfix_merged_at:
                weak_count = self._weak_link_by_module(
                    case, schedule_failures, bugfix_merged_at
                )
                # 弱关联仅作为补充，不与主信号去重（不同 PR）
                linked_bugfix_shas.update(weak_count)

            case.auto_issues_found = len(linked_bugfix_shas)
            case.auto_suspected_test_issue_count = len(linked_test_shas)
            issues_total += case.auto_issues_found
            suspected_total += case.auto_suspected_test_issue_count
            updated += 1

        await self.db.commit()
        logger.info(
            "issues_found_derivation_done: updated=%d skipped_override=%d issues_total=%d suspected_total=%d",
            updated, skipped_override, issues_total, suspected_total,
        )
        return {
            "updated": updated,
            "skipped_override": skipped_override,
            "issues_total": issues_total,
            "suspected_total": suspected_total,
        }

    async def derive_single(self, case_id: int) -> dict[str, Any] | None:
        """推导单个用例（用于按需触发）。"""
        case = (
            await self.db.execute(select(TestCase).where(TestCase.id == case_id))
        ).scalar_one_or_none()
        if not case:
            return None
        if case.issues_found_override:
            return {
                "case_id": case_id,
                "skipped": "issues_found_override=True（人工值优先）",
            }

        pr_index = await self._build_pr_index()
        bugfix_merged_at = await self._load_bugfix_merged_timeline()
        failed_runs = await self._get_failed_runs(case_id)

        linked_bugfix: set[str] = set()
        linked_test: set[str] = set()
        schedule_failures: list[datetime] = []
        for run in failed_runs or []:
            sha = (run.head_sha or "").strip()
            event = (run.event or "").strip()
            if sha and sha in pr_index:
                cats = pr_index[sha]
                if "bugfix" in cats:
                    linked_bugfix.add(sha)
                if "test" in cats:
                    linked_test.add(sha)
            if event in ("schedule", "workflow_dispatch", "push") and run.started_at:
                schedule_failures.append(run.started_at)

        if schedule_failures and bugfix_merged_at:
            linked_bugfix.update(
                self._weak_link_by_module(case, schedule_failures, bugfix_merged_at)
            )

        case.auto_issues_found = len(linked_bugfix)
        case.auto_suspected_test_issue_count = len(linked_test)
        await self.db.commit()
        return {
            "case_id": case_id,
            "auto_issues_found": case.auto_issues_found,
            "auto_suspected_test_issue_count": case.auto_suspected_test_issue_count,
            "linked_bugfix_shas": sorted(linked_bugfix),
            "linked_test_shas": sorted(linked_test),
        }

    async def _build_pr_index(self) -> dict[str, set[str]]:
        """构建 head_sha -> {类别集合} 映射。

        类别：'bugfix'（BugFix PR）或 'test'（Test PR）。
        """
        stmt = select(PullRequest.head_sha, PullRequest.title, PullRequest.data).where(
            PullRequest.head_sha.isnot(None),
            PullRequest.head_sha != "",
        )
        rows = (await self.db.execute(stmt)).all()
        index: dict[str, set[str]] = {}
        for sha, title, data_json in rows:
            if not sha:
                continue
            sha = sha.strip()
            cats: set[str] = set()
            if title and BUGFIX_TITLE_RE.match(title):
                cats.add("bugfix")
            if title and TEST_TITLE_RE.match(title):
                cats.add("test")
            # 补充：PR body 中明确提到 fix bug 的，也视为 bugfix（次级信号）
            if "bugfix" not in cats and data_json:
                body = self._extract_body(data_json)
                if body and self._body_indicates_bugfix(body):
                    cats.add("bugfix")
            if cats:
                index.setdefault(sha, set()).update(cats)
        return index

    async def _load_bugfix_merged_timeline(self) -> list[tuple[datetime, str, str]]:
        """加载所有已合入 BugFix PR 的 (merged_at, title, head_sha)，用于弱关联。"""
        stmt = (
            select(PullRequest.merged_at, PullRequest.title, PullRequest.head_sha)
            .where(
                PullRequest.merged_at.isnot(None),
                PullRequest.head_sha.isnot(None),
            )
        )
        rows = (await self.db.execute(stmt)).all()
        timeline: list[tuple[datetime, str, str]] = []
        for merged_at, title, sha in rows:
            if merged_at and title and BUGFIX_TITLE_RE.match(title):
                timeline.append((merged_at, title, sha or ""))
        timeline.sort(key=lambda x: x[0])
        return timeline

    async def _get_failed_runs(self, case_id: int) -> list[TestRun]:
        stmt = (
            select(TestRun)
            .where(TestRun.test_case_id == case_id, TestRun.result == "failed")
            .order_by(TestRun.started_at.desc())
        )
        return list((await self.db.execute(stmt)).scalars().all())

    def _weak_link_by_module(
        self,
        case: TestCase,
        schedule_failures: list[datetime],
        bugfix_timeline: list[tuple[datetime, str, str]],
    ) -> set[str]:
        """对 schedule/workflow_dispatch 运行失败，弱关联时间窗口内的 BugFix PR。

        策略：
          1. 按模块/模型关键词匹配（失败后 WEAK_LINK_WINDOW_DAYS 天内合入的 BugFix PR）
          2. 若无匹配，回退到纯时间窗口（失败前 24h 内合入的 BugFix PR，
             代表测试捕捉到了近期合入代码引入的回归）
        """
        module_kw = (case.module_name or "").lower()
        model_kw = self._extract_model_keyword(case.test_name)

        linked: set[str] = set()
        for fail_at in schedule_failures:
            window_end = fail_at + timedelta(days=WEAK_LINK_WINDOW_DAYS)
            for merged_at, title, sha in bugfix_timeline:
                if fail_at <= merged_at <= window_end and sha:
                    title_lower = title.lower()
                    if (module_kw and module_kw in title_lower) or (
                        model_kw and model_kw in title_lower
                    ):
                        linked.add(sha)

        # 回退：纯时间窗口（失败前 24h 内合入的 BugFix PR，代表测试捕捉到近期合入代码的回归）
        if not linked:
            for fail_at in schedule_failures:
                window_start = fail_at - timedelta(hours=24)
                for merged_at, title, sha in bugfix_timeline:
                    if window_start <= merged_at <= fail_at and sha:
                        linked.add(sha)
        return linked

    @staticmethod
    def _extract_body(data_json: Any) -> str:
        """从 PullRequest.data JSON 中提取 body 字段。"""
        if not data_json:
            return ""
        if isinstance(data_json, dict):
            return data_json.get("body") or ""
        if isinstance(data_json, str):
            try:
                return json.loads(data_json).get("body") or ""
            except (json.JSONDecodeError, TypeError):
                return ""
        return ""

    # body 中指示 bugfix 的关键词模式（需同时包含 fix 和 bug/issue/crash/error）
    _BODY_FIX_RE = re.compile(r"\bfix\b", re.IGNORECASE)
    _BODY_BUG_RE = re.compile(r"\b(bug|issue|crash|error|defect|regression)\b", re.IGNORECASE)

    @classmethod
    def _body_indicates_bugfix(cls, body: str) -> bool:
        """PR body 同时包含 fix 和 bug/issue/crash/error 关键词时，视为 bugfix。"""
        return bool(cls._BODY_FIX_RE.search(body) and cls._BODY_BUG_RE.search(body))

    @staticmethod
    def _extract_model_keyword(test_name: str) -> str:
        """从测试名中提取模型关键词。"""
        name_lower = test_name.lower()
        for model in ("qwen", "deepseek", "llama", "glm", "kimi", "minimax",
                       "gemma", "phi", "internlm", "baichuan", "chatglm"):
            if model in name_lower:
                return model
        return ""
