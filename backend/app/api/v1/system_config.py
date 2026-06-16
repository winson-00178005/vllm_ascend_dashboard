"""
系统配置 API 路由
提供系统配置的查看和更新功能
"""
import logging
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from apscheduler.triggers.interval import IntervalTrigger

from app.api.deps import get_current_active_admin_user, get_current_active_super_admin_user, get_current_user, get_db
from app.core.config import settings
from app.core.email import SMTP_CONFIG_KEY
from app.models import ProjectDashboardConfig, User

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def get_system_config(
    current_user: Annotated[User, Depends(get_current_active_super_admin_user)]
):
    """
    获取系统配置

    需要超级管理员权限（super_admin）
    返回可公开的配置信息（敏感数据如 token 会脱敏显示）
    """
    # 时区配置从数据库读取，默认 Asia/Shanghai
    from sqlalchemy import select
    from app.models import ProjectDashboardConfig
    from app.db.base import SessionLocal
    
    timezone_str = 'Asia/Shanghai'  # 默认时区
    try:
        async with SessionLocal() as db:
            stmt = select(ProjectDashboardConfig).where(
                ProjectDashboardConfig.config_key == 'daily_summary_schedule'
            )
            result = await db.execute(stmt)
            config = result.scalar_one_or_none()
            if config and 'timezone' in config.config_value:
                timezone_str = config.config_value['timezone']
    except Exception as e:
        logger.warning(f"Failed to load timezone from database, using default: {timezone_str}. Error: {e}")
    
    # 只返回非敏感配置
    return {
        "app_config": {
            "environment": settings.ENVIRONMENT,
            "debug": settings.DEBUG,
            "log_level": settings.LOG_LEVEL,
            "timezone": timezone_str,  # 从数据库读取
        },
        "github_config": {
            "owner": settings.GITHUB_OWNER,
            "repo": settings.GITHUB_REPO,
            "token_configured": bool(settings.GITHUB_TOKEN),  # 只返回是否配置
            "token_preview": settings.GITHUB_TOKEN[:8] + "..." if settings.GITHUB_TOKEN and len(settings.GITHUB_TOKEN) > 8 else None,
        },
        "sync_config": {
            "ci_sync_config": {
                "sync_interval_minutes": settings.CI_SYNC_INTERVAL_MINUTES,
                "days_back": settings.CI_SYNC_DAYS_BACK,
                "max_runs_per_workflow": settings.CI_SYNC_MAX_RUNS_PER_WORKFLOW,
                "force_full_refresh": settings.CI_SYNC_FORCE_FULL_REFRESH,
            },
            "model_sync_config": {
                "sync_interval_minutes": settings.MODEL_SYNC_INTERVAL_MINUTES,
                "days_back": settings.MODEL_SYNC_DAYS_BACK,
                "runs_limit": settings.MODEL_SYNC_RUNS_LIMIT,
            },
            "data_retention_days": settings.DATA_RETENTION_DAYS,
            "project_dashboard_cache_interval_minutes": settings.PROJECT_DASHBOARD_CACHE_INTERVAL_MINUTES,
            "github_cache_dir": settings.GITHUB_CACHE_DIR,
        },
        "database_config": {
            "type": "sqlite" if "sqlite" in settings.DATABASE_URL else "mysql",
            "configured": bool(settings.DATABASE_URL),
        },
    }


@router.put("/app")
async def update_app_config(
    log_level: str | None = Query(None),
    debug: bool | None = Query(None),
    current_user: Annotated[User, Depends(get_current_active_super_admin_user)] = None
):
    """
    更新应用配置

    需要超级管理员权限（super_admin）
    同时更新运行时配置和 .env 文件
    """
    from app.core.config_manager import update_env_config
    import logging

    updates = []
    env_updates = {}

    if log_level is not None:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if log_level.upper() not in valid_levels:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"日志级别必须是：{', '.join(valid_levels)}",
            )
        
        # 更新运行时配置
        old_level = settings.LOG_LEVEL
        settings.LOG_LEVEL = log_level.upper()
        env_updates['log_level'] = log_level.upper()
        updates.append(f"日志级别：{old_level} → {log_level.upper()}")
        
        # 动态调整 logging 级别（无需重启）
        try:
            new_level = getattr(logging, log_level.upper())
            root_logger = logging.getLogger()
            root_logger.setLevel(new_level)
            
            # 更新所有 handler 的级别
            for handler in root_logger.handlers:
                handler.setLevel(new_level)
            
            updates.append("✅ 日志级别已动态调整（无需重启）")
            logger.info(f"Log level dynamically changed from {old_level} to {log_level.upper()}")
        except Exception as e:
            logger.error(f"Failed to dynamically adjust log level: {e}")
            updates.append("⚠️ 日志级别已更新，但需要重启服务才能生效")

    if debug is not None:
        settings.DEBUG = debug
        env_updates['debug'] = debug
        updates.append(f"调试模式：{'开启' if debug else '关闭'}")

    # 同步更新 .env 文件
    if env_updates:
        try:
            success = update_env_config(env_updates)
            if success:
                updates.append(".env 文件已更新")
            else:
                logger.warning("Failed to update .env file")
        except Exception as e:
            logger.error(f"Failed to update .env file: {e}")

    return {
        "success": True,
        "message": "配置已更新：" + ", ".join(updates) if updates else "无更新",
        "updates": updates,
    }


