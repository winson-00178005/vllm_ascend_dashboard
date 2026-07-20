"""Self-tests for IssuesFoundDerivator (auto issues_found derivation).

Covers:
- BugFix PR linked by head_sha → auto_issues_found incremented
- Test PR linked by head_sha → auto_suspected_test_issue_count incremented
- issues_found_override=True → derivation skipped (manual value preserved)
- No failed runs → auto counts stay 0
- Distinct PR count (same PR matched by multiple runs counts once)
- Weak link: schedule failure + BugFix PR merged within window + module keyword
- effective_issues_found in schema: manual override takes priority over auto
- use_auto_issues=True resets override
"""
import os
from datetime import UTC, datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-key-at-least-32-chars-long!!")
os.environ.setdefault("GITHUB_TOKEN", "ghp_test_token")
os.environ.setdefault("GITHUB_OWNER", "vllm-ascend")
os.environ.setdefault("GITHUB_REPO", "vllm-ascend")

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base, PullRequest
from app.models.test_board import TestCase, TestRun
from app.schemas.test_board import TestCaseResponse
from app.services.issues_found_derivator import IssuesFoundDerivator
from tests.conftest import make_test_case, make_test_run


def _make_run(
    test_case_id: int, result: str = "failed", head_sha: str = "abc",
    started_at: datetime | None = None, event: str = "pull_request",
) -> TestRun:
    """Helper: 创建 TestRun 并设置 event 字段（conftest 的 make_test_run 不支持 event）。"""
    run = make_test_run(
        test_case_id=test_case_id, result=result, head_sha=head_sha, started_at=started_at,
    )
    run.event = event
    return run


@pytest.fixture
async def rich_db():
    """In-memory SQLite with test_board + pull_requests tables."""
    from app.models import CIResult, JobOwner
    from app.models.test_board import TestSuiteSnapshot

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[
                    CIResult.__table__, JobOwner.__table__,
                    TestCase.__table__, TestRun.__table__, TestSuiteSnapshot.__table__,
                    PullRequest.__table__,
                ]
            )
        )
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sf() as session:
        yield session
    await engine.dispose()


def _make_pr(
    pr_number: int, title: str, head_sha: str,
    merged_at: datetime | None = None, body: str = "",
) -> PullRequest:
    now = datetime.now(UTC)
    return PullRequest(
        pr_number=pr_number, owner="vllm-ascend", repo="vllm-ascend",
        title=title, author="testuser", state="merged" if merged_at else "open",
        head_sha=head_sha, created_at=now, merged_at=merged_at, updated_at=now,
        data={"body": body, "number": pr_number, "title": title},
    )


class TestBugFixLinkage:
    """BugFix PR 通过 head_sha 关联到失败用例。"""

    @pytest.mark.asyncio
    async def test_bugfix_pr_linked_increments_issues_found(self, rich_db):
        """用例在 BugFix PR 的代码上失败 → auto_issues_found=1。"""
        now = datetime.now(UTC)
        case = make_test_case(test_name="test_qwen3_serving", module_name="serving")
        rich_db.add(case)
        await rich_db.flush()

        rich_db.add(_make_run(test_case_id=case.id, result="failed", head_sha="sha_bugfix_1", started_at=now, event="pull_request"))
        rich_db.add(_make_pr(
            101, "[BugFix] Fix Qwen3 serving crash", "sha_bugfix_1", merged_at=now,
        ))
        await rich_db.commit()

        derivator = IssuesFoundDerivator(rich_db)
        await derivator.derive_all()

        await rich_db.refresh(case)
        assert case.auto_issues_found == 1
        assert case.auto_suspected_test_issue_count == 0

    @pytest.mark.asyncio
    async def test_bugfix_case_insensitive_prefix(self, rich_db):
        """[Bugfix] / [bugfix] 也应识别。"""
        now = datetime.now(UTC)
        case = make_test_case(test_name="test_a")
        rich_db.add(case)
        await rich_db.flush()
        rich_db.add(make_test_run(
            test_case_id=case.id, result="failed", head_sha="sha1", started_at=now,
        ))
        rich_db.add(_make_pr(1, "[Bugfix] Fix A", "sha1", merged_at=now))
        rich_db.add(_make_pr(2, "[bugfix] Fix B", "sha2", merged_at=now))
        await rich_db.commit()

        await IssuesFoundDerivator(rich_db).derive_all()
        await rich_db.refresh(case)
        assert case.auto_issues_found == 1


