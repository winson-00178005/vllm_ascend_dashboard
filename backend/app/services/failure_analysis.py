import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, UTC
from typing import Optional

from sqlalchemy import select, delete, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CIJob, ProjectDashboardConfig, JobFailureAnalysis
from app.models.daily_summary import LLMProviderConfig
from app.services.llm_client import LLMClient, LLMError
from app.services.failure_analysis_file_store import FailureAnalysisFileStore
from app.core.config import settings

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"基础设施", "测试用例", "开发代码", "其他"}

CATEGORY_KEYWORDS = {
    "基础设施": ["runner", "environment", "install", "setup", "driver", "pip",
                 "network", "disk", "memory", "npu", "docker", "依赖", "环境"],
    "测试用例": ["test", "assert", "flaky", "断言", "测试数据", "不稳定"],
    "开发代码": ["bug", "logic", "null", "pointer", "exception", "接口",
                 "空指针", "逻辑", "代码"],
}


class FailureAnalysisService:

    def __init__(self):
        self.llm_client = LLMClient()
        self.file_store = FailureAnalysisFileStore()

    async def _get_llm_config(self, db: AsyncSession):
        stmt = select(LLMProviderConfig).where(LLMProviderConfig.is_active == True).limit(1)
        result = await db.execute(stmt)
        config = result.scalar_one_or_none()
        if not config:
            raise ValueError("No active LLM provider configured")
        if not config.api_key:
            raise ValueError(f"API Key not configured for provider: {config.provider}")
        return config

    def _get_default_prompt(self) -> str:
        from app.services.skill_registry import get_skill_registry
        registry = get_skill_registry()
        skill = registry.get_skill_by_scope("ci_failure_analysis")
        if skill and skill.content:
            return skill.content
        return "你是一名专业的 CI/CD 失败诊断分析师。请根据提供的 CI Job 失败信息，进行根因分析并给出改进建议。"

    async def _get_system_prompt(self, db: AsyncSession) -> str:
        stmt = select(ProjectDashboardConfig).where(
            ProjectDashboardConfig.config_key == 'ci_failure_analysis_system_prompt'
        )
        result = await db.execute(stmt)
        config = result.scalar_one_or_none()
        if config and config.config_value:
            value = config.config_value
            if isinstance(value, dict):
                return value.get('default', self._get_default_prompt())
            if isinstance(value, str):
                return value
        return self._get_default_prompt()

    async def analyze_failed_job(self, job_id: int, db: AsyncSession, force: bool = False):
        stmt = select(CIJob).where(CIJob.job_id == job_id)
        result = await db.execute(stmt)
        job = result.scalar_one_or_none()
        if not job:
            raise ValueError(f"CIJob with job_id={job_id} not found")
        if job.conclusion not in ("failure", "cancelled"):
            raise ValueError(f"CIJob {job_id} conclusion is '{job.conclusion}', not a failed/cancelled job")

        if force:
            del_stmt = delete(JobFailureAnalysis).where(
                JobFailureAnalysis.job_id == job_id
            )
            await db.execute(del_stmt)
            await db.flush()

        existing_stmt = select(JobFailureAnalysis).where(
            JobFailureAnalysis.job_id == job_id
        ).order_by(JobFailureAnalysis.id.desc()).limit(1)
        existing_result = await db.execute(existing_stmt)
        existing = existing_result.scalar_one_or_none()
        if existing and existing.analysis_status in ("completed", "reused") and not force:
            return existing

        fingerprint = self.compute_failure_fingerprint(job)

        dedup_stmt = select(JobFailureAnalysis).where(
            and_(
                JobFailureAnalysis.failure_fingerprint == fingerprint,
                JobFailureAnalysis.analysis_status == "completed",
                JobFailureAnalysis.job_id != job_id
            )
        ).limit(1)
        dedup_result = await db.execute(dedup_stmt)
        dedup_match = dedup_result.scalar_one_or_none()

        if dedup_match and not force:
            reused = JobFailureAnalysis(
                job_id=job_id,
                run_id=job.run_id,
                workflow_name=job.workflow_name,
                job_name=job.job_name,
                failure_date=job.completed_at or datetime.now(UTC),
                failure_fingerprint=fingerprint,
                reused_analysis_id=dedup_match.id,
                problem_category=dedup_match.problem_category,
                root_cause_summary=dedup_match.root_cause_summary,
                improvement_measures_summary=dedup_match.improvement_measures_summary,
                report_file_path=dedup_match.report_file_path,
                analysis_status="reused",
            )
            db.add(reused)
            await db.commit()
            await db.refresh(reused)
            return reused

        analysis = JobFailureAnalysis(
            job_id=job_id,
            run_id=job.run_id,
            workflow_name=job.workflow_name,
            job_name=job.job_name,
            failure_date=job.completed_at or datetime.now(UTC),
            failure_fingerprint=fingerprint,
            analysis_status="analyzing",
        )
        db.add(analysis)
        await db.commit()
        await db.refresh(analysis)

        try:
            system_prompt = await self._get_system_prompt(db)
            user_prompt = await self._build_job_context(job, db)
            llm_config = await self._get_llm_config(db)

            llm_result = await self.llm_client.generate(
                provider=llm_config.provider,
                model=llm_config.default_model,
                api_key=llm_config.api_key,
                api_base=llm_config.api_base_url,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=8192,
            )
            parsed = self.parse_llm_response(llm_result.content)
            report_content = parsed.get("full_report", llm_result.content)
            report_path = await self.file_store.save_report(
                job.workflow_name, job.job_name, job_id, report_content
            )

            analysis.analysis_status = "completed"
            analysis.problem_category = parsed["problem_category"]
            analysis.root_cause_summary = parsed["root_cause_summary"]
            analysis.improvement_measures_summary = parsed["improvement_measures_summary"]
            analysis.report_file_path = report_path
            analysis.llm_provider = llm_config.provider
            analysis.llm_model = llm_config.default_model
            analysis.prompt_tokens = llm_result.prompt_tokens
            analysis.completion_tokens = llm_result.completion_tokens
            analysis.generation_time_seconds = llm_result.generation_time

            await db.commit()
            await db.refresh(analysis)
        except (LLMError, Exception) as e:
            analysis.analysis_status = "failed"
            analysis.error_message = str(e)
            await db.commit()
            await db.refresh(analysis)
            logger.error(f"LLM analysis failed for job {job_id}: {e}")

        return analysis

    async def analyze_batch(self, days_back: int, db: AsyncSession):
        cutoff = datetime.now(UTC) - timedelta(days=days_back)
        stmt = select(CIJob).where(
            and_(
                CIJob.conclusion.in_(["failure", "cancelled"]),
                CIJob.completed_at >= cutoff,
            )
        )
        result = await db.execute(stmt)
        jobs = result.scalars().all()

        analysed_stmt = select(JobFailureAnalysis.job_id).where(
            JobFailureAnalysis.analysis_status.in_(["completed", "reused"])
        )
        analysed_result = await db.execute(analysed_stmt)
        analysed_ids = set(analysed_result.scalars().all())

        results = []
        for job in jobs:
            if job.job_id in analysed_ids:
                continue
            try:
                analysis = await self.analyze_failed_job(job.job_id, db)
                results.append(analysis)
            except Exception as e:
                logger.error(f"Batch analysis failed for job {job.job_id}: {e}")
        return results

    async def _build_job_context(self, job: CIJob, db: AsyncSession) -> str:
        lines = []
        lines.append(f"## CI Job 失败信息\n")
        lines.append(f"- **Workflow**: {job.workflow_name}")
        lines.append(f"- **Job Name**: {job.job_name}")
        lines.append(f"- **Hardware**: {job.hardware or 'unknown'}")
        lines.append(f"- **Runner**: {job.runner_name or 'unknown'}")
        try:
            labels = json.loads(job.runner_labels) if job.runner_labels else []
            if isinstance(labels, list):
                lines.append(f"- **Runner Labels**: {', '.join(labels)}")
            else:
                lines.append(f"- **Runner Labels**: {labels}")
        except (json.JSONDecodeError, TypeError):
            lines.append(f"- **Runner Labels**: {job.runner_labels or 'unknown'}")
        lines.append(f"- **Conclusion**: {job.conclusion}")
        lines.append(f"- **Duration**: {job.duration_seconds or 'unknown'}s")
        lines.append(f"- **Started At**: {job.started_at or 'unknown'}")
        lines.append(f"- **Completed At**: {job.completed_at or 'unknown'}")

        try:
            steps = json.loads(job.steps_data) if job.steps_data else []
            failed_steps = []
            all_steps_summary = []
            for step in steps:
                step_name = step.get("name", "unknown")
                step_conclusion = step.get("conclusion", "")
                step_number = step.get("number", "")
                step_started = step.get("started_at", "")
                step_completed = step.get("completed_at", "")
                step_status = step.get("status", "")

                if step_conclusion in ("failure", "timed_out", "startup_failure"):
                    duration_info = ""
                    if step_started and step_completed:
                        try:
                            from datetime import datetime as dt
                            s = dt.fromisoformat(step_started.replace("Z", "+00:00"))
                            e = dt.fromisoformat(step_completed.replace("Z", "+00:00"))
                            secs = int((e - s).total_seconds())
                            duration_info = f" (耗时 {secs}s)"
                        except Exception:
                            pass
                    failed_steps.append(f"  - Step #{step_number} `{step_name}` → {step_conclusion}{duration_info}")
                elif step_status == "completed" and step_conclusion == "success":
                    continue
                else:
                    all_steps_summary.append(f"  - Step #{step_number} `{step_name}` → status={step_status}, conclusion={step_conclusion}")

            if failed_steps:
                lines.append(f"\n### Failed Steps:\n")
                lines.extend(failed_steps)

            if all_steps_summary:
                lines.append(f"\n### Other Non-Success Steps:\n")
                lines.extend(all_steps_summary)
        except (json.JSONDecodeError, TypeError):
            lines.append(f"- **Steps Data**: (unparseable)")

        annotations = await self._fetch_job_annotations(job.job_id, db)
        if annotations:
            lines.append(f"\n### GitHub Actions Annotations (关键错误信息):\n")
            for ann in annotations:
                level = ann.get("annotation_level", "notice")
                title = ann.get("title", "")
                message = ann.get("message", "")
                path_info = ann.get("path", "")
                if title:
                    lines.append(f"  - [{level}] **{title}**: {message}")
                else:
                    lines.append(f"  - [{level}] {message}")

        github_url = f"https://github.com/{settings.GITHUB_OWNER}/{settings.GITHUB_REPO}/actions/runs/{job.run_id}/job/{job.job_id}"
        lines.append(f"\n- **GitHub Job URL**: {github_url}")
        lines.append(f"\n请分析以上 CI Job 失败信息，特别关注 Annotations 中的错误提示，给出根因分析和改进建议。")
        return "\n".join(lines)

    async def _fetch_job_annotations(self, job_id: int, db: AsyncSession) -> list[dict]:
        try:
            from app.services.github_client import GitHubClient
            github_token = settings.GITHUB_TOKEN
            if not github_token:
                logger.warning("GitHub token not configured, cannot fetch annotations")
                return []
            client = GitHubClient(token=github_token, owner=settings.GITHUB_OWNER, repo=settings.GITHUB_REPO)
            url = f"/repos/{settings.GITHUB_OWNER}/{settings.GITHUB_REPO}/actions/jobs/{job_id}/annotations"
            response = await client._request("GET", url)
            if isinstance(response, list):
                return response
            if isinstance(response, dict) and 'items' in response:
                return response.get('items', [])
            return []
        except Exception as e:
            logger.warning(f"Failed to fetch annotations for job {job_id}: {e}")
            return []

    @staticmethod
    def compute_failure_fingerprint(job: CIJob) -> str:
        try:
            steps = json.loads(job.steps_data) if job.steps_data else []
        except (json.JSONDecodeError, TypeError):
            steps = []

        failed_steps = []
        for step in steps:
            if step.get("conclusion") in ("failure", "timed_out", "startup_failure"):
                failed_steps.append({"name": step.get("name", ""), "conclusion": step.get("conclusion", "")})

        if not failed_steps:
            fingerprint_data = json.dumps({"conclusion": job.conclusion or ""}, sort_keys=True)
        else:
            fingerprint_data = json.dumps({"failed_steps": failed_steps}, sort_keys=True)

        return hashlib.md5(fingerprint_data.encode()).hexdigest()

    @staticmethod
    def parse_llm_response(raw: str) -> dict:
        json_block_match = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
        if json_block_match:
            try:
                data = json.loads(json_block_match.group(1))
                category = data.get("problem_category", "其他")
                if category not in VALID_CATEGORIES:
                    for cat in VALID_CATEGORIES:
                        if cat in category:
                            category = cat
                            break
                    else:
                        category = "其他"
                return {
                    "problem_category": category,
                    "root_cause_summary": data.get("root_cause_summary", "解析成功但摘要缺失"),
                    "improvement_measures_summary": data.get("improvement_measures_summary", "解析成功但措施缺失"),
                    "full_report": raw,
                }
            except json.JSONDecodeError:
                pass

        category = None
        cause = None
        measures = None

        cat_match = re.search(r'"problem_category"\s*:\s*"([^"]+)"', raw)
        if cat_match:
            category = cat_match.group(1)
        cause_match = re.search(r'"root_cause_summary"\s*:\s*"([^"]+)"', raw)
        if cause_match:
            cause = cause_match.group(1)
        measures_match = re.search(r'"improvement_measures_summary"\s*:\s*"([^"]+)"', raw)
        if measures_match:
            measures = measures_match.group(1)

        if category not in VALID_CATEGORIES:
            for cat, keywords in CATEGORY_KEYWORDS.items():
                if any(kw in raw.lower() for kw in keywords):
                    category = cat
                    break
            if category not in VALID_CATEGORIES:
                category = "其他"

        return {
            "problem_category": category or "其他",
            "root_cause_summary": cause or "解析失败，请查看完整报告",
            "improvement_measures_summary": measures or "解析失败，请查看完整报告",
            "full_report": raw,
        }

    async def get_analysis(self, analysis_id: int, db: AsyncSession):
        stmt = select(JobFailureAnalysis).where(JobFailureAnalysis.id == analysis_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_analysis_by_job_id(self, job_id: int, db: AsyncSession):
        stmt = select(JobFailureAnalysis).where(JobFailureAnalysis.job_id == job_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_analyses(self, filters: Optional[dict] = None, db: AsyncSession = None):
        stmt = select(JobFailureAnalysis)
        if filters:
            if filters.get("problem_category"):
                stmt = stmt.where(JobFailureAnalysis.problem_category == filters["problem_category"])
            if filters.get("analysis_status"):
                stmt = stmt.where(JobFailureAnalysis.analysis_status == filters["analysis_status"])
            if filters.get("workflow_name"):
                stmt = stmt.where(JobFailureAnalysis.workflow_name == filters["workflow_name"])
            if filters.get("days_back"):
                cutoff = datetime.now(UTC) - timedelta(days=filters["days_back"])
                stmt = stmt.where(JobFailureAnalysis.failure_date >= cutoff)
        stmt = stmt.order_by(JobFailureAnalysis.failure_date.desc())
        result = await db.execute(stmt)
        items = result.scalars().all()
        return {"total": len(items), "items": items}