@router.put("/github")
async def update_github_config(
    github_token: str | None = Query(None),
    current_user: Annotated[User, Depends(get_current_active_super_admin_user)] = None
):
    """
    更新 GitHub 配置

    需要超级管理员权限（super_admin）
    同时更新运行时配置和 .env 文件

    注意：GitHub 项目固定为 vllm-project/vllm-ascend，不可修改
    """
    from app.core.config_manager import update_env_config

    updates = []
    env_updates = {}

    if github_token is not None and len(github_token.strip()) > 0:
        # 验证 token 格式（GitHub token 通常以 ghp_ 或 github_pat_ 开头）
        token = github_token.strip()
        if not (token.startswith("ghp_") or token.startswith("github_pat_")) and len(token) < 10:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="GitHub Token 格式不正确，应以 ghp_ 或 github_pat_ 开头",
            )
        settings.GITHUB_TOKEN = token
        env_updates['github_token'] = token
        updates.append("GitHub Token 已更新")

    # 同步更新 .env 文件
    if env_updates:
        try:
            success = update_env_config(env_updates)
            if success:
                updates.append(".env 文件已更新")
            else:
                logger.warning("Failed to update .env file")
        except Exception as e:
            logger.error(f"Failed to update .env file: {e}")

    return {
        "success": True,
        "message": "配置已更新：" + ", ".join(updates) if updates else "无更新",
        "updates": updates,
    }


