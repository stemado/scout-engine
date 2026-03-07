"""Shared test fixtures for scout-engine."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.database as app_database  # module import for patching
from app.database import get_db, get_session_factory
from app.main import app
from app.models import Base
from app.services.scheduler import scheduler as apscheduler_instance


@pytest.fixture(autouse=True)
async def test_db():
    """Override database with async SQLite in-memory for tests.

    Overrides:
    - ``get_db`` for request-scoped sessions (FastAPI dependency)
    - ``get_session_factory`` for background tasks
    - ``app.database.async_session`` for auth middleware (which can't use Depends)
    """
    # Use AUTOCOMMIT to avoid transaction interleaving between fire-and-forget
    # tasks (e.g. _touch_last_used) and endpoint sessions on the single
    # StaticPool connection that SQLite in-memory uses.
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        isolation_level="AUTOCOMMIT",
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    def override_get_session_factory():
        return session_factory

    # Patch all three access paths to the session factory
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    original_async_session = app_database.async_session
    app_database.async_session = session_factory  # middleware reads this at call time

    yield

    app.dependency_overrides.clear()
    app_database.async_session = original_async_session
    await engine.dispose()


@pytest.fixture(autouse=True)
def reset_scheduler():
    """Clear all APScheduler jobs between tests.

    The scheduler singleton persists across tests because it is module-level.
    Without this fixture, schedule API calls in one test leave orphaned jobs
    that accumulate across the session.
    """
    yield
    apscheduler_instance.remove_all_jobs()