class TestTestPrLinkage:
    """Test PR 关联到失败用例 → suspected_test_issue_count。"""

    @pytest.mark.asyncio
    async def test_test_pr_linked_increments_suspected(self, rich_db):
        now = datetime.now(UTC)
        case = make_test_case(test_name="test_x")
        rich_db.add(case)
        await rich_db.flush()
        rich_db.add(make_test_run(
            test_case_id=case.id, result="failed", head_sha="sha_test_1", started_at=now,
        ))
        rich_db.add(_make_pr(201, "[Test] Fix test case for X", "sha_test_1", merged_at=now))
        await rich_db.commit()

        await IssuesFoundDerivator(rich_db).derive_all()
        await rich_db.refresh(case)
        assert case.auto_suspected_test_issue_count == 1
        assert case.auto_issues_found == 0


class TestDistinctCount:
    """同一 PR 被多次失败执行关联时，只计 1 次。"""

    @pytest.mark.asyncio
    async def test_same_pr_multiple_runs_counts_once(self, rich_db):
        now = datetime.now(UTC)
        case = make_test_case(test_name="test_distinct")
        rich_db.add(case)
        await rich_db.flush()
        for i in range(3):
            rich_db.add(_make_run(test_case_id=case.id, result="failed", head_sha="same_sha", started_at=now - timedelta(days=i), event="pull_request"))
        rich_db.add(_make_pr(301, "[BugFix] Fix distinct", "same_sha", merged_at=now))
        await rich_db.commit()

        await IssuesFoundDerivator(rich_db).derive_all()
        await rich_db.refresh(case)
        assert case.auto_issues_found == 1


class TestNoFailedRuns:
    """无失败执行的用例，auto 计数保持 0。"""

    @pytest.mark.asyncio
    async def test_no_failures_stays_zero(self, rich_db):
        case = make_test_case(test_name="test_passing", last_result="passed")
        rich_db.add(case)
        await rich_db.flush()
        rich_db.add(make_test_run(
            test_case_id=case.id, result="passed", head_sha="sha_ok", started_at=datetime.now(UTC),
        ))
        await rich_db.commit()

        await IssuesFoundDerivator(rich_db).derive_all()
        await rich_db.refresh(case)
        assert case.auto_issues_found == 0
        assert case.auto_suspected_test_issue_count == 0


class TestManualOverrideProtection:
    """issues_found_override=True 时，自动推导跳过该用例。"""

    @pytest.mark.asyncio
    async def test_override_skips_derivation(self, rich_db):
        now = datetime.now(UTC)
        case = make_test_case(test_name="test_override")
        case.issues_found = 5
        case.issues_found_override = True
        rich_db.add(case)
        await rich_db.flush()
        rich_db.add(make_test_run(
            test_case_id=case.id, result="failed", head_sha="sha_ov", started_at=now,
        ))
        rich_db.add(_make_pr(401, "[BugFix] Fix override", "sha_ov", merged_at=now))
        await rich_db.commit()

        result = await IssuesFoundDerivator(rich_db).derive_all()
        await rich_db.refresh(case)
        assert result["skipped_override"] == 1
        # 人工值不被覆盖
        assert case.issues_found == 5
        # auto 字段不更新
        assert case.auto_issues_found == 0


