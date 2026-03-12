"""scout-engine -- workflow execution service for Scout browser automation."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from .api.artifacts import router as artifacts_router
from .api.auth import router as auth_router
from .api.executions import router as executions_router
from .api.schedules import router as schedules_router
from .api.workflows import router as workflows_router
from .auth import ApiKeyMiddleware
from .config import settings
from .services.scheduler import (
    add_or_replace_schedule,
    shutdown_scheduler,
    start_scheduler,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    start_scheduler()

    from app.database import async_session

    # Load existing enabled schedules from database so cron jobs survive restarts.
    # Wrapped in try/except so the app can still start on a fresh install where
    # the database has not been migrated yet.
    try:
        from app.models import Schedule
        from sqlalchemy import select

        async with async_session() as db:
            result = await db.execute(select(Schedule).where(Schedule.enabled == True))  # noqa: E712
            schedules = result.scalars().all()
            for sched in schedules:
                add_or_replace_schedule(
                    str(sched.id),
                    sched.cron_expression,
                    sched.timezone,
                )
            logger.info("Loaded %d schedule(s) from database", len(schedules))
    except Exception:
        logger.warning("Could not load schedules from database (migrations pending?)", exc_info=True)

    # Register artifact cleanup job if retention is enabled
    if settings.artifact_retention_days > 0:
        from app.services.cleanup import cleanup_old_artifacts
        from app.services.scheduler import scheduler

        scheduler.add_job(
            cleanup_old_artifacts,
            trigger="cron",
            hour=3,
            id="artifact_cleanup",
            kwargs={
                "session_factory": async_session,
                "retention_days": settings.artifact_retention_days,
                "screenshot_dir": settings.screenshot_dir,
                "download_dir": settings.download_dir,
            },
            replace_existing=True,
        )
        logger.info(
            "Artifact cleanup scheduled (retention=%d days)",
            settings.artifact_retention_days,
        )

    yield
    shutdown_scheduler()


app = FastAPI(
    title="scout-engine",
    description="Workflow execution service for Scout browser automation",
    version="0.1.0",
    lifespan=lifespan,
)


app.add_middleware(ApiKeyMiddleware)

app.include_router(artifacts_router)
app.include_router(auth_router)
app.include_router(executions_router)
app.include_router(schedules_router)
app.include_router(workflows_router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


def cli():
    """CLI entry point."""
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


async def _create_admin_key_impl(label: str) -> str:
    """Create an admin API key and return the raw key."""
    import app.database as app_database
    from app.models import ApiKey
    from app.services.keys import generate_api_key, hash_key

    raw_key = generate_api_key()
    async with app_database.async_session() as session:
        key = ApiKey(
            key_hash=hash_key(raw_key),
            key_prefix=raw_key[:8],
            label=label,
            is_admin=True,
        )
        session.add(key)
        await session.commit()
    return raw_key


def create_admin_key():
    """CLI entry point: create an admin API key."""
    import argparse

    parser = argparse.ArgumentParser(description="Create an admin API key")
    parser.add_argument("--label", required=True, help="Label for the key (e.g. your name)")
    args = parser.parse_args(sys.argv[1:])

    raw_key = asyncio.run(_create_admin_key_impl(args.label))
    print(f"\nAdmin API key created for '{args.label}':")
    print(f"\n  {raw_key}\n")
    print("Save this key — it cannot be retrieved again.")


if __name__ == "__main__":
    cli()