@router.put("/sync")
async def update_sync_config(
    ci_sync_interval_minutes: int | None = Query(None),
    ci_sync_days_back: int | None = Query(None),
    ci_sync_max_runs_per_workflow: int | None = Query(None),
    ci_sync_force_full_refresh: bool | None = Query(None),
    model_sync_interval_minutes: int | None = Query(None),
    model_sync_days_back: int | None = Query(None),
    model_sync_runs_limit: int | None = Query(None),
    data_retention_days: int | None = Query(None),
    project_dashboard_cache_interval_minutes: int | None = Query(None),
    github_cache_dir: str | None = Query(None),
    current_user: Annotated[User, Depends(get_current_active_super_admin_user)] = None
):
    """
    更新同步配置

    需要超级管理员权限（super_admin）
    同时更新运行时配置和 .env 文件
    """
    from app.core.config_manager import update_env_config
    from app.services.scheduler import get_scheduler

    updates = []
    env_updates = {}

    # 验证参数
    if ci_sync_interval_minutes is not None:
        if ci_sync_interval_minutes < 1 or ci_sync_interval_minutes > 10080:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="同步间隔必须在 1-10080 分钟之间",
            )

    if ci_sync_days_back is not None:
        if ci_sync_days_back < 1 or ci_sync_days_back > 90:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="同步天数必须在 1-90 天之间",
            )

    if ci_sync_max_runs_per_workflow is not None:
        if ci_sync_max_runs_per_workflow < 1 or ci_sync_max_runs_per_workflow > 1000:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="每个 Workflow 最多采集数量必须在 1-1000 之间",
            )

    if model_sync_interval_minutes is not None:
        if model_sync_interval_minutes < 1 or model_sync_interval_minutes > 10080:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="模型同步间隔必须在 1-10080 分钟之间",
            )

    if model_sync_days_back is not None:
        if model_sync_days_back < 1 or model_sync_days_back > 90:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="模型同步天数必须在 1-90 天之间",
            )

    if model_sync_runs_limit is not None:
        if model_sync_runs_limit < 1 or model_sync_runs_limit > 1000:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="每个 Workflow 最多获取 Runs 数量必须在 1-1000 之间",
            )

    if data_retention_days is not None:
        if data_retention_days < 1 or data_retention_days > 3650:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="数据保留天数必须在 1-3650 天之间",
            )

    if project_dashboard_cache_interval_minutes is not None:
        if project_dashboard_cache_interval_minutes < 1 or project_dashboard_cache_interval_minutes > 1440:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project Dashboard 缓存更新间隔必须在 1-1440 分钟之间",
            )

    if github_cache_dir is not None:
        settings.GITHUB_CACHE_DIR = github_cache_dir
        env_updates['github_cache_dir'] = github_cache_dir
        updates.append(f"GitHub 缓存目录：{github_cache_dir or '默认 (data/)'}")

    # 更新配置（运行时）
    if ci_sync_interval_minutes is not None:
        settings.CI_SYNC_INTERVAL_MINUTES = ci_sync_interval_minutes
        env_updates['ci_sync_interval_minutes'] = ci_sync_interval_minutes
        updates.append(f"CI 同步间隔：{ci_sync_interval_minutes}分钟")

        # 重新配置调度器
        try:
            scheduler = get_scheduler()
            # 显式移除旧任务再添加，确保 Trigger 完全更新
            try:
                scheduler.scheduler.remove_job('ci_data_sync')
            except Exception:
                pass
            scheduler.scheduler.add_job(
                scheduler._sync_ci_data_job,
                trigger=IntervalTrigger(minutes=ci_sync_interval_minutes),
                id='ci_data_sync',
                name='CI Data Sync',
            )
            updates.append("调度器已重新配置")
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"重新配置调度器失败：{str(e)}",
            )

    if ci_sync_days_back is not None:
        settings.CI_SYNC_DAYS_BACK = ci_sync_days_back
        env_updates['ci_sync_days_back'] = ci_sync_days_back
        updates.append(f"同步天数范围：{ci_sync_days_back}天")

    if ci_sync_max_runs_per_workflow is not None:
        settings.CI_SYNC_MAX_RUNS_PER_WORKFLOW = ci_sync_max_runs_per_workflow
        env_updates['ci_sync_max_runs_per_workflow'] = ci_sync_max_runs_per_workflow
        updates.append(f"每个 Workflow 最多采集：{ci_sync_max_runs_per_workflow}条")

    if ci_sync_force_full_refresh is not None:
        settings.CI_SYNC_FORCE_FULL_REFRESH = ci_sync_force_full_refresh
        env_updates['ci_sync_force_full_refresh'] = ci_sync_force_full_refresh
        updates.append(f"全量覆盖刷新：{'开启' if ci_sync_force_full_refresh else '关闭'}")

    if data_retention_days is not None:
        settings.DATA_RETENTION_DAYS = data_retention_days
        env_updates['data_retention_days'] = data_retention_days
        updates.append(f"数据保留天数：{data_retention_days}天")

    if project_dashboard_cache_interval_minutes is not None:
        settings.PROJECT_DASHBOARD_CACHE_INTERVAL_MINUTES = project_dashboard_cache_interval_minutes
        env_updates['project_dashboard_cache_interval_minutes'] = project_dashboard_cache_interval_minutes
        updates.append(f"Project Dashboard 缓存更新间隔：{project_dashboard_cache_interval_minutes}分钟")

        # 重新配置调度器
        try:
            scheduler = get_scheduler()
            try:
                scheduler.scheduler.remove_job('project_dashboard_cache_update')
            except Exception:
                pass
            scheduler.scheduler.add_job(
                scheduler._update_project_dashboard_cache_job,
                trigger=IntervalTrigger(minutes=project_dashboard_cache_interval_minutes),
                id='project_dashboard_cache_update',
                name='Project Dashboard Cache Update',
            )
            updates.append("Project Dashboard 调度器已重新配置")
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"重新配置调度器失败：{str(e)}",
            )

    # 模型同步配置更新
    if model_sync_interval_minutes is not None:
        settings.MODEL_SYNC_INTERVAL_MINUTES = model_sync_interval_minutes
        env_updates['model_sync_interval_minutes'] = model_sync_interval_minutes
        updates.append(f"模型同步间隔：{model_sync_interval_minutes}分钟")

        # 重新配置调度器
        try:
            scheduler = get_scheduler()
            try:
                scheduler.scheduler.remove_job('model_report_sync')
            except Exception:
                pass
            scheduler.scheduler.add_job(
                scheduler._sync_model_reports_job,
                trigger=IntervalTrigger(minutes=model_sync_interval_minutes),
                id='model_report_sync',
                name='Model Report Sync',
            )
            updates.append("模型同步调度器已重新配置")
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"重新配置调度器失败：{str(e)}",
            )

    if model_sync_days_back is not None:
        settings.MODEL_SYNC_DAYS_BACK = model_sync_days_back
        env_updates['model_sync_days_back'] = model_sync_days_back
        updates.append(f"模型同步天数范围：{model_sync_days_back}天")

    if model_sync_runs_limit is not None:
        settings.MODEL_SYNC_RUNS_LIMIT = model_sync_runs_limit
        env_updates['model_sync_runs_limit'] = model_sync_runs_limit
        updates.append(f"每个 Workflow 最多获取 Runs: {model_sync_runs_limit}条")

    if github_cache_dir is not None:
        settings.GITHUB_CACHE_DIR = github_cache_dir
        env_updates['github_cache_dir'] = github_cache_dir
        updates.append(f"GitHub 缓存目录：{github_cache_dir or '默认 (data/)'}")

    # 同步更新 .env 文件
    if env_updates:
        try:
            success = update_env_config(env_updates)
            if success:
                updates.append(".env 文件已更新")
            else:
                logger.warning("Failed to update .env file")
        except Exception as e:
            logger.error(f"Failed to update .env file: {e}")

    return {
        "success": True,
        "message": "配置已更新：" + ", ".join(updates) if updates else "无更新",
        "updates": updates,
    }


@router.post("/sync/trigger")
async def trigger_sync_config(
    current_user: Annotated[User, Depends(get_current_active_super_admin_user)] = None
):
    """
    手动触发配置重载

    需要超级管理员权限（super_admin）
    从环境变量重新加载配置
    """

    try:
        # 重新读取环境变量
        # 注意：Python 中无法直接重载已导入的模块配置
        # 这里只返回当前配置状态
        return {
            "success": True,
            "message": "配置重载功能受限，请通过重启服务应用新配置",
            "current_config": {
                "ci_sync_interval_minutes": settings.CI_SYNC_INTERVAL_MINUTES,
                "data_retention_days": settings.DATA_RETENTION_DAYS,
            },
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"配置重载失败：{str(e)}",
        )


