import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, func, and_, desc, asc, delete, text, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CIJob, CIResult, JobOwner, JobFailureAnalysis, WorkflowConfig
from app.models.test_board import TestCase, TestRun, TestSuiteSnapshot, FailureAnnotation
from app.services.test_timing_parser import TestTimingParser
from app.services.junit_xml_parser import JUnitXMLParser
from app.services.test_health_calculator import TestHealthCalculator
from app.services.failure_classifier import FailureClassifier
from app.services.github_client import GitHubClient

logger = logging.getLogger(__name__)


class TestBoardService:
    __test__ = False  # Production service, not a pytest test class.

    def __init__(self, db: AsyncSession, github_client: GitHubClient | None = None):
        self.db = db
        self.github = github_client

    async def get_overview(self, days: int = 7) -> dict[str, Any]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        total_stmt = select(func.count(TestCase.id))
        total = (await self.db.execute(total_stmt)).scalar() or 0

        flaky_stmt = select(func.count(TestCase.id)).where(TestCase.is_flaky == True)
        flaky_count = (await self.db.execute(flaky_stmt)).scalar() or 0

        attention_stmt = select(func.count(TestCase.id)).where(
            TestCase.last_result.in_(["failed", "error"])
        )
        attention = (await self.db.execute(attention_stmt)).scalar() or 0

        pass_rate_stmt = select(func.avg(TestCase.pass_rate_7d)).where(TestCase.pass_rate_7d.isnot(None))
        pass_rate_7d = (await self.db.execute(pass_rate_stmt)).scalar() or 0.0

        avg_dur_stmt = select(func.avg(TestCase.avg_duration_seconds)).where(TestCase.avg_duration_seconds.isnot(None))
        avg_dur = (await self.db.execute(avg_dur_stmt)).scalar() or 0.0

        suite_dist_stmt = select(TestCase.test_suite, TestCase.hardware, func.count(TestCase.id)).group_by(TestCase.test_suite, TestCase.hardware)
        suite_dist_rows = (await self.db.execute(suite_dist_stmt)).all()
        suite_distribution = {}
        for row in suite_dist_rows:
            suite_name = row[0]
            hardware = row[1]
            key = suite_name if hardware and hardware in suite_name else f"{suite_name}-{hardware}"
            suite_distribution[key] = row[2]

        result_dist_stmt = select(TestCase.last_result, func.count(TestCase.id)).group_by(TestCase.last_result)
        result_dist_rows = (await self.db.execute(result_dist_stmt)).all()
        result_distribution = {row[0] or "unknown": row[1] for row in result_dist_rows}

        avg_hs_stmt = select(func.avg(TestCase.health_score)).where(TestCase.health_score.isnot(None))
        avg_hs = (await self.db.execute(avg_hs_stmt)).scalar() or 0.0
        hs_level = TestHealthCalculator._score_to_level(avg_hs) if avg_hs else "D"

        health_trend_stmt = select(TestSuiteSnapshot.snapshot_date, func.avg(TestSuiteSnapshot.health_score)).group_by(TestSuiteSnapshot.snapshot_date).order_by(TestSuiteSnapshot.snapshot_date).limit(30)
        health_trend_rows = (await self.db.execute(health_trend_stmt)).all()
        health_trend = [{"date": r[0], "score": round(r[1] or 0, 1), "level": TestHealthCalculator._score_to_level(r[1] or 0)} for r in health_trend_rows]

        pass_rate_trend_stmt = select(TestSuiteSnapshot.snapshot_date, func.avg(TestSuiteSnapshot.pass_rate)).group_by(TestSuiteSnapshot.snapshot_date).order_by(TestSuiteSnapshot.snapshot_date).limit(30)
        pass_rate_trend_rows = (await self.db.execute(pass_rate_trend_stmt)).all()
        pass_rate_trend = [{"date": r[0], "rate": round(r[1] or 0, 3)} for r in pass_rate_trend_rows]

        avg_flaky_stmt = select(func.avg(TestCase.flaky_rate)).where(TestCase.flaky_rate.isnot(None))
        avg_flaky = (await self.db.execute(avg_flaky_stmt)).scalar() or 0.0
        stability = round(1.0 - avg_flaky, 2)

        reliability = round(pass_rate_7d, 2)

        dur_covered_stmt = select(func.count(TestCase.id)).where(TestCase.avg_duration_seconds.isnot(None))
        dur_covered = (await self.db.execute(dur_covered_stmt)).scalar() or 0
        timeliness = round(dur_covered / total, 2) if total > 0 else 0.0

        owner_covered_stmt = select(func.count(TestCase.id)).where(TestCase.owner.isnot(None))
        owner_covered = (await self.db.execute(owner_covered_stmt)).scalar() or 0
        coverage = round(owner_covered / total, 2) if total > 0 else 0.0

        return {
            "health_score": {"overall": round(avg_hs, 1), "pass_rate": round(pass_rate_7d, 3), "stability": stability, "reliability": reliability, "timeliness": timeliness, "coverage": coverage, "level": hs_level},
            "total_cases": total, "pass_rate_7d": round(pass_rate_7d, 3),
            "flaky_case_count": flaky_count, "attention_case_count": attention,
            "avg_duration_p50": round(avg_dur, 1),
            "suite_distribution": suite_distribution, "result_distribution": result_distribution,
            "health_trend": health_trend, "pass_rate_trend": pass_rate_trend,
        }

    async def get_suites(self) -> list[dict[str, Any]]:
        stmt = select(TestCase.test_suite, TestCase.test_type, TestCase.hardware,
                       func.count(TestCase.id), func.avg(TestCase.health_score), func.avg(TestCase.pass_rate_7d),
                       func.sum(case((TestCase.is_flaky == True, 1), else_=0)),
                       func.avg(TestCase.avg_duration_seconds)).group_by(
            TestCase.test_suite, TestCase.test_type, TestCase.hardware)
        rows = (await self.db.execute(stmt)).all()
        max_run_stmt = select(TestCase.test_suite, TestCase.hardware, func.max(TestCase.last_run_at)).group_by(TestCase.test_suite, TestCase.hardware)
        max_run_rows = (await self.db.execute(max_run_stmt)).all()
        last_run_map = {f"{r[0]}-{r[1]}": r[2] for r in max_run_rows}
        results = []
        for r in rows:
            key = f"{r[0]}-{r[2]}"
            results.append({
                "suite_name": r[0], "test_type": r[1], "hardware": r[2], "card_count": None,
                "total_cases": r[3], "health_score": round(r[4] or 0, 1),
                "health_level": TestHealthCalculator._score_to_level(r[4] or 0),
                "pass_rate": round(r[5] or 0, 3), "flaky_cases": r[6] or 0,
                "avg_duration_seconds": round(r[7] or 0, 1),
                "last_run_at": last_run_map.get(key),
            })
        return results

    async def get_cases(self, filters: dict[str, Any] = None, page: int = 1, per_page: int = 20) -> dict[str, Any]:
        stmt = select(TestCase)
        if filters:
            if filters.get("test_type"):
                stmt = stmt.where(TestCase.test_type == filters["test_type"])
            if filters.get("hardware"):
                stmt = stmt.where(TestCase.hardware == filters["hardware"])
            if filters.get("test_suite"):
                stmt = stmt.where(TestCase.test_suite == filters["test_suite"])
            if filters.get("module_name"):
                stmt = stmt.where(TestCase.module_name == filters["module_name"])
            if filters.get("result"):
                stmt = stmt.where(TestCase.last_result == filters["result"])
            if filters.get("health_level"):
                stmt = stmt.where(TestCase.health_level == filters["health_level"])
            if filters.get("is_flaky"):
                stmt = stmt.where(TestCase.is_flaky == bool(filters["is_flaky"]))
            if filters.get("owner"):
                stmt = stmt.where(TestCase.owner == filters["owner"])

        sort = (filters or {}).get("sort", "health_score")
        order = (filters or {}).get("order", "desc")
        sort_map = {"health_score": TestCase.health_score, "pass_rate": TestCase.pass_rate_7d,
                    "flaky_rate": TestCase.flaky_rate, "duration": TestCase.avg_duration_seconds,
                    "last_run": TestCase.last_run_at,
                    "lifetime_runs": TestCase.lifetime_runs,
                    "lifetime_failures": TestCase.lifetime_failures,
                    "issues_found": TestCase.issues_found,
                    "effective_issues_found": TestCase.auto_issues_found,
                    "suspected_test_issue_count": TestCase.suspected_test_issue_count,
                    "effective_suspected_test_issue_count": TestCase.auto_suspected_test_issue_count}
        sort_col = sort_map.get(sort, TestCase.health_score)
        stmt = stmt.order_by(desc(sort_col) if order == "desc" else asc(sort_col))

        total_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await self.db.execute(total_stmt)).scalar() or 0
        stmt = stmt.offset((page - 1) * per_page).limit(per_page)
        result = await self.db.execute(stmt)
        items = result.scalars().all()
        return {"total": total, "items": items, "page": page, "page_size": per_page}

    async def get_case_detail(self, case_id: int) -> dict[str, Any] | None:
        stmt = select(TestCase).where(TestCase.id == case_id)
        case = (await self.db.execute(stmt)).scalar_one_or_none()
        if not case:
            return None
        runs_stmt = select(TestRun).where(TestRun.test_case_id == case_id).order_by(desc(TestRun.started_at)).limit(30)
        runs = list((await self.db.execute(runs_stmt)).scalars().all())
        return {"case": case, "runs": runs}

    async def get_flaky_cases(self, min_flip_rate: float = 0.01, days: int = 30, filters: dict = None, page: int = 1, per_page: int = 20) -> dict[str, Any]:
        stmt = select(TestCase).where(TestCase.is_flaky == True, TestCase.flaky_rate >= min_flip_rate)
        if filters:
            if filters.get("test_suite"):
                stmt = stmt.where(TestCase.test_suite == filters["test_suite"])
            if filters.get("module_name"):
                stmt = stmt.where(TestCase.module_name == filters["module_name"])

        sort = filters.get("sort", "flaky_rate") if filters else "flaky_rate"
        sort_map = {"flip_rate": TestCase.flaky_rate, "flip_count": TestCase.flip_count_30d, "total_runs": TestCase.total_runs}
        sort_col = sort_map.get(sort, TestCase.flaky_rate)
        stmt = stmt.order_by(desc(sort_col))

        total = (await self.db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
        stmt = stmt.offset((page - 1) * per_page).limit(per_page)
        items = list((await self.db.execute(stmt)).scalars().all())

        flaky_details = []
        for case in items:
            runs = await self._get_recent_results(case.id, days)
            suggested = "紧急修复" if case.flaky_rate > 0.25 else "需要治理" if case.flaky_rate > 0.10 else "观察期"
            flaky_details.append({
                "test_name": case.test_name, "test_suite": case.test_suite,
                "module_name": case.module_name, "owner": case.owner,
                "flip_rate": case.flaky_rate, "total_runs": case.total_runs,
                "flip_count": case.flip_count_30d, "recent_results": runs[:10],
                "suggested_action": suggested,
            })
        return {"total": total, "items": flaky_details, "page": page, "page_size": per_page}

    async def get_failure_breakdown(self, days: int = 30, category: str | None = None, suite_name: str | None = None) -> dict[str, Any]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = select(TestRun.failure_category, func.count(TestRun.id)).where(
            TestRun.result == "failed", TestRun.started_at >= cutoff
        ).group_by(TestRun.failure_category)
        if category:
            stmt = stmt.where(TestRun.failure_category == category)
        if suite_name:
            stmt = stmt.where(TestRun.workflow_name == suite_name)
        rows = (await self.db.execute(stmt)).all()
        cat_counts = {r[0] or "unknown": r[1] for r in rows}
        total = sum(cat_counts.values())
        pb = cat_counts.get("product_bug", 0)
        tb = cat_counts.get("test_bug", 0)
        infra = cat_counts.get("infrastructure", 0)
        unk = cat_counts.get("unknown", 0)
        flaky_fail_stmt = select(func.count(TestCase.id)).where(
            TestCase.is_flaky == True, TestCase.last_result == "failed"
        )
        flaky_failures = (await self.db.execute(flaky_fail_stmt)).scalar() or 0
        return {
            "product_bug": pb, "test_bug": tb, "infrastructure": infra, "unknown": unk, "total": total,
            "product_bug_ratio": round(pb / total, 2) if total else 0,
            "infrastructure_ratio": round(infra / total, 2) if total else 0,
            "noise_ratio": round((flaky_failures + infra) / total, 2) if total else 0,
        }

    async def get_duration_analysis(self, days: int = 30, suite_name: str | None = None) -> dict[str, Any]:
        stmt = select(TestCase.test_name, TestCase.avg_duration_seconds, TestCase.duration_p90_seconds).where(
            TestCase.avg_duration_seconds.isnot(None)
        )
        if suite_name:
            stmt = stmt.where(TestCase.test_suite == suite_name)
        stmt = stmt.order_by(desc(TestCase.avg_duration_seconds)).limit(20)
        rows = (await self.db.execute(stmt)).all()
        return {"top_slow": [{"test_name": r[0], "avg_duration": r[1], "p90_duration": r[2]} for r in rows]}

    async def get_owner_matrix(self) -> list[dict[str, Any]]:
        stmt = select(TestCase.owner, TestCase.module_name, func.count(TestCase.id),
                       func.avg(TestCase.pass_rate_7d), func.sum(case((TestCase.is_flaky == True, 1), else_=0)),
                       func.sum(case((TestCase.last_result == "failed", 1), else_=0))).group_by(TestCase.owner, TestCase.module_name)
        rows = (await self.db.execute(stmt)).all()
        owner_map: dict[str, dict] = {}
        for r in rows:
            owner = r[0] or "未分配"
            if owner not in owner_map:
                owner_map[owner] = {"owner": owner, "modules": [], "total_cases": 0, "pass_rate_7d": 0, "flaky_cases": 0, "pending_failures": 0, "avg_fix_hours": None}
            owner_map[owner]["modules"].append(r[1] or "unknown")
            owner_map[owner]["total_cases"] += r[2]
            owner_map[owner]["pass_rate_7d"] = r[3] or 0
            owner_map[owner]["flaky_cases"] += r[4] or 0
            owner_map[owner]["pending_failures"] += r[5] or 0
        return list(owner_map.values())

    async def get_module_health(self) -> list[dict[str, Any]]:
        stmt = select(TestCase.module_name, TestCase.owner, func.count(TestCase.id),
                       func.avg(TestCase.pass_rate_7d), func.sum(case((TestCase.is_flaky == True, 1), else_=0)),
                       func.sum(case((TestCase.last_result == "failed", 1), else_=0)),
                       func.avg(TestCase.health_score)).group_by(TestCase.module_name, TestCase.owner)
        rows = (await self.db.execute(stmt)).all()
        return [{"module_name": r[0] or "unknown", "owner": r[1], "total_cases": r[2],
                 "pass_rate_7d": round(r[3] or 0, 3), "flaky_count": r[4] or 0,
                 "pending_failures": r[5] or 0, "health_score": round(r[6] or 0, 1),
                 "health_level": TestHealthCalculator._score_to_level(r[6] or 0)} for r in rows]

    async def parse_ci_results(self, days_back: int = 7, force: bool = False) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=days_back)
        stmt = select(CIJob).where(CIJob.completed_at >= cutoff, CIJob.conclusion.isnot(None))
        if not force:
            parsed_stmt = select(func.count(TestRun.id))
            if (await self.db.execute(parsed_stmt)).scalar() > 0:
                latest_stmt = select(func.max(TestRun.started_at))
                latest = (await self.db.execute(latest_stmt)).scalar()
                if latest:
                    stmt = stmt.where(CIJob.started_at > latest)
        jobs = list((await self.db.execute(stmt.order_by(desc(CIJob.started_at)).limit(500))).scalars().all())
        classifier = FailureClassifier()
        count = 0
        for job in jobs:
            try:
                results = await self._parse_job_results(job, classifier)
                count += results
            except Exception as e:
                logger.warning(f"Failed to parse job {job.job_id}: {e}")
        if count > 0:
            calc = TestHealthCalculator(self.db)
            await calc.calculate_all_health_scores()
        await self.db.commit()
        return count

    async def _parse_job_results(self, ci_job: CIJob, classifier: FailureClassifier) -> int:
        if not self.github:
            return 0
        try:
            artifacts = await self.github.list_artifacts(ci_job.run_id)
        except Exception as e:
            logger.warning(f"Failed to list artifacts for run {ci_job.run_id}: {e}")
            artifacts = []

        parsed_results = []
        timing_artifact = next((a for a in artifacts if "test_timing" in a.get("name", "").lower()), None)
        junit_artifact = next((a for a in artifacts if "junit" in a.get("name", "").lower()), None)

        if junit_artifact:
            try:
                content = await self.github.download_artifact(junit_artifact["id"])
                parsed_results = JUnitXMLParser.parse(content)
            except Exception as e:
                logger.warning(f"Failed to download JUnit artifact: {e}")

        if not parsed_results and timing_artifact:
            try:
                content = await self.github.download_artifact(timing_artifact["id"])
                parsed_results = TestTimingParser.parse(content)
            except Exception as e:
                logger.warning(f"Failed to download timing artifact: {e}")

        if not parsed_results:
            if ci_job.job_name and TestBoardService._is_test_job(ci_job.job_name):
                name = ci_job.job_name.split(" / ")[0].strip()
                parsed_results.append({
                    "test_name": name,
                    "test_file": ci_job.job_name,
                    "result": "passed" if ci_job.conclusion == "success" else "failed" if ci_job.conclusion else "unknown",
                    "duration_seconds": ci_job.duration_seconds,
                    "data_granularity": "job_level",
                })

        run_stmt = select(CIResult).where(CIResult.run_id == ci_job.run_id).limit(1)
        ci_result = (await self.db.execute(run_stmt)).scalar_one_or_none()

        count = 0
        for pr in parsed_results:
            metadata = self._infer_metadata(ci_job.job_name, ci_job.workflow_name, ci_job.hardware)
            test_case = await self._ensure_test_case(pr, ci_job, metadata)
            category, confidence = await classifier.classify(
                TestRun(test_case_id=test_case.id, result=pr["result"], ci_job_id=ci_job.job_id,
                        ci_run_id=ci_job.run_id, workflow_name=ci_job.workflow_name, job_name=ci_job.job_name,
                        duration_seconds=pr.get("duration_seconds"), head_sha=ci_result.head_sha if ci_result else None,
                        event=ci_result.event if ci_result else "schedule",
                        branch=ci_result.branch if ci_result else None,
                        started_at=ci_job.started_at, completed_at=ci_job.completed_at,
                        failure_category=None,
                        failure_message=pr.get("failure_message")),
                ci_job, self.db
            )
            run = TestRun(
                test_case_id=test_case.id, result=pr["result"],
                ci_job_id=ci_job.job_id, ci_run_id=ci_job.run_id,
                workflow_name=ci_job.workflow_name, job_name=ci_job.job_name,
                duration_seconds=pr.get("duration_seconds"),
                model_load_seconds=pr.get("model_load_seconds"),
                test_exec_seconds=pr.get("test_exec_seconds"),
                failure_category=category if pr["result"] == "failed" else None,
                failure_message=pr.get("failure_message"),
                head_sha=ci_result.head_sha if ci_result else None,
                event=ci_result.event if ci_result else "schedule",
                branch=ci_result.branch if ci_result else None,
                started_at=ci_job.started_at, completed_at=ci_job.completed_at,
            )
            self.db.add(run)
            test_case.data_granularity = pr.get("data_granularity", test_case.data_granularity)
            # 全生命周期累计计数（自用例上线以来），不受滑动窗口与数据清理影响
            # 使用 SQL 表达式赋值（flush 时生成 SET col = coalesce(col,0) + 1），原子递增，避免并发同步丢失
            test_case.lifetime_runs = func.coalesce(TestCase.lifetime_runs, 0) + 1
            if pr["result"] == "failed":
                test_case.lifetime_failures = func.coalesce(TestCase.lifetime_failures, 0) + 1
            count += 1
        return count

    async def _ensure_test_case(self, parsed_result: dict, ci_job: CIJob, metadata: dict) -> TestCase:
        test_name = parsed_result.get("test_name", "unknown")
        suite = metadata.get("test_suite", ci_job.workflow_name)
        hardware = metadata.get("hardware", ci_job.hardware or "unknown")
        stmt = select(TestCase).where(
            TestCase.test_name == test_name, TestCase.test_suite == suite, TestCase.hardware == hardware
        )
        existing = (await self.db.execute(stmt)).scalar_one_or_none()
        if existing:
            existing.last_seen_at = datetime.now(UTC)
            existing.last_result = parsed_result.get("result")
            existing.inference_confidence = metadata.get("inference_confidence", existing.inference_confidence)
            if not existing.category:
                existing.category = metadata.get("category", "other")
            if parsed_result.get("result") == "passed" and parsed_result.get("duration_seconds") is not None:
                existing.last_pass_duration_seconds = parsed_result["duration_seconds"]
            return existing

        owner_stmt = select(JobOwner.owner, JobOwner.email).where(
            JobOwner.workflow_name == ci_job.workflow_name, JobOwner.job_name == ci_job.job_name
        ).limit(1)
        owner_row = (await self.db.execute(owner_stmt)).first()

        tc = TestCase(
            test_name=test_name, test_suite=suite, test_type=metadata.get("test_type", "unknown"),
            category=metadata.get("category", "other"),
            hardware=hardware, card_count=metadata.get("card_count"),
            module_name=metadata.get("module_name"), file_path=parsed_result.get("test_file"),
            class_name=parsed_result.get("class_name"),
            test_name_hash=hashlib.md5(test_name.encode()).hexdigest()[:32],
            owner=owner_row[0] if owner_row else None, owner_email=owner_row[1] if owner_row else None,
            inference_confidence=metadata.get("inference_confidence", 0.0),
            data_granularity=parsed_result.get("data_granularity", "file_level"),
            first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
            last_result=parsed_result.get("result"),
        )
        self.db.add(tc)
        await self.db.flush()
        return tc

    @staticmethod
    def _is_test_job(job_name: str) -> bool:
        """Check if a CI job is a test execution job (not build/setup/cleanup)."""
        test_prefixes = (
            "single-node", "double-node", "multi-node",
            "doc-test", "e2e-upstream",
        )
        return any(job_name.startswith(prefix) for prefix in test_prefixes)

    def _infer_metadata(self, job_name: str, workflow_name: str, hardware: str | None, file_path: str | None = None) -> dict:
        jn = job_name.lower()
        wf = workflow_name.lower()
        test_type = "e2e" if "e2e" in jn or "nightly" in wf else "ut" if "ut" in jn else "unknown"
        if "nightly" in wf:
            category = "nightly"
        elif "weekly" in wf:
            category = "weekly"
        elif "e2e-full" in wf or "e2e_full" in wf:
            category = "e2e-full"
        else:
            category = "other"
        hw = hardware or ("A3" if "a3" in wf else "A2" if "a2" in wf else "unknown")
        card = 4 if "4card" in jn or "four_card" in jn else 2 if "2card" in jn or "two_card" in jn else 1 if "1card" in jn or "one_card" in jn or "single" in jn else None
        module = None
        module_keywords = ("attention", "quantization", "compilation", "models", "serving",
                           "distributed", "pipeline", "lora", "speculative", "multimodal",
                           "vlm", "vision", "audio", "disaggregated", "graph", "vllm")
        for kw in module_keywords:
            if kw in jn or kw in wf:
                module = kw
                break
        if module is None and file_path:
            parts = [p.lower() for p in file_path.replace("\\", "/").split("/") if p]
            for p in parts:
                if p in module_keywords:
                    module = p
                    break
        if module is None:
            for model_kw in ("qwen", "deepseek", "llama", "glm", "kimi", "minimax", "gemma", "phi", "internlm"):
                if model_kw in jn:
                    module = f"models/{model_kw}"
                    break
        if module is None and "nightly" in wf:
            module = "e2e"
        confidence = 0.0
        if test_type != "unknown": confidence += 0.25
        if hw != "unknown": confidence += 0.25
        if card is not None: confidence += 0.25
        if module is not None: confidence += 0.25
        return {"test_type": test_type, "category": category, "test_suite": workflow_name, "hardware": hw, "card_count": card, "module_name": module, "inference_confidence": confidence}

    async def _get_recent_results(self, test_case_id: int, days: int) -> list[str]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = select(TestRun.result).where(TestRun.test_case_id == test_case_id, TestRun.started_at >= cutoff).order_by(TestRun.started_at)
        rows = (await self.db.execute(stmt)).all()
        return [r[0] for r in rows]
