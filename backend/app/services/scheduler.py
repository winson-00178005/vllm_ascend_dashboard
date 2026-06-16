"""
数据同步定时任务调度器
使用 APScheduler 实现定时数据采集
"""
import asyncio
import logging
from datetime import UTC, datetime

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.db.base import SessionLocal
from app.services.ci_collector import CICollector
from app.services.github_client import GitHubClient

logger = logging.getLogger(__name__)


def _read_config_from_db(config_key: str) -> dict | None:
    async def _read():
        from sqlalchemy import select as sa_select
        from app.models import ProjectDashboardConfig
        async with SessionLocal() as db:
            stmt = sa_select(ProjectDashboardConfig).where(
                ProjectDashboardConfig.config_key == config_key
            )
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()
            if row and row.config_value:
                return dict(row.config_value)
            return None
    try:
        return asyncio.run(_read())
    except Exception as e:
        logger.warning(f"Failed to read config '{config_key}' from database: {e}")
        return None


class DataSyncScheduler:
    """
    数据同步定时任务调度器
    
    功能：
    - 定时同步 CI 数据（可配置间隔）
    - 手动触发同步
    - 任务执行监控
    """

    def __init__(self):
        """初始化调度器"""
        # 时区从数据库读取，默认 Asia/Shanghai
        from sqlalchemy import select
        from app.models import ProjectDashboardConfig
        from app.db.base import SessionLocal
        
        timezone_str = 'Asia/Shanghai'
        config_data = _read_config_from_db('daily_summary_schedule')
        if config_data and 'timezone' in config_data:
            timezone_str = config_data['timezone']
            logger.info(f"Loaded timezone from database: {timezone_str}")
        
        self.scheduler = AsyncIOScheduler(
            timezone=timezone_str,
            job_defaults={
                'coalesce': True,  # 合并错过的执行
                'max_instances': 1,  # 同一任务最多只有 1 个实例运行
                'misfire_grace_time': 60,  # 错过执行的容忍时间（秒）
            }
        )

        # 任务执行监听
        self.scheduler.add_listener(
            self._job_event_listener,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR
        )

        self.github_client: GitHubClient | None = None
        self._initialized = False

        logger.info("DataSyncScheduler initialized")

    def _job_event_listener(self, event):
        """任务执行事件监听"""
        if event.exception:
            logger.error(f"Job {event.job_id} failed: {event.exception}")
        else:
            logger.info(f"Job {event.job_id} executed successfully at {datetime.now()}")

    def start(self) -> None:
        """启动调度器"""
        logger.info("=" * 60)
        logger.info("SCHEDULER STARTING - Adding scheduled jobs")
        logger.info("=" * 60)
        
        if not self._initialized:
            self._initialize_github_client()

        # CI 数据同步任务 - 可配置间隔（默认 720 分钟 = 12 小时）
        sync_interval_minutes = getattr(settings, 'CI_SYNC_INTERVAL_MINUTES', 720)

        try:
            self.scheduler.add_job(
                self._sync_ci_data_job,
                trigger=IntervalTrigger(minutes=sync_interval_minutes),
                id="ci_data_sync",
                name="CI Data Sync",
                replace_existing=True,
            )
            logger.info(f"[1/4] CI data sync scheduled every {sync_interval_minutes} minutes")
        except Exception as e:
            logger.error(f"Failed to add CI data sync job: {e}", exc_info=True)

        # Project Dashboard Git 仓库缓存更新任务 - 每小时更新一次
        cache_update_interval = getattr(settings, 'PROJECT_DASHBOARD_CACHE_INTERVAL_MINUTES', 60)
        try:
            self.scheduler.add_job(
                self._update_project_dashboard_cache_job,
                trigger=IntervalTrigger(minutes=cache_update_interval),
                id="project_dashboard_cache_update",
                name="Project Dashboard Cache Update",
                replace_existing=True,
            )
            logger.info(f"[2/4] Project dashboard cache update scheduled every {cache_update_interval} minutes")
        except Exception as e:
            logger.error(f"Failed to add project dashboard cache update job: {e}", exc_info=True)

        # 模型报告同步任务 - 可配置间隔（默认 60 分钟）
        model_sync_interval = getattr(settings, 'MODEL_SYNC_INTERVAL_MINUTES', 60)
        try:
            self.scheduler.add_job(
                self._sync_model_reports_job,
                trigger=IntervalTrigger(minutes=model_sync_interval),
                id="model_report_sync",
                name="Model Report Sync",
                replace_existing=True,
            )
            logger.info(f"[3/4] Model report sync scheduled every {model_sync_interval} minutes")
        except Exception as e:
            logger.error(f"Failed to add model report sync job: {e}", exc_info=True)

        # 每日总结生成任务 - 每天早上 8 点执行（可配置）
        # 时区从数据库读取，默认 Asia/Shanghai
        try:
            from apscheduler.triggers.cron import CronTrigger

            # 从数据库读取时区配置，如果数据库未初始化则使用默认值
            timezone_str = 'Asia/Shanghai'
            config_data = _read_config_from_db('daily_summary_schedule')
            if config_data and 'timezone' in config_data:
                timezone_str = config_data['timezone']
                logger.info(f"Loaded timezone from database: {timezone_str}")

            cron_hour = getattr(settings, 'DAILY_SUMMARY_CRON_HOUR', 8)
            cron_minute = getattr(settings, 'DAILY_SUMMARY_CRON_MINUTE', 0)
            enabled = getattr(settings, 'DAILY_SUMMARY_ENABLED', True)

            if enabled:
                self.scheduler.add_job(
                    self._generate_daily_summary_job,
                    trigger=CronTrigger(hour=cron_hour, minute=cron_minute, timezone=timezone_str),
                    id="daily_summary_task",
                    name="Generate Daily Summary",
                    replace_existing=True,
                )
                logger.info(f"[4/4] Daily summary generation scheduled at {cron_hour}:{cron_minute:02d} {timezone_str} (enabled={enabled})")
            else:
                logger.info(f"[4/4] Daily summary generation DISABLED (enabled={enabled})")
        except Exception as e:
            logger.error(f"Failed to add daily summary job: {e}", exc_info=True)

        # 启动调度器
        if not self.scheduler.running:
            try:
                self.scheduler.start()
                logger.info("=" * 60)
                logger.info("SCHEDULER STARTED SUCCESSFULLY")
                jobs = self.scheduler.get_jobs()
                logger.info(f"Total jobs scheduled: {len(jobs)}")
                for job in jobs:
                    logger.info(f"  - {job.id}: {job.name}, next_run={job.next_run_time}")
                logger.info("=" * 60)
            except Exception as e:
                logger.error(f"Failed to start scheduler: {e}", exc_info=True)
        else:
            logger.info("Scheduler already running")

        # NPU 指标采集任务 - 默认每 1 分钟执行
        try:
            from app.schemas.resource_metrics import RESOURCE_METRICS_CONFIG_KEY
            metrics_interval = 1
            config_data = _read_config_from_db(RESOURCE_METRICS_CONFIG_KEY)
            if config_data and "interval_minutes" in config_data:
                metrics_interval = config_data["interval_minutes"]

            self.scheduler.add_job(
                self._collect_resource_metrics_job,
                trigger=IntervalTrigger(minutes=metrics_interval),
                id="resource_metrics_collect",
                name="Resource Metrics Collect",
                replace_existing=True,
            )
            logger.info(f"Resource metrics collection scheduled every {metrics_interval} minutes")
        except Exception as e:
            logger.error(f"Failed to add resource metrics collection job: {e}", exc_info=True)

        # NPU 指标数据清理任务 - 每天凌晨 00:00 执行
        try:
            from apscheduler.triggers.cron import CronTrigger
            self.scheduler.add_job(
                self._cleanup_resource_metrics_job,
                trigger=CronTrigger(hour=0, minute=0, timezone=timezone_str),
                id="resource_metrics_cleanup",
                name="Resource Metrics Cleanup",
                replace_existing=True,
            )
            logger.info(f"Resource metrics cleanup scheduled at 00:00 {timezone_str}")
        except Exception as e:
            logger.error(f"Failed to add resource metrics cleanup job: {e}", exc_info=True)

        # 失败分析兜底定时任务 - 每 30 分钟运行
        try:
            self.scheduler.add_job(
                self._failure_analysis_fallback_job,
                trigger=IntervalTrigger(minutes=30),
                id="failure_analysis_fallback",
                name="Failure Analysis Fallback",
                replace_existing=True,
            )
            logger.info("Failure analysis fallback scheduled every 30 minutes")
        except Exception as e:
            logger.error(f"Failed to add failure analysis fallback job: {e}", exc_info=True)

        # 每日运行报告邮件推送任务 - 每天早上 8:30 执行（可配置）
        # 与每日总结任务（8:00 AM）错开 30 分钟，确保数据采集先完成
        try:
            from apscheduler.triggers.cron import CronTrigger

            report_enabled = getattr(settings, 'REPORT_ENABLED', True)
            report_hour = getattr(settings, 'REPORT_SCHEDULE_HOUR', 8)
            report_minute = getattr(settings, 'REPORT_SCHEDULE_MINUTE', 30)

            timezone_str = 'Asia/Shanghai'
            report_config = _read_config_from_db('daily_summary_schedule')
            if report_config and 'timezone' in report_config:
                timezone_str = report_config['timezone']

            if report_enabled:
                self.scheduler.add_job(
                    self._send_daily_report_job,
                    trigger=CronTrigger(hour=report_hour, minute=report_minute, timezone=timezone_str),
                    id="daily_report_task",
                    name="Daily Report Email",
                    replace_existing=True,
                )
                logger.info(f"Daily report email scheduled at {report_hour}:{report_minute:02d} {timezone_str} (enabled={report_enabled})")
            else:
                logger.info(f"Daily report email DISABLED (enabled={report_enabled})")
        except Exception as e:
            logger.error(f"Failed to add daily report job: {e}", exc_info=True)

    def stop(self) -> None:
        """停止调度器"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("DataSyncScheduler stopped")

        # 注意：这里不能直接 await 关闭 GitHub 客户端
        # 需要在应用生命周期管理中使用 async close() 方法
        if self.github_client:
            logger.info("GitHub client will be closed on next cleanup")

    async def close(self) -> None:
        """关闭调度器并清理资源（异步版本）"""
        self.stop()
        if self.github_client:
            await self.github_client.close()
            self.github_client = None
            self._initialized = False
        logger.info("DataSyncScheduler resources cleaned up")

    def _initialize_github_client(self) -> None:
        """初始化 GitHub 客户端"""
        if not settings.GITHUB_TOKEN:
            logger.warning("GITHUB_TOKEN not configured, GitHub API calls will fail")

        self.github_client = GitHubClient(
            token=settings.GITHUB_TOKEN,
            owner=settings.GITHUB_OWNER,
            repo=settings.GITHUB_REPO,
        )
        self._initialized = True

    async def _sync_ci_data_job(self) -> None:
        """CI 数据同步任务"""
        logger.info("=" * 60)
        logger.info("CI DATA SYNC JOB STARTED")
        logger.info("=" * 60)

        if not self.github_client:
            self._initialize_github_client()

        async with SessionLocal() as db:
            try:
                collector = CICollector(
                    github_client=self.github_client,  # type: ignore
                    db_session=db,
                )

                # 使用配置中的同步策略
                days_back = getattr(settings, 'CI_SYNC_DAYS_BACK', 7)
                max_runs_per_workflow = getattr(settings, 'CI_SYNC_MAX_RUNS_PER_WORKFLOW', 100)
                force_full_refresh = getattr(settings, 'CI_SYNC_FORCE_FULL_REFRESH', False)

                logger.info(f"Sync strategy: days_back={days_back}, max_runs={max_runs_per_workflow}, force_full_refresh={force_full_refresh}")

                collected = await collector.collect_workflow_runs(
                    days_back=days_back,
                    max_runs_per_workflow=max_runs_per_workflow,
                    force_full_refresh=force_full_refresh,
                )

                # 同步完成后，更新所有启用的 workflow 的 last_sync_at
                from sqlalchemy import update

                from app.models import WorkflowConfig

                await db.execute(
                    update(WorkflowConfig)
                    .where(WorkflowConfig.enabled == True)
                    .values(last_sync_at=datetime.now(UTC))
                )
                await db.commit()

                logger.info("=" * 60)
                logger.info(f"CI DATA SYNC JOB COMPLETED - Collected {collected} runs")
                logger.info("=" * 60)

                try:
                    await self._trigger_failure_analysis_after_sync(db)
                except Exception as fae:
                    logger.warning(f"Post-sync failure analysis trigger failed: {fae}")

            except Exception as e:
                logger.error("=" * 60)
                logger.error(f"CI DATA SYNC JOB FAILED - Error: {e}", exc_info=True)
                logger.error("=" * 60)
                # async with 会自动 rollback 和 close
                raise

    def _update_project_dashboard_cache_job(self) -> None:
        """Project Dashboard Git 仓库缓存更新任务"""
        logger.info("=" * 60)
        logger.info("PROJECT DASHBOARD CACHE UPDATE JOB STARTED")
        logger.info("=" * 60)

        try:
            from app.services.github_cache import get_github_cache, get_github_cache_for_repo
            
            results = []
            
            # 更新 vllm-ascend 仓库
            logger.info("Updating vllm-ascend repository...")
            ascend_cache = get_github_cache()
            if not ascend_cache._is_repo_cloned():
                success = ascend_cache.clone()
                results.append(f"vllm-ascend: {'cloned' if success else 'clone failed'}")
            else:
                success = ascend_cache.pull()
                results.append(f"vllm-ascend: {'pulled' if success else 'pull failed'}")
            
            # 更新 vllm 仓库
            logger.info("Updating vllm repository...")
            vllm_cache = get_github_cache_for_repo(owner="vllm-project", repo="vllm")
            if not vllm_cache._is_repo_cloned():
                success = vllm_cache.clone()
                results.append(f"vllm: {'cloned' if success else 'clone failed'}")
            else:
                success = vllm_cache.pull()
                results.append(f"vllm: {'pulled' if success else 'pull failed'}")
            
            logger.info(f"Cache update results: {', '.join(results)}")
            logger.info("=" * 60)
            logger.info("PROJECT DASHBOARD CACHE UPDATE JOB COMPLETED")
            logger.info("=" * 60)
            
        except Exception as e:
            logger.error("=" * 60)
            logger.error(f"PROJECT DASHBOARD CACHE UPDATE JOB FAILED - Error: {e}", exc_info=True)
            logger.error("=" * 60)
            raise

    async def _sync_model_reports_job(self) -> None:
        """模型报告同步任务"""
        logger.info("=" * 60)
        logger.info("MODEL REPORT SYNC JOB STARTED")
        logger.info("=" * 60)

        if not self.github_client:
            self._initialize_github_client()

        async with SessionLocal() as db:
            try:
                from app.services.model_sync_service import ModelSyncService

                sync_service = ModelSyncService(db, self.github_client)

                # 使用配置中的同步策略
                days_back = getattr(settings, 'MODEL_SYNC_DAYS_BACK', 3)
                runs_limit = getattr(settings, 'MODEL_SYNC_RUNS_LIMIT', 100)

                logger.info(f"Model sync strategy: days_back={days_back}, runs_limit={runs_limit}")

                # 同步所有启用的模型同步配置
                total, collected = await sync_service.sync_all_enabled_configs(
                    days_back=days_back,
                    runs_limit=runs_limit,
                )

                # 同步完成后，更新所有启用的 workflow 的 last_sync_at
                from sqlalchemy import update

                from app.models import ModelSyncConfig

                await db.execute(
                    update(ModelSyncConfig)
                    .where(ModelSyncConfig.enabled == True)
                    .values(last_sync_at=datetime.now(UTC))
                )
                await db.commit()

                logger.info("=" * 60)
                logger.info(f"MODEL REPORT SYNC JOB COMPLETED - {total} configs, collected {collected} reports")
                logger.info("=" * 60)

            except Exception as e:
                logger.error("=" * 60)
                logger.error(f"MODEL REPORT SYNC JOB FAILED - Error: {e}", exc_info=True)
                logger.error("=" * 60)
                # async with 会自动 rollback 和 close
                raise

    async def _generate_daily_summary_job(self):
        """每日总结生成任务"""
        logger.info("=" * 60)
        logger.info("DAILY SUMMARY GENERATION JOB STARTED")
        logger.info("=" * 60)
        
        try:
            from datetime import date, timedelta
            from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
            from sqlalchemy.orm import sessionmaker
            from app.models.daily_summary import DailySummary
            from app.models import ProjectDashboardConfig
            from app.services.daily_summary import DailySummaryService
            from sqlalchemy import select

            # 创建数据库会话
            engine = create_async_engine(settings.DATABASE_URL, echo=False)
            async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

            async with async_session() as db:
                # 获取配置的项目列表
                projects_stmt = select(ProjectDashboardConfig).where(
                    ProjectDashboardConfig.config_key == 'daily_summary_projects'
                )
                projects_result = await db.execute(projects_stmt)
                projects_config = projects_result.scalar_one_or_none()

                projects = projects_config.config_value if projects_config else [
                    {"id": "ascend", "name": "vLLM Ascend", "enabled": True},
                    {"id": "vllm", "name": "vLLM", "enabled": True},
                ]

                # 计算昨天的日期
                yesterday = date.today() - timedelta(days=1)

                for project in projects:
                    if not project.get("enabled", True):
                        continue

                    project_id = project.get("id")
                    if not project_id:
                        continue

                    try:
                        # 1. 获取数据
                        logger.info(f"Fetching data for project: {project_id} on {yesterday}")
                        service = DailySummaryService(db)
                        await service.fetch_daily_data(project_id, yesterday)

                        # 2. 生成总结
                        logger.info(f"Generating summary for project: {project_id} on {yesterday}")
                        await service.generate_summary(project_id, yesterday)

                        logger.info(f"Daily summary completed for project: {project_id}")
                    except Exception as e:
                        logger.error(f"Failed to generate summary for {project_id}: {e}", exc_info=True)

                await db.commit()

            logger.info("=" * 60)
            logger.info("DAILY SUMMARY GENERATION JOB COMPLETED")
            logger.info("=" * 60)

        except Exception as e:
            logger.error("=" * 60)
            logger.error(f"DAILY SUMMARY GENERATION JOB FAILED - Error: {e}", exc_info=True)
            logger.error("=" * 60)

    async def _send_daily_report_job(self):
        """每日运行报告邮件推送任务"""
        logger.info("=" * 60)
        logger.info("DAILY REPORT EMAIL JOB STARTED")
        logger.info("=" * 60)

        try:
            from datetime import date, timedelta
            from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
            from sqlalchemy.orm import sessionmaker
            from sqlalchemy import select
            from app.models import ProjectDashboardConfig
            from app.services.daily_report import DailyReportService, REPORT_CONFIG_KEY

            if not settings.REPORT_ENABLED:
                logger.info("Report disabled, skipping")
                return

            engine = create_async_engine(settings.DATABASE_URL, echo=False)
            async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

            async with async_session() as db:
                stmt = select(ProjectDashboardConfig).where(
                    ProjectDashboardConfig.config_key == REPORT_CONFIG_KEY
                )
                config_result = await db.execute(stmt)
                config = config_result.scalar_one_or_none()
                db_config = config.config_value if config else {}

                if not db_config.get("report_recipients"):
                    logger.info("No recipients configured in DB, skipping")
                    return

                if not db_config.get("smtp_host"):
                    logger.info("SMTP_HOST not configured in DB, skipping")
                    return

                yesterday = date.today() - timedelta(days=1)
                service = DailyReportService(db)
                history = await service.send_report(yesterday)

                logger.info(f"Daily report result: status={history.status}, date={history.report_date}")

            await engine.dispose()

            logger.info("=" * 60)
            logger.info("DAILY REPORT EMAIL JOB COMPLETED")
            logger.info("=" * 60)

        except Exception as e:
            logger.error("=" * 60)
            logger.error(f"DAILY REPORT EMAIL JOB FAILED - Error: {e}", exc_info=True)
            logger.error("=" * 60)

    async def _trigger_failure_analysis_after_sync(self, _ci_db):
        """CI同步完成后触发失败Job分析（使用独立 db session，不阻塞 CI 事务）"""
        from app.services.failure_analysis import FailureAnalysisService
        async with SessionLocal() as db:
            service = FailureAnalysisService()
            results = await service.analyze_batch(days_back=7, db=db)
            logger.info(f"Post-sync failure analysis: processed {len(results)} failed jobs")

    async def _failure_analysis_fallback_job(self) -> None:
        """失败分析兜底定时任务"""
        logger.info("=" * 60)
        logger.info("FAILURE ANALYSIS FALLBACK JOB STARTED")
        logger.info("=" * 60)

        async with SessionLocal() as db:
            try:
                from app.services.failure_analysis import FailureAnalysisService
                service = FailureAnalysisService()
                results = await service.analyze_batch(days_back=7, db=db)
                logger.info(f"FAILURE ANALYSIS FALLBACK JOB COMPLETED - processed {len(results)} jobs")
            except Exception as e:
                logger.error(f"FAILURE ANALYSIS FALLBACK JOB FAILED - Error: {e}", exc_info=True)

    async def _collect_resource_metrics_job(self) -> None:
        """NPU 指标采集任务"""
        logger.info("=" * 60)
        logger.info("RESOURCE METRICS COLLECT JOB STARTED")
        logger.info("=" * 60)

        try:
            from app.services.resource_metrics import ResourceMetricsService

            async with SessionLocal() as db:
                service = ResourceMetricsService(db)
                count = await service.collect_snapshot()
                logger.info(f"RESOURCE METRICS COLLECT JOB COMPLETED - Collected {count} cluster metrics")

                # 评估告警规则
                try:
                    from app.services.alert_evaluator import AlertEvaluator
                    evaluator = AlertEvaluator(db)
                    alerts_triggered = await evaluator.evaluate_all_rules()
                    if alerts_triggered > 0:
                        logger.info(f"Alert evaluation: {alerts_triggered} alert(s) triggered")
                except Exception as alert_exc:
                    logger.error(f"Alert evaluation failed: {alert_exc}", exc_info=True)
        except Exception as e:
            logger.error("=" * 60)
            logger.error(f"RESOURCE METRICS COLLECT JOB FAILED - Error: {e}", exc_info=True)
            logger.error("=" * 60)

    async def _cleanup_resource_metrics_job(self) -> None:
        """NPU 指标数据清理任务"""
        logger.info("=" * 60)
        logger.info("RESOURCE METRICS CLEANUP JOB STARTED")
        logger.info("=" * 60)

        try:
            from app.services.resource_metrics import ResourceMetricsService

            async with SessionLocal() as db:
                service = ResourceMetricsService(db)
                deleted = await service.cleanup_old_metrics()
                logger.info(f"RESOURCE METRICS CLEANUP JOB COMPLETED - Deleted {deleted} old records")
        except Exception as e:
            logger.error("=" * 60)
            logger.error(f"RESOURCE METRICS CLEANUP JOB FAILED - Error: {e}", exc_info=True)
            logger.error("=" * 60)

    def update_daily_summary_schedule(self, enabled: bool, cron_hour: int, cron_minute: int, timezone: str = 'Asia/Shanghai'):
        """
        动态更新每日总结定时任务配置

        Args:
            enabled: 是否启用
            cron_hour: 执行时间（小时）
            cron_minute: 执行时间（分钟）
            timezone: 时区
        """
        from apscheduler.triggers.cron import CronTrigger

        try:
            if enabled:
                self.scheduler.add_job(
                    self._generate_daily_summary_job,
                    trigger=CronTrigger(hour=cron_hour, minute=cron_minute, timezone=timezone),
                    id="daily_summary_task",
                    name="Generate Daily Summary",
                    replace_existing=True,
                )
                logger.info(f"Daily summary schedule updated: {cron_hour}:{cron_minute:02d} {timezone}")
            else:
                try:
                    self.scheduler.remove_job('daily_summary_task')
                except Exception:
                    pass  # 任务可能不存在，忽略错误
                logger.info("Daily summary task disabled")
        except Exception as e:
            logger.error(f"Failed to update daily summary schedule: {e}", exc_info=True)

    def update_resource_metrics_schedule(self, interval_minutes: int = 1):
        """
        动态更新 NPU 指标采集间隔

        Args:
            interval_minutes: 采集间隔（分钟）
        """
        try:
            self.scheduler.add_job(
                self._collect_resource_metrics_job,
                trigger=IntervalTrigger(minutes=interval_minutes),
                id="resource_metrics_collect",
                name="Resource Metrics Collect",
                replace_existing=True,
            )
            logger.info(f"Resource metrics collection schedule updated: every {interval_minutes} minutes")
        except Exception as e:
            logger.error(f"Failed to update resource metrics schedule: {e}", exc_info=True)

    async def trigger_manual_sync(
        self,
        sync_type: str = "ci",
        days_back: int = 7,
        max_runs_per_workflow: int = 100,
        force_full_refresh: bool = False,
    ) -> dict:
        """
        手动触发同步

        Args:
            sync_type: 同步类型 ("ci")
            days_back: 从多少天前开始采集
            max_runs_per_workflow: 每个 workflow 最多采集多少条记录
            force_full_refresh: 是否强制全量覆盖刷新

        Returns:
            同步结果信息
        """
        logger.info(f"Manual sync triggered: {sync_type}, days_back={days_back}, max_runs={max_runs_per_workflow}, force={force_full_refresh}")

        if sync_type != "ci":
            return {
                "success": False,
                "message": f"Unsupported sync type: {sync_type}",
            }

        if not self.github_client:
            self._initialize_github_client()

        async with SessionLocal() as db:
            try:
                collector = CICollector(
                    github_client=self.github_client,  # type: ignore
                    db_session=db,
                )

                collected = await collector.collect_workflow_runs(
                    days_back=days_back,
                    max_runs_per_workflow=max_runs_per_workflow,
                    force_full_refresh=force_full_refresh,
                )

                # 同步完成后，更新进度
                from app.services.sync_progress import get_sync_progress
                progress = get_sync_progress()
                progress.complete()

                # 同步完成后，更新所有启用的 workflow 的 last_sync_at
                from sqlalchemy import update

                from app.models import WorkflowConfig

                await db.execute(
                    update(WorkflowConfig)
                    .where(WorkflowConfig.enabled == True)
                    .values(last_sync_at=datetime.now(UTC))
                )
                await db.commit()

                return {
                    "success": True,
                    "message": f"Successfully collected {collected} CI runs",
                    "collected_count": collected,
                }

            except Exception as e:
                logger.error(f"Manual sync failed: {e}", exc_info=True)
                # async with 会自动 rollback 和 close
                raise

    def get_next_run_time(self, job_id: str) -> datetime | None:
        """
        获取任务下次执行时间
        
        Args:
            job_id: 任务 ID
            
        Returns:
            下次执行时间，任务不存在时返回 None
        """
        job = self.scheduler.get_job(job_id)
        if job:
            return job.next_run_time
        return None

    def get_job_info(self) -> list[dict]:
        """
        获取所有任务信息
        
        Returns:
            任务信息列表
        """
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            })
        return jobs


# 全局调度器实例
_scheduler: DataSyncScheduler | None = None


def get_scheduler() -> DataSyncScheduler:
    """获取全局调度器实例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = DataSyncScheduler()
    return _scheduler


def start_scheduler() -> None:
    """启动全局调度器"""
    scheduler = get_scheduler()
    scheduler.start()


async def stop_scheduler_async() -> None:
    """停止全局调度器并清理资源（异步版本）"""
    global _scheduler
    if _scheduler:
        await _scheduler.close()
        _scheduler = None


def stop_scheduler() -> None:
    """停止全局调度器（同步版本，不关闭 GitHub 客户端）"""
    global _scheduler
    if _scheduler:
        _scheduler.stop()
        # 注意：同步版本无法关闭异步的 GitHub 客户端
        # 建议使用 stop_scheduler_async