@router.get("/status")
async def get_system_status(
    current_user: Annotated[User, Depends(get_current_user)] = None
):
    """
    获取系统状态信息（所有登录用户可访问）
    """
    from datetime import datetime

    from sqlalchemy import func, select

    from app.db.base import SessionLocal
    from app.models import ModelSyncConfig, WorkflowConfig
    from app.services.scheduler import get_scheduler

    scheduler = get_scheduler()

    # 获取 CI 同步任务状态
    ci_job = scheduler.scheduler.get_job('ci_data_sync')
    ci_next_sync = ci_job.next_run_time if ci_job else None

    # 获取模型报告同步任务状态
    model_job = scheduler.scheduler.get_job('model_report_sync')
    model_next_sync = model_job.next_run_time if model_job else None

    # 获取 Project Dashboard 缓存更新任务状态
    cache_job = scheduler.scheduler.get_job('project_dashboard_cache_update')
    cache_next_sync = cache_job.next_run_time if cache_job else None

    # 获取项目动态同步任务状态
    daily_summary_job = scheduler.scheduler.get_job('daily_summary_task')
    daily_summary_next_sync = daily_summary_job.next_run_time if daily_summary_job else None

    # 获取所有启用的 workflow 中最新的 last_sync_at
    async with SessionLocal() as db:
        try:
            stmt = select(func.max(WorkflowConfig.last_sync_at)).where(WorkflowConfig.enabled == True)
            result = await db.execute(stmt)
            last_sync = result.scalar()
        except Exception as e:
            logger.warning(f"Failed to get last sync time: {e}")
            last_sync = None

    return {
        "scheduler": {
            "running": scheduler.scheduler.running,
            "sync_interval_minutes": settings.CI_SYNC_INTERVAL_MINUTES,
            "last_sync": last_sync.isoformat() if last_sync else None,  # 上次同步时间
            "tasks": {
                "ci_sync": {
                    "name": "CI 数据同步",
                    "next_sync": ci_next_sync.isoformat() if ci_next_sync else None,
                    "interval_minutes": settings.CI_SYNC_INTERVAL_MINUTES,
                },
                "model_report_sync": {
                    "name": "模型报告同步",
                    "next_sync": model_next_sync.isoformat() if model_next_sync else None,
                    "interval_minutes": settings.MODEL_SYNC_INTERVAL_MINUTES,
                },
                "project_dashboard_cache": {
                    "name": "Git 仓库缓存更新",
                    "next_sync": cache_next_sync.isoformat() if cache_next_sync else None,
                    "interval_minutes": settings.PROJECT_DASHBOARD_CACHE_INTERVAL_MINUTES,
                },
                "daily_summary": {
                    "name": "项目动态同步",
                    "next_sync": daily_summary_next_sync.isoformat() if daily_summary_next_sync else None,
                    "enabled": getattr(settings, 'DAILY_SUMMARY_ENABLED', True),
                    "cron_hour": getattr(settings, 'DAILY_SUMMARY_CRON_HOUR', 8),
                    "cron_minute": getattr(settings, 'DAILY_SUMMARY_CRON_MINUTE', 0),
                },
            },
        },
        "database": {
            "connected": True,  # 如果能响应说明数据库连接正常
            "type": "sqlite" if "sqlite" in settings.DATABASE_URL else "mysql",
        },
        "github": {
            "configured": bool(settings.GITHUB_TOKEN),
            "owner": settings.GITHUB_OWNER,
            "repo": settings.GITHUB_REPO,
        },
        "timestamp": datetime.now(UTC).isoformat(),
    }


@router.get("/git-cache/status")
async def get_git_cache_status(
    current_user: Annotated[User, Depends(get_current_active_super_admin_user)]
):
    """
    获取 Git 缓存状态

    需要超级管理员权限（super_admin）
    返回最新 commit 信息，方便管理员判断是否需要手动同步
    支持多个仓库：vllm-ascend 和 vllm
    """
    from app.services.github_cache import get_github_cache, get_github_cache_for_repo

    # 获取默认仓库（vllm-ascend）的缓存状态
    ascend_cache = get_github_cache()
    ascend_commit = ascend_cache.get_latest_commit()

    # 获取 vllm 仓库的缓存状态
    vllm_cache = get_github_cache_for_repo(owner="vllm-project", repo="vllm")
    vllm_commit = vllm_cache.get_latest_commit()

    return {
        "repositories": {
            "ascend": {
                "owner": ascend_cache.owner,
                "repo": ascend_cache.repo,
                "latest_commit": ascend_commit,
                "cache_dir": str(ascend_cache.cache_dir),
                "is_cloned": ascend_cache._is_repo_cloned(),
            },
            "vllm": {
                "owner": vllm_cache.owner,
                "repo": vllm_cache.repo,
                "latest_commit": vllm_commit,
                "cache_dir": str(vllm_cache.cache_dir),
                "is_cloned": vllm_cache._is_repo_cloned(),
            },
        },
        # 保持向后兼容
        "latest_commit": ascend_commit,
        "cache_dir": str(ascend_cache.cache_dir),
        "is_cloned": ascend_cache._is_repo_cloned(),
    }


