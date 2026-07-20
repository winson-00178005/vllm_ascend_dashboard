import csv
import io
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, CurrentSuperAdminUser, DbSession
from app.db.base import SessionLocal
from app.models import User
from app.models.test_board import TestCase
from app.schemas import Message
from app.schemas.test_board import (
    TestOverviewResponse, TestCaseResponse, TestRunResponse,
    TestSuiteResponse, FlakyCaseDetail, FailureCategoryBreakdown,
    OwnerMatrixItem, ModuleHealthItem, TestBoardSyncRequest,
    FailureAnnotationRequest, TestCaseUpdateRequest,
)
from app.services.test_board_service import TestBoardService
from app.services.test_health_calculator import TestHealthCalculator
from app.services.github_client import GitHubClient
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/test-board", tags=["Test Board"])


async def get_db():
    async with SessionLocal() as session:
        yield session


def get_github():
    return GitHubClient(token=settings.GITHUB_TOKEN, owner=settings.GITHUB_OWNER, repo=settings.GITHUB_REPO) if settings.GITHUB_TOKEN else None


@router.get("/overview", response_model=TestOverviewResponse)
async def get_overview(days: int = Query(7, ge=1, le=90), db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    svc = TestBoardService(db)
    data = await svc.get_overview(days=days)
    return data


@router.get("/suites")
async def get_suites(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    svc = TestBoardService(db)
    return await svc.get_suites()


@router.get("/filter-options")
async def get_filter_options(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    from sqlalchemy import select, distinct
    from app.models.test_board import TestCase
    test_types = (await db.execute(select(distinct(TestCase.test_type)).where(TestCase.test_type.isnot(None)))).all()
    suites = (await db.execute(select(distinct(TestCase.test_suite)).where(TestCase.test_suite.isnot(None)))).all()
    hardwares = (await db.execute(select(distinct(TestCase.hardware)).where(TestCase.hardware.isnot(None)))).all()
    return {
        "test_types": [r[0] for r in test_types],
        "suites": [r[0] for r in suites],
        "hardwares": [r[0] for r in hardwares],
    }


@router.get("/cases")
async def get_cases(
    test_type: str | None = None, suite_name: str | None = None, module_name: str | None = None,
    hardware: str | None = None, result: str | None = None, health_level: str | None = None,
    is_flaky: bool | None = None, owner: str | None = None,
    sort: str = "health_score", order: str = "desc",
    page: int = 1, per_page: int = 20,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    filters = {"test_type": test_type, "test_suite": suite_name, "module_name": module_name,
               "hardware": hardware, "result": result, "health_level": health_level,
               "is_flaky": is_flaky, "owner": owner, "sort": sort, "order": order}
    filters = {k: v for k, v in filters.items() if v is not None}
    svc = TestBoardService(db)
    data = await svc.get_cases(filters=filters, page=page, per_page=per_page)
    return data


@router.get("/cases/{case_id}")
async def get_case_detail(case_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    svc = TestBoardService(db)
    data = await svc.get_case_detail(case_id)
    if not data:
        return {"error": "Case not found"}
    return data


@router.get("/runs")
async def get_runs(
    test_case_id: int | None = None, result: str | None = None,
    days: int = 30, page: int = 1, per_page: int = 20,
    format: str | None = None,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    from sqlalchemy import select, and_, desc, func
    from app.models.test_board import TestRun
    from datetime import timedelta
    cutoff = datetime.now(UTC) - timedelta(days=days)
    stmt = select(TestRun).where(TestRun.started_at >= cutoff)
    if test_case_id:
        stmt = stmt.where(TestRun.test_case_id == test_case_id)
    if result:
        stmt = stmt.where(TestRun.result == result)
    stmt = stmt.order_by(desc(TestRun.started_at))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    items = list((await db.execute(stmt)).scalars().all())
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "test_case_id", "result", "duration_seconds", "failure_category", "head_sha", "started_at"])
        for item in items:
            writer.writerow([item.id, item.test_case_id, item.result, item.duration_seconds, item.failure_category, item.head_sha, item.started_at])
        return Response(content=output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=test_runs.csv"})
    return {"total": total, "items": items, "page": page, "page_size": per_page}


@router.get("/flaky")
async def get_flaky(
    min_flip_rate: float = 0.01, days: int = 30,
    suite_name: str | None = None, module_name: str | None = None,
    sort: str = "flip_rate", order: str = "desc",
    page: int = 1, per_page: int = 20,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
):
    filters = {"suite_name": suite_name, "module_name": module_name, "sort": sort, "order": order}
    filters = {k: v for k, v in filters.items() if v is not None}
    svc = TestBoardService(db)
    return await svc.get_flaky_cases(min_flip_rate=min_flip_rate, days=days, filters=filters, page=page, per_page=per_page)


@router.get("/failures")
async def get_failures(days: int = 30, category: str | None = None, suite_name: str | None = None, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    svc = TestBoardService(db)
    return await svc.get_failure_breakdown(days=days, category=category, suite_name=suite_name)


@router.get("/duration")
async def get_duration(days: int = 30, suite_name: str | None = None, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    svc = TestBoardService(db)
    return await svc.get_duration_analysis(days=days, suite_name=suite_name)


@router.get("/owners")
async def get_owners(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    svc = TestBoardService(db)
    return await svc.get_owner_matrix()


@router.get("/modules")
async def get_modules(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    svc = TestBoardService(db)
    return await svc.get_module_health()


@router.get("/trends")
async def get_trends(days: int = 30, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    from sqlalchemy import select
    from app.models.test_board import TestSuiteSnapshot
    from datetime import timedelta
    stmt = select(TestSuiteSnapshot).where(TestSuiteSnapshot.snapshot_date >= (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")).order_by(TestSuiteSnapshot.snapshot_date)
    snapshots = list((await db.execute(stmt)).scalars().all())
    health_trend = [{"date": s.snapshot_date, "score": s.health_score, "level": s.health_level} for s in snapshots]
    pass_rate_trend = [{"date": s.snapshot_date, "rate": s.pass_rate} for s in snapshots]
    return {"health_trend": health_trend, "pass_rate_trend": pass_rate_trend}


@router.post("/sync")
async def trigger_sync(request: TestBoardSyncRequest, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role not in ("admin", "super_admin"):
        return {"error": "Admin access required"}
    gh = get_github()
    svc = TestBoardService(db, gh)
    count = await svc.parse_ci_results(days_back=request.days_back, force=request.force)
    # CI 同步后自动跑一次发现问题数推导，保持 auto_issues_found 最新
    from app.services.issues_found_derivator import IssuesFoundDerivator
    derivator = IssuesFoundDerivator(db)
    derivation = await derivator.derive_all()
    return {"success": True, "message": f"Parsed {count} test results", "count": count, "derivation": derivation}


@router.post("/annotate")
async def annotate_failure(request: FailureAnnotationRequest, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.test_board import FailureAnnotation
    annotation = FailureAnnotation(
        test_run_id=request.test_run_id, annotated_category=request.annotated_category,
        annotated_by=request.annotated_by, annotation_source="manual",
    )
    db.add(annotation)
    await db.commit()
    return {"success": True, "message": "Annotation saved"}


@router.patch("/cases/{case_id}", response_model=TestCaseResponse)
async def update_case(
    case_id: int,
    request: TestCaseUpdateRequest,
    db: DbSession,
    user: CurrentSuperAdminUser,
):
    """超级管理员维护测试用例元数据。

    可维护字段：发现问题数、疑似用例问题次数、Flaky 标记（含人工锁定）、负责人。
    所有变更写入审计日志（app_logs），记录操作人、用例 ID 与变更前后值。
    """
    case = (await db.execute(select(TestCase).where(TestCase.id == case_id))).scalar_one_or_none()
    if not case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="测试用例不存在")

    # 记录变更前后值，用于审计
    changes: dict[str, tuple] = {}

    def _record(field: str, new_val):
        old_val = getattr(case, field)
        if new_val != old_val:
            changes[field] = (old_val, new_val)

    if request.issues_found is not None:
        _record("issues_found", request.issues_found)
        case.issues_found = request.issues_found
        case.issues_found_override = True
    if request.suspected_test_issue_count is not None:
        _record("suspected_test_issue_count", request.suspected_test_issue_count)
        case.suspected_test_issue_count = request.suspected_test_issue_count
        case.issues_found_override = True
    if request.is_flaky_manual is not None:
        _record("is_flaky_manual", request.is_flaky_manual)
        case.is_flaky_manual = request.is_flaky_manual
    if request.is_flaky is not None:
        _record("is_flaky", request.is_flaky)
        case.is_flaky = request.is_flaky
        # 仅当显式标记为 Flaky 且未指定锁定时，默认锁定为人工维护；
        # 标记为稳定（False）不自动锁定，保留自动检测继续观察
        if request.is_flaky_manual is None and request.is_flaky is True:
            _record("is_flaky_manual", True)
            case.is_flaky_manual = True
    if request.owner is not None:
        new_owner = request.owner or None
        _record("owner", new_owner)
        case.owner = new_owner
    if request.owner_email is not None:
        new_email = request.owner_email or None
        _record("owner_email", new_email)
        case.owner_email = new_email
    if request.use_auto_issues is True:
        _record("issues_found_override", False)
        case.issues_found_override = False
        case.issues_found = 0
        case.suspected_test_issue_count = 0

    await db.commit()
    await db.refresh(case)

    # 审计日志：谁、何时、改了什么（持久化到 app_logs，满足 requirements §10.3）
    if changes:
        logger.info(
            "test_case_metadata_updated: user=%s case_id=%s changes=%s",
            user.username, case_id,
            {k: {"from": v[0], "to": v[1]} for k, v in changes.items()},
        )
    return case


@router.post("/derive-issues")
async def trigger_derive_issues(
    case_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """触发"发现问题数"自动推导。

    - 不传 case_id：全量推导所有用例
    - 传 case_id：仅推导指定用例
    需 admin/super_admin 权限。
    """
    if user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    from app.services.issues_found_derivator import IssuesFoundDerivator
    derivator = IssuesFoundDerivator(db)
    if case_id:
        result = await derivator.derive_single(case_id)
        if not result:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用例不存在")
    else:
        result = await derivator.derive_all()
    return {"success": True, "result": result}
