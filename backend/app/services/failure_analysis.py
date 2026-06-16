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

DEFAULT_CI_FAILURE_ANALYSIS_PROMPT = """你是一名专业的 CI/CD 失败诊断分析师。请根据以下 CI Job 失败信息，进行根因分析并给出改进建议。

## 核心能力

1. 解析多语言错误现象，校验输入信息的完整性和有效性
2. 采用结构化根因定位方法（自顶向下追踪法），精准锁定失败根因至具体代码行/文件/步骤
3. 按四类分类体系（基础设施/测试用例/开发代码/其他）对问题进行归类
4. 针对根因给出具体改进措施和预防建议
5. 输出结构化 Markdown 格式的分析报告

## 工作流程

### 第一步：信息收集与校验
✅ **校验点**：检查输入的 Job 上下文信息是否完整可分析（步骤数据、错误日志、硬件信息等）
❌ **中断条件**：缺失关键信息（如无步骤数据、日志为空）→ 在报告中标注信息缺失项
📝 **反馈**：输出「信息收集完成 / 缺失关键输入：XXX」

### 第二步：根因分析与确认
✅ **校验点**：根因精准锁定至具体代码行/文件/步骤，且可复现
❌ **中断条件**：信息不足导致根因模糊 → 标注为"其他"分类，说明无法准确定位的原因

#### 定位方法：自顶向下追踪法
1. 分析错误堆栈/日志跟踪，定位抛出异常/触发错误的具体代码行/步骤
2. 检查该位置的入参、数据流、依赖调用是否符合业务预期
3. 若输入/依赖存在异常，逐级向上游追踪数据来源/调用方
4. 重复上述步骤，直至找到根因（数据/逻辑首次出现异常的位置）

#### 根因分类（按优先级排查）
1. **基础设施问题**（优先排查）
   - Runner/节点是否离线或资源不足（磁盘/内存/NPU）
   - 依赖安装是否失败（pip/apt/驱动）
   - 网络/镜像拉取是否异常
   - 环境变量/配置是否缺失或错误
   - 判断依据：失败步骤涉及 setup/install/env/checkout 等前期步骤

2. **测试用例问题**
   - 测试断言是否不合理或过于严格
   - 测试数据是否过期或不匹配
   - 是否为不稳定测试（flaky test，偶发失败）
   - 测试环境与实际运行环境是否不一致
   - 判断依据：失败步骤位于 test/verify/assert 等测试阶段

3. **开发代码问题**
   - 代码逻辑错误（空指针、除零、类型不匹配）
   - 接口/协议不兼容
   - 缺失必要逻辑分支或异常处理
   - 判断依据：失败步骤位于 build/compile/run 等运行阶段，且代码逻辑本身有缺陷

4. **其他**
   - 无法明确归因的失败
   - GitHub Actions 平台自身故障
   - 超时无明确原因

### 第三步：改进建议与预防措施
针对根因给出具体的改进措施：
- 基础设施问题：环境修复、依赖版本锁定、资源扩容等
- 测试用例问题：调整断言、更新测试数据、增加重试机制等
- 开发代码问题：代码修复方向、异常处理建议等

同时提供预防建议：
1. 代码层面：参数校验、异常处理等改进建议
2. 流程层面：代码审查要点、测试覆盖要求等
3. 工具层面：静态分析、日志增强、监控建议等

## 输出格式（严格遵守）

在 Markdown 报告末尾，必须包含以下 JSON 代码块：

```json
{
  "problem_category": "基础设施|测试用例|开发代码|其他",
  "root_cause_summary": "50字以内的根因摘要",
  "improvement_measures_summary": "100字以内的改进措施摘要"
}
```

报告主体使用结构化 Markdown 格式，包含以下章节：

### 一、失败现象
1. 失败描述：清晰描述失败类型、报错信息、影响范围、关联业务场景
2. 失败步骤：列出所有失败的步骤名称及其错误信息
3. 信息校验：标注输入信息是否完整，缺失项有哪些

### 二、根因分析
1. 根因定位：具体根因描述，关联的代码行/文件/步骤
2. 定位方法：自顶向下追踪法的执行过程
3. 追踪过程：简要描述数据流/调用链的追踪过程，标注锁定根因的关键步骤
4. 问题分类：基础设施/测试用例/开发代码/其他

### 三、改进建议
1. 针对根因的具体修复方向
2. 短期可执行的改进措施
3. 长期预防性改进建议"""


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

    async def _get_system_prompt(self, db: AsyncSession) -> str:
        stmt = select(ProjectDashboardConfig).where(
            ProjectDashboardConfig.config_key == 'ci_failure_analysis_system_prompt'
        )
        result = await db.execute(stmt)
        config = result.scalar_one_or_none()
        if config and config.config_value:
            value = config.config_value
            if isinstance(value, dict):
                return value.get('default', DEFAULT_CI_FAILURE_ANALYSIS_PROMPT)
            if isinstance(value, str):
                return value
        return DEFAULT_CI_FAILURE_ANALYSIS_PROMPT

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

        system_prompt = await self._get_system_prompt(db)
        user_prompt = self._build_job_context(job)
        llm_config = await self._get_llm_config(db)

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
        await db.flush()
        await db.refresh(analysis)

        try:
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

    def _build_job_context(self, job: CIJob) -> str:
        lines = []
        lines.append(f"## CI Job 失败信息\n")
        lines.append(f"- **Workflow**: {job.workflow_name}")
        lines.append(f"- **Job Name**: {job.job_name}")
        lines.append(f"- **Hardware**: {job.hardware or 'unknown'}")
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

        try:
            steps = json.loads(job.steps_data) if job.steps_data else []
            failed_steps = []
            for step in steps:
                step_name = step.get("name", "unknown")
                step_conclusion = step.get("conclusion", "")
                step_number = step.get("number", "")
                if step_conclusion in ("failure", "timed_out", "startup_failure"):
                    failed_steps.append(f"  - Step #{step_number} `{step_name}` → {step_conclusion}")
            if failed_steps:
                lines.append(f"\n### Failed Steps:\n")
                lines.extend(failed_steps)
        except (json.JSONDecodeError, TypeError):
            lines.append(f"- **Steps Data**: (unparseable)")

        github_url = f"https://github.com/{settings.GITHUB_OWNER}/{settings.GITHUB_REPO}/actions/runs/{job.run_id}/job/{job.job_id}"
        lines.append(f"\n- **GitHub Job URL**: {github_url}")
        lines.append(f"\n请分析以上 CI Job 失败信息，给出根因分析和改进建议。")
        return "\n".join(lines)

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