class TestWeakLinkSchedule:
    """schedule 运行失败 + N 天内合入的 BugFix PR + 模块关键词 → 弱关联。"""

    @pytest.mark.asyncio
    async def test_weak_link_module_keyword(self, rich_db):
        now = datetime.now(UTC)
        case = make_test_case(test_name="test_serving_latency", module_name="serving")
        rich_db.add(case)
        await rich_db.flush()
        # schedule 运行失败
        rich_db.add(_make_run(test_case_id=case.id, result="failed", head_sha="main_sha_1", started_at=now - timedelta(days=3), event="schedule"))
        # 2 天后合入的 BugFix PR，标题含 "serving"
        rich_db.add(_make_pr(
            501, "[BugFix] Fix serving latency regression", "fix_sha_1",
            merged_at=now - timedelta(days=1),
        ))
        await rich_db.commit()

        await IssuesFoundDerivator(rich_db).derive_all()
        await rich_db.refresh(case)
        assert case.auto_issues_found >= 1, "弱关联应识别到 BugFix PR"

    @pytest.mark.asyncio
    async def test_weak_link_outside_window_not_counted(self, rich_db):
        """超过 N 天窗口的 BugFix PR 不计入弱关联。"""
        now = datetime.now(UTC)
        case = make_test_case(test_name="test_attention", module_name="attention")
        rich_db.add(case)
        await rich_db.flush()
        rich_db.add(_make_run(test_case_id=case.id, result="failed", head_sha="main_sha_2", started_at=now - timedelta(days=30), event="schedule"))
        # 20 天后合入（超出 7 天窗口）
        rich_db.add(_make_pr(
            502, "[BugFix] Fix attention bug", "fix_sha_2",
            merged_at=now - timedelta(days=10),
        ))
        await rich_db.commit()

        await IssuesFoundDerivator(rich_db).derive_all()
        await rich_db.refresh(case)
        assert case.auto_issues_found == 0


class TestBodyFallback:
    """PR body 同时包含 fix + bug 关键词时，识别为 bugfix（无 [BugFix] 前缀）。"""

    @pytest.mark.asyncio
    async def test_body_fix_bug_keywords(self, rich_db):
        now = datetime.now(UTC)
        case = make_test_case(test_name="test_body_fb")
        rich_db.add(case)
        await rich_db.flush()
        rich_db.add(make_test_run(
            test_case_id=case.id, result="failed", head_sha="sha_body", started_at=now,
        ))
        rich_db.add(_make_pr(
            601, "Update kernel for performance", "sha_body", merged_at=now,
            body="This fix addresses a bug in the attention kernel causing crash.",
        ))
        await rich_db.commit()

        await IssuesFoundDerivator(rich_db).derive_all()
        await rich_db.refresh(case)
        assert case.auto_issues_found == 1


class TestSchemaEffectiveValue:
    """TestCaseResponse.effective_issues_found 优先人工值，回退自动值。"""

    @pytest.mark.asyncio
    async def test_effective_uses_auto_when_no_override(self, rich_db):
        case = make_test_case(test_name="test_eff_auto")
        case.auto_issues_found = 3
        case.issues_found_override = False
        rich_db.add(case)
        await rich_db.commit()

        resp = TestCaseResponse.model_validate(case)
        assert resp.effective_issues_found == 3, "无人工覆盖时应使用 auto 值"

    @pytest.mark.asyncio
    async def test_effective_uses_manual_when_override(self, rich_db):
        case = make_test_case(test_name="test_eff_manual")
        case.issues_found = 7
        case.auto_issues_found = 3
        case.issues_found_override = True
        rich_db.add(case)
        await rich_db.commit()

        resp = TestCaseResponse.model_validate(case)
        assert resp.effective_issues_found == 7, "有人工覆盖时应使用人工值"


class TestDeriveSingle:
    """derive_single 按需推导单个用例。"""

    @pytest.mark.asyncio
    async def test_derive_single_returns_result(self, rich_db):
        now = datetime.now(UTC)
        case = make_test_case(test_name="test_single")
        rich_db.add(case)
        await rich_db.flush()
        rich_db.add(make_test_run(
            test_case_id=case.id, result="failed", head_sha="sha_s", started_at=now,
        ))
        rich_db.add(_make_pr(701, "[BugFix] Single fix", "sha_s", merged_at=now))
        await rich_db.commit()

        result = await IssuesFoundDerivator(rich_db).derive_single(case.id)
        assert result is not None
        assert result["auto_issues_found"] == 1
        assert "sha_s" in result["linked_bugfix_shas"]

    @pytest.mark.asyncio
    async def test_derive_single_nonexistent_returns_none(self, rich_db):
        result = await IssuesFoundDerivator(rich_db).derive_single(99999)
        assert result is None