@router.post("/git-cache/sync")
async def sync_git_cache(
    current_user: Annotated[User, Depends(get_current_active_super_admin_user)],
    repo_type: str = Query("all", description="仓库类型: ascend, vllm, all"),
):
    """
    同步 Git 缓存

    支持同步指定仓库或所有仓库
    """
    from app.services.github_cache import get_github_cache, get_github_cache_for_repo, ensure_repo_cloned, update_repo

    results = []

    if repo_type == "all" or repo_type == "ascend":
        ascend_cache = get_github_cache()
        if not ascend_cache._is_repo_cloned():
            success = ascend_cache.clone()
            results.append({
                "repo": "ascend",
                "action": "clone",
                "success": success,
                "message": "克隆成功" if success else "克隆失败",
            })
        else:
            success = ascend_cache.pull()
            results.append({
                "repo": "ascend",
                "action": "pull",
                "success": success,
                "message": "更新成功" if success else "更新失败",
            })

    if repo_type == "all" or repo_type == "vllm":
        vllm_cache = get_github_cache_for_repo(owner="vllm-project", repo="vllm")
        if not vllm_cache._is_repo_cloned():
            success = vllm_cache.clone()
            results.append({
                "repo": "vllm",
                "action": "clone",
                "success": success,
                "message": "克隆成功" if success else "克隆失败",
            })
        else:
            success = vllm_cache.pull()
            results.append({
                "repo": "vllm",
                "action": "pull",
                "success": success,
                "message": "更新成功" if success else "更新失败",
            })

    return {
        "success": all(r["success"] for r in results),
        "results": results,
    }


# ============ 每日总结配置相关 API ============

@router.get("/daily-summary")
async def get_daily_summary_config(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """
    获取每日总结配置

    所有登录用户可访问
    """
    from app.models.daily_summary import LLMProviderConfig
    from app.models import ProjectDashboardConfig

    try:
        # 获取定时任务配置
        schedule_stmt = select(ProjectDashboardConfig).where(
            ProjectDashboardConfig.config_key == 'daily_summary_schedule'
        )
        schedule_result = await db.execute(schedule_stmt)
        schedule_config = schedule_result.scalar_one_or_none()
        
        # 获取项目配置
        projects_stmt = select(ProjectDashboardConfig).where(
            ProjectDashboardConfig.config_key == 'daily_summary_projects'
        )
        projects_result = await db.execute(projects_stmt)
        projects_config = projects_result.scalar_one_or_none()
        
        # 获取 LLM 提供商列表
        llm_stmt = select(LLMProviderConfig).order_by(LLMProviderConfig.display_order)
        llm_result = await db.execute(llm_stmt)
        llm_providers = llm_result.scalars().all()
        
        # 检查 API Key 是否配置
        import os
        llm_list = []
        for llm in llm_providers:
            llm_list.append({
                "provider": llm.provider,
                "display_name": llm.display_name,
                "default_model": llm.default_model,
                "enabled": llm.enabled,
                "is_active": llm.is_active,
                "display_order": llm.display_order,
                "api_key_configured": bool(llm.api_key),
            })
        
        return {
            "enabled": schedule_config.config_value.get('enabled', True) if schedule_config else True,
            "cron_hour": schedule_config.config_value.get('cron_hour', 8) if schedule_config else 8,
            "cron_minute": schedule_config.config_value.get('cron_minute', 0) if schedule_config else 0,
            "timezone": schedule_config.config_value.get('timezone', 'Asia/Shanghai') if schedule_config else 'Asia/Shanghai',
            "llm_providers": llm_list,
            "projects": projects_config.config_value if projects_config else [
                {"id": "ascend", "name": "vLLM Ascend", "enabled": True},
                {"id": "vllm", "name": "vLLM", "enabled": True},
            ],
        }
    except Exception as e:
        logger.error(f"Failed to get daily summary config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取配置失败：{str(e)}"
        )


@router.put("/daily-summary")
async def update_daily_summary_config(
    config: dict,
    current_user: Annotated[User, Depends(get_current_active_super_admin_user)],
    db: AsyncSession = Depends(get_db)
):
    """
    更新每日总结配置

    需要管理员权限
    """
    from app.models import ProjectDashboardConfig

    try:
        # 更新定时任务配置
        if 'enabled' in config or 'cron_hour' in config or 'cron_minute' in config or 'timezone' in config:
            schedule_stmt = select(ProjectDashboardConfig).where(
                ProjectDashboardConfig.config_key == 'daily_summary_schedule'
            )
            schedule_result = await db.execute(schedule_stmt)
            schedule_config = schedule_result.scalar_one_or_none()
            
            if schedule_config:
                schedule_config.config_value.update({
                    'enabled': config.get('enabled', True),
                    'cron_hour': config.get('cron_hour', 8),
                    'cron_minute': config.get('cron_minute', 0),
                    'timezone': config.get('timezone', 'Asia/Shanghai'),
                })
            else:
                schedule_config = ProjectDashboardConfig(
                    config_key='daily_summary_schedule',
                    config_value={
                        'enabled': config.get('enabled', True),
                        'cron_hour': config.get('cron_hour', 8),
                        'cron_minute': config.get('cron_minute', 0),
                        'timezone': config.get('timezone', 'Asia/Shanghai'),
                    },
                    description='每日总结定时任务配置',
                )
                db.add(schedule_config)
        
        # 更新项目配置
        if 'projects' in config:
            projects_stmt = select(ProjectDashboardConfig).where(
                ProjectDashboardConfig.config_key == 'daily_summary_projects'
            )
            projects_result = await db.execute(projects_stmt)
            projects_config = projects_result.scalar_one_or_none()
            
            if projects_config:
                projects_config.config_value = config['projects']
            else:
                projects_config = ProjectDashboardConfig(
                    config_key='daily_summary_projects',
                    config_value=config['projects'],
                    description='每日总结项目配置',
                )
                db.add(projects_config)
        
        await db.commit()

        # 更新调度器
        try:
            from app.services.scheduler import get_scheduler
            scheduler = get_scheduler()
            scheduler.update_daily_summary_schedule(
                enabled=config.get('enabled', True),
                cron_hour=config.get('cron_hour', 8),
                cron_minute=config.get('cron_minute', 0),
                timezone=config.get('timezone', 'Asia/Shanghai'),
            )
            logger.info("Daily summary scheduler updated successfully")
        except Exception as e:
            logger.error(f"Failed to update daily summary scheduler: {e}")

        return {
            "success": True,
            "message": "配置已更新",
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update daily summary config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新配置失败：{str(e)}"
        )


@router.get("/llm-providers")
async def get_llm_providers(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db)
):
    """
    获取 LLM 提供商列表

    所有登录用户可访问
    """
    from app.models.daily_summary import LLMProviderConfig
    try:
        stmt = select(LLMProviderConfig).order_by(LLMProviderConfig.display_order)
        result = await db.execute(stmt)
        providers = result.scalars().all()

        return [
            {
                "provider": p.provider,
                "display_name": p.display_name,
                "default_model": p.default_model,
                "enabled": p.enabled,
                "is_active": p.is_active,
                "display_order": p.display_order,
                "api_key_configured": bool(p.api_key),
                "api_key_preview": p.api_key[:8] + "..." + p.api_key[-4:] if p.api_key and len(p.api_key) >= 12 else None,
                "api_base_url": p.api_base_url,
            }
            for p in providers
        ]
    except Exception as e:
        logger.error(f"Failed to get LLM providers: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取 LLM 提供商失败：{str(e)}"
        )


