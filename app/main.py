"""scout-engine -- workflow execution service for Scout browser automation."""

import logging
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

    # Load existing enabled schedules from database so cron jobs survive restarts.
    # Wrapped in try/except so the app can still start on a fresh install where
    # the database has not been migrated yet.
    try:
        from app.database import async_session
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


if __name__ == "__main__":
    cli()
