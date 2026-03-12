"""Artifact cleanup -- removes old execution artifacts from disk."""

import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Execution

logger = logging.getLogger(__name__)


async def cleanup_old_artifacts(
    session_factory: async_sessionmaker[AsyncSession],
    retention_days: int,
    screenshot_dir: str,
    download_dir: str,
) -> int:
    """Delete artifact directories for executions older than retention_days.

    Returns the number of directories removed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    async with session_factory() as db:
        result = await db.execute(
            select(Execution.id).where(Execution.created_at < cutoff)
        )
        old_ids = [str(row[0]) for row in result.all()]

    if not old_ids:
        return 0

    removed = 0
    for exec_id in old_ids:
        for base_dir in (screenshot_dir, download_dir):
            exec_dir = os.path.join(base_dir, exec_id)
            if await asyncio.to_thread(os.path.isdir, exec_dir):
                await asyncio.to_thread(shutil.rmtree, exec_dir, ignore_errors=True)
                removed += 1

    logger.info("Cleaned up %d artifact directories for %d old executions", removed, len(old_ids))
    return removed