@router.put("/llm-providers/{provider}")
async def update_llm_provider(
    provider: str,
    config: dict,
    current_user: Annotated[User, Depends(get_current_active_super_admin_user)],
    db: AsyncSession = Depends(get_db)
):
    """
    更新 LLM 提供商配置

    需要管理员权限

    可更新字段：
    - enabled: 是否启用
    - is_active: 是否为当前激活的提供商（用于 AI 总结）
    - default_model: 默认模型
    - display_name: 显示名称
    - api_key: API Key
    - api_base_url: API 基础 URL
    """
    from app.models.daily_summary import LLMProviderConfig

    try:
        stmt = select(LLMProviderConfig).where(LLMProviderConfig.provider == provider)
        result = await db.execute(stmt)
        provider_config = result.scalar_one_or_none()

        if not provider_config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"LLM 提供商 {provider} 不存在"
            )

        # 更新字段
        if 'enabled' in config:
            provider_config.enabled = config['enabled']

        if 'is_active' in config:
            # 如果设置为激活，需要先取消其他所有 provider 的激活状态
            if config['is_active']:
                deactivate_stmt = select(LLMProviderConfig).where(
                    LLMProviderConfig.is_active == True
                )
                deactivate_result = await db.execute(deactivate_stmt)
                active_providers = deactivate_result.scalars().all()
                for active_p in active_providers:
                    active_p.is_active = False
            provider_config.is_active = config['is_active']

        if 'default_model' in config:
            provider_config.default_model = config['default_model']

        if 'display_name' in config:
            provider_config.display_name = config['display_name']

        if 'api_key' in config:
            provider_config.api_key = config['api_key']

        if 'api_base_url' in config:
            provider_config.api_base_url = config['api_base_url']

        await db.commit()

        return {
            "success": True,
            "message": f"LLM 提供商 {provider} 配置已更新",
            "data": {
                "provider": provider_config.provider,
                "is_active": provider_config.is_active,
                "api_key_configured": bool(provider_config.api_key),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update LLM provider: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新 LLM 提供商失败：{str(e)}"
        )


# ============ 系统提示词配置 API ============


def get_system_prompt_scope_config(scope: str) -> tuple[str, dict[str, str], str]:
    if scope == "ci_failure_analysis":
        return (
            "ci_failure_analysis_system_prompt",
            {
                "default": """你是一名专业的 CI/CD 失败诊断分析师。请根据以下 CI Job 失败信息，进行根因分析并给出改进建议。

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
3. 长期预防性改进建议""",
            },
            "系统提示词用于指导 AI 分析 CI 失败 Job 的根因和分类（基于 auto-bug-fixer 技能方法论 + CI 专属分类附录）",
        )
    if scope == "commit_analysis":
        return (
            "commit_analysis_system_prompt",
            {
                "ascend": """你是一名专业的 vLLM Ascend Commit 技术分析师。请基于给定的 Commit 元信息、关联 PR 信息、文件变更和已有人工分析字段，生成中文 Markdown 总结。

要求：
1. 概括该 Commit 做了什么
2. 判断修改类型：Feature / Bugfix / Refactor / Common / Test / CI / Other
3. 判断是否影响 API，并说明原因
4. 分析对 vLLM Ascend 的潜在影响；如不涉及，请明确说明“不涉及”
5. 给出后续跟进建议；如无需跟进，请明确说明“不涉及”
6. 输出结构清晰，避免编造未提供的信息""",
                "vllm": """你是一名专业的 vLLM Commit 技术分析师。请基于给定的 Commit 元信息、关联 PR 信息、文件变更和已有人工分析字段，生成中文 Markdown 总结。

要求：
1. 概括该 Commit 做了什么
2. 判断修改类型：Feature / Bugfix / Refactor / Common / Test / CI / Other
3. 判断是否影响 API，并说明原因
4. 分析对 vLLM Ascend 的潜在影响；如不涉及，请明确说明“不涉及”
5. 给出后续跟进建议；如无需跟进，请明确说明“不涉及”
6. 输出结构清晰，避免编造未提供的信息""",
            },
            "系统提示词用于指导 AI 生成单个 Commit 分析总结的风格和内容重点",
        )
    if scope != "daily_summary":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid system prompt scope: {scope}")
    return (
        "daily_summary_system_prompt",
        {
            "ascend": """你是一名专业的 vLLM Ascend 项目技术分析师。请根据以下数据生成项目动态总结和分析。

要求：
1. 总结 PR 趋势，包括热门修改领域、重要 PR 概述
2. 分析 Issue 热点，包括问题类型分布、用户反馈热点、需要关注的问题
3. 分析 Commit 活跃度，包括提交频率、代码变更热点
4. 综合以上信息，生成项目整体动态总结，包括：
   - 项目活跃度评估
   - 重要更新和里程碑
   - 需要关注的风险或问题
5. 使用 Markdown 格式，语言为中文
6. 结构清晰，重点突出，便于快速了解当日技术动态
7. 重点PR/ISSUE/COMMIT附带ID和github链接""",
            "vllm": """你是一名专业的 vLLM 项目分析师。请根据以下数据生成项目动态总结和分析。

要求：
1. 总结 PR 趋势，包括热门修改领域、重要 PR 概述
2. 分析 Issue 热点，包括问题类型分布、用户反馈热点、需要关注的问题
3. 分析 Commit 活跃度，包括提交频率、代码变更热点
4. 综合以上信息，生成项目整体动态总结，包括：
   - 项目活跃度评估
   - 重要更新和里程碑
   - 需要关注的风险或问题
5. 使用 Markdown 格式，语言为中文
6. 结构清晰，重点突出，便于快速了解当日技术动态
7. 重点PR/ISSUE/COMMIT附带ID和github链接""",
        },
        "系统提示词用于指导 AI 生成项目动态总结的风格和内容重点",
    )


@router.get("/system-prompt")
async def get_system_prompt_config(
    current_user: Annotated[User, Depends(get_current_user)],
    scope: str = "daily_summary",
    db: AsyncSession = Depends(get_db)
):
    """
    获取系统提示词配置

    所有登录用户可访问

    返回各项目的系统提示词配置：
    - ascend: vLLM Ascend 项目提示词
    - vllm: vLLM 项目提示词
    """
    from app.models import ProjectDashboardConfig

    try:
        config_key, default_prompts, default_description = get_system_prompt_scope_config(scope)
        stmt = select(ProjectDashboardConfig).where(
            ProjectDashboardConfig.config_key == config_key
        )
        result = await db.execute(stmt)
        config = result.scalar_one_or_none()

        if config and config.config_value:
            return {
                "prompts": config.config_value,
                "description": config.description,
            }

        return {
            "prompts": default_prompts,
            "description": default_description,
        }
    except Exception as e:
        logger.error(f"Failed to get system prompt config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取系统提示词配置失败：{str(e)}"
        )


@router.put("/system-prompt")
async def update_system_prompt_config(
    config: dict,
    current_user: Annotated[User, Depends(get_current_active_super_admin_user)],
    scope: str = "daily_summary",
    db: AsyncSession = Depends(get_db)
):
    """
    更新系统提示词配置

    需要管理员权限

    请求体示例：
    {
        "prompts": {
            "ascend": "自定义的 ascend 项目提示词...",
            "vllm": "自定义的 vllm 项目提示词..."
        }
    }
    """
    from app.models import ProjectDashboardConfig

    try:
        if 'prompts' not in config:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="请提供 prompts 配置"
            )

        config_key, _, default_description = get_system_prompt_scope_config(scope)
        stmt = select(ProjectDashboardConfig).where(
            ProjectDashboardConfig.config_key == config_key
        )
        result = await db.execute(stmt)
        prompt_config = result.scalar_one_or_none()

        if prompt_config:
            prompt_config.config_value = config['prompts']
            prompt_config.description = default_description
        else:
            prompt_config = ProjectDashboardConfig(
                config_key=config_key,
                config_value=config['prompts'],
                description=default_description,
            )
            db.add(prompt_config)

        await db.commit()

        return {
            "success": True,
            "message": "系统提示词配置已更新",
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to update system prompt config: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新系统提示词配置失败：{str(e)}"
        )


