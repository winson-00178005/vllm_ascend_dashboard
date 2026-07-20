"""Test fixtures for PR Pipeline and Test Board bug fix tests."""
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

backend_dir = str(Path(__file__).resolve().parent.parent)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.models import Base, CIResult, PullRequest, WorkflowConfig  # noqa: E402
from app.models.test_board import TestCase, TestRun, TestSuiteSnapshot  # noqa: E402


@pytest_asyncio.fixture
async def db_session():
    """Create test database with all required tables (MySQL)."""
    # Use test database on the same MySQL server
    test_db_url = "mysql+aiomysql://dashboard:dashboard123@localhost:3306/vllm_dashboard_test"
    engine = create_async_engine(
        test_db_url,
        echo=False,
    )
    # Create test database if it doesn't exist
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: c.execute(text("CREATE DATABASE IF NOT EXISTS vllm_dashboard_test")))
    except Exception:
        pass
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[
                    PullRequest.__table__,
                    CIResult.__table__,
                    WorkflowConfig.__table__,
                    TestCase.__table__,
                    TestRun.__table__,
                    TestSuiteSnapshot.__table__,
                ]
            )
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


# ---------------------------------------------------------------------------
# PR Pipeline test helpers
# ---------------------------------------------------------------------------

def make_pr(
    pr_number: int,
    owner: str = "vllm-ascend",
    repo: str = "vllm-ascend",
    state: str = "open",
    is_draft: bool = False,
    created_at: datetime | None = None,
    merged_at: datetime | None = None,
    closed_at: datetime | None = None,
    head_sha: str | None = None,
    ci_status: str | None = None,
    pipeline_stage: str | None = None,
    review_status: str | None = None,
    ci_started_at: datetime | None = None,
    ci_completed_at: datetime | None = None,
    author: str = "testuser",
) -> PullRequest:
    """Helper to create a PullRequest instance for testing."""
    now = datetime.now(UTC)
    return PullRequest(
        pr_number=pr_number,
        owner=owner,
        repo=repo,
        title=f"PR #{pr_number}",
        author=author,
        state=state,
        is_draft=is_draft,
        head_sha=head_sha,
        ci_status=ci_status,
        pipeline_stage=pipeline_stage,
        review_status=review_status,
        created_at=created_at or now,
        merged_at=merged_at,
        closed_at=closed_at,
        ci_started_at=ci_started_at,
        ci_completed_at=ci_completed_at,
        updated_at=now,
    )


def make_ci_result(
    run_id: int,
    head_sha: str,
    status: str = "completed",
    conclusion: str | None = "success",
    event: str = "pull_request",
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> CIResult:
    """Helper to create a CIResult instance for testing."""
    now = datetime.now(UTC)
    return CIResult(
        workflow_name="test-workflow",
        run_id=run_id,
        run_number=1,
        status=status,
        conclusion=conclusion,
        event=event,
        branch="main",
        head_sha=head_sha,
        started_at=started_at or now,
        completed_at=completed_at or now,
        duration_seconds=60,
        hardware="A2",
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Test Board test helpers
# ---------------------------------------------------------------------------

def make_test_case(
    test_name: str = "test_example",
    test_suite: str = "Nightly-A2",
    hardware: str = "A2",
    test_type: str = "e2e",
    owner: str | None = None,
    is_flaky: bool = False,
    flaky_rate: float = 0.0,
    pass_rate_7d: float | None = None,
    health_score: float | None = None,
    health_level: str | None = None,
    avg_duration_seconds: float | None = None,
    last_result: str = "passed",
    module_name: str | None = None,
) -> TestCase:
    now = datetime.now(UTC)
    return TestCase(
        test_name=test_name,
        test_suite=test_suite,
        test_type=test_type,
        hardware=hardware,
        owner=owner,
        is_flaky=is_flaky,
        flaky_rate=flaky_rate,
        pass_rate_7d=pass_rate_7d,
        health_score=health_score,
        health_level=health_level,
        avg_duration_seconds=avg_duration_seconds,
        last_result=last_result,
        module_name=module_name,
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )


def make_test_run(
    test_case_id: int,
    result: str = "passed",
    duration_seconds: float | None = 60.0,
    head_sha: str = "abc123",
    failure_category: str | None = None,
    started_at: datetime | None = None,
    workflow_name: str = "Nightly-A2",
    job_name: str = "Run Pytest",
) -> TestRun:
    now = started_at or datetime.now(UTC)
    return TestRun(
        test_case_id=test_case_id,
        result=result,
        duration_seconds=duration_seconds,
        head_sha=head_sha,
        failure_category=failure_category if result == "failed" else None,
        workflow_name=workflow_name,
        job_name=job_name,
        started_at=now,
        completed_at=now,
        created_at=now,
    )
