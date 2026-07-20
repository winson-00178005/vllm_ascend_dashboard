"""增量回填历史 PR 的 body（描述）字段。

背景：pr_pipeline_collector.py 在 v0.0.33 之前构建 pr_data 时漏存了 body 字段，
导致已有的 619 个 PR 缺少描述，无法用于"发现问题数"推导。

本脚本：
  1. 扫描 pull_requests 表中 data JSON 缺少 body 字段的记录
  2. 调用 GitHub API get_pr_detail 获取 body
  3. 更新 data JSON，保留原有字段

使用方法：
    cd backend
    uv run python scripts/backfill_pr_body.py              # 全量回填
    uv run python scripts/backfill_pr_body.py --limit 50   # 只回填前 50 个
    uv run python scripts/backfill_pr_body.py --dry-run    # 仅检查不写入

注意：GitHub API 速率限制 5000 次/小时，619 个 PR 需要约 8 分钟。
"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from app.core.config import settings
from app.db.base import SessionLocal
from app.models import PullRequest
from app.services.github_client import GitHubClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _has_body(data: dict | str | None) -> bool:
    """检查 data JSON 中是否已有 body 字段。"""
    if not data:
        return False
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return False
    return isinstance(data, dict) and "body" in data


async def backfill(limit: int | None = None, dry_run: bool = False) -> dict:
    """回填缺失 body 的 PR 记录。

    Returns:
        统计字典 {scanned, missing_body, backfilled, failed, skipped}
    """
    gh = GitHubClient(
        token=settings.GITHUB_TOKEN,
        owner=settings.GITHUB_OWNER,
        repo=settings.GITHUB_REPO,
    ) if settings.GITHUB_TOKEN else None
    if not gh:
        raise RuntimeError("GITHUB_TOKEN 未配置，无法调用 GitHub API")

    stmt = select(PullRequest).order_by(PullRequest.created_at.desc())
    if limit:
        stmt = stmt.limit(limit)

    async with SessionLocal() as db:
        prs = list((await db.execute(stmt)).scalars().all())
        stats = {"scanned": len(prs), "missing_body": 0, "backfilled": 0, "failed": 0, "skipped": 0}

        for pr in prs:
            if _has_body(pr.data):
                stats["skipped"] += 1
                continue
            stats["missing_body"] += 1
            if dry_run:
                continue

            try:
                detail = await gh.get_pr_detail(pr.owner, pr.repo, pr.pr_number)
                body = detail.get("body") or ""
                # 合并到现有 data JSON
                existing = pr.data if isinstance(pr.data, dict) else {}
                if isinstance(pr.data, str):
                    try:
                        existing = json.loads(pr.data)
                    except (json.JSONDecodeError, TypeError):
                        existing = {}
                existing["body"] = body
                pr.data = existing
                stats["backfilled"] += 1
                if stats["backfilled"] % 50 == 0:
                    await db.commit()
                    logger.info("已回填 %d 个 PR body...", stats["backfilled"])
            except Exception as e:
                logger.warning(f"获取 PR #{pr.pr_number} body 失败: {e}")
                stats["failed"] += 1
                continue

        if not dry_run:
            await db.commit()

    logger.info("回填完成: %s", stats)
    return stats


def main():
    parser = argparse.ArgumentParser(description="回填历史 PR 的 body 字段")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个 PR")
    parser.add_argument("--dry-run", action="store_true", help="仅检查不写入")
    args = parser.parse_args()

    stats = asyncio.run(backfill(limit=args.limit, dry_run=args.dry_run))
    print(f"\n回填统计: {stats}")


if __name__ == "__main__":
    main()