# ── SMTP 配置（独立于每日报告）──

@router.get("/smtp")
async def get_smtp_config_endpoint(
    current_user: Annotated[User, Depends(get_current_active_admin_user)],
    db: AsyncSession = Depends(get_db),
):
    """获取 SMTP 邮件外发服务器配置（admin 权限）"""
    from app.core.email import get_smtp_config as _read
    config = await _read(db)
    return {
        "smtp_host": config.get("smtp_host", ""),
        "smtp_port": config.get("smtp_port", 587),
        "smtp_username": config.get("smtp_username", ""),
        "smtp_use_tls": config.get("smtp_use_tls", True),
        "smtp_password_set": bool(config.get("smtp_password", "")),
        "from_email": config.get("from_email", ""),
    }


@router.put("/smtp")
async def update_smtp_config(
    config_update: dict,
    current_user: Annotated[User, Depends(get_current_active_super_admin_user)],
    db: AsyncSession = Depends(get_db),
):
    """更新 SMTP 邮件外发服务器配置（super_admin 权限）"""
    from app.core.email import get_smtp_config as _read

    current = await _read(db)
    for key in ("smtp_host", "smtp_port", "smtp_username", "smtp_password", "smtp_use_tls", "from_email"):
        if key in config_update:
            val = config_update[key]
            if key == "smtp_password" and not val:
                continue
            current[key] = val

    stmt = select(ProjectDashboardConfig).where(ProjectDashboardConfig.config_key == SMTP_CONFIG_KEY)
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row:
        row.config_value = current
    else:
        db.add(ProjectDashboardConfig(config_key=SMTP_CONFIG_KEY, config_value=current, description="SMTP 邮件服务器配置"))

    await db.commit()
    logger.info(f"SMTP config updated by {current_user.username}")
    return {
        "success": True,
        "message": "SMTP 配置已更新",
        "smtp_host": current.get("smtp_host", ""),
        "smtp_port": current.get("smtp_port", 587),
        "smtp_username": current.get("smtp_username", ""),
        "smtp_use_tls": current.get("smtp_use_tls", True),
        "smtp_password_set": bool(current.get("smtp_password", "")),
        "from_email": current.get("from_email", ""),
    }


