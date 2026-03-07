"""Async SQLAlchemy engine and session factory."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import settings

engine = create_async_engine(settings.database_url, echo=settings.debug)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    """Dependency that yields an async database session."""
    async with async_session() as session:
        yield session


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Dependency that returns the session factory for background tasks.

    Background tasks (like workflow execution) cannot use get_db because they
    outlive the HTTP request.  They need the raw factory to create their own
    sessions.  Exposing this as a dependency lets tests override it alongside
    get_db so background tasks use the test database.
    """
    return async_session