@router.post("/smtp/test")
async def test_smtp_connection(
    current_user: Annotated[User, Depends(get_current_active_admin_user)],
    db: AsyncSession = Depends(get_db),
):
    """测试 SMTP 连通性（admin 权限）"""
    from app.core.email import get_smtp_config as _read
    import aiosmtplib
    from email.mime.text import MIMEText

    config = await _read(db)
    host = config.get("smtp_host", "")
    port = int(config.get("smtp_port", 587))
    username = config.get("smtp_username", "")
    password = config.get("smtp_password", "")
    use_tls = bool(config.get("smtp_use_tls", True))

    if not host:
        raise HTTPException(status_code=400, detail="SMTP 主机未配置")

    use_implicit = use_tls and port == 465
    use_starttls = use_tls and port != 465

    steps = []
    try:
        steps.append(f"连接 {host}:{port}...")
        smtp = aiosmtplib.SMTP(hostname=host, port=port, use_tls=use_implicit, timeout=15)
        await smtp.connect()
        steps.append("✓ 连接成功")

        if use_starttls:
            steps.append("启动 STARTTLS...")
            await smtp.starttls(validate_certs=False)
            steps.append("✓ STARTTLS 成功")

        if username and password:
            steps.append(f"登录 {username}...")
            await smtp.login(username, password)
            steps.append("✓ 登录成功")
        elif username:
            steps.append("(跳过登录，未提供密码)")

        steps.append("发送测试邮件...")
        msg = MIMEText("vLLM Ascend Dashboard SMTP 连通性测试", "plain", "utf-8")
        msg["Subject"] = "SMTP 连通性测试"
        msg["From"] = config.get("from_email", username or "test@localhost")
        msg["To"] = config.get("from_email", username or "test@localhost")
        await smtp.send_message(msg)
        steps.append("✓ 测试邮件发送成功")

        await smtp.quit()
        return {"success": True, "message": "SMTP 连通性测试通过", "steps": steps}
    except Exception as e:
        steps.append(f"✗ 失败: {e}")
        try:
            await smtp.quit()
        except Exception:
            pass
        return {"success": False, "message": f"测试失败: {e}", "steps": steps}
