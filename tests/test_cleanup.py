"""Tests for the artifact cleanup service."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base, Execution, WorkflowRecord
from app.services.cleanup import cleanup_old_artifacts


@pytest.fixture
async def cleanup_db():
    """Standalone async SQLite engine for cleanup tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", isolation_level="AUTOCOMMIT")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _insert_execution(factory, created_at, exec_id=None):
    """Helper to insert a workflow + execution with a specific created_at."""
    exec_id = exec_id or uuid.uuid4()
    wf_id = uuid.uuid4()
    async with factory() as db:
        wf = WorkflowRecord(
            id=wf_id,
            name="test",
            workflow_json={"name": "test", "version": "1.0", "steps": []},
        )
        db.add(wf)
        ex = Execution(id=exec_id, workflow_id=wf_id, status="completed", created_at=created_at)
        db.add(ex)
        await db.commit()
    return str(exec_id)


@pytest.mark.asyncio
async def test_cleanup_removes_old_dirs(cleanup_db, tmp_path):
    """Old execution artifact directories are removed."""
    old_id = await _insert_execution(
        cleanup_db,
        created_at=datetime.now(timezone.utc) - timedelta(days=60),
    )

    # Create artifact dirs for the old execution
    ss_dir = tmp_path / "screenshots" / old_id
    ss_dir.mkdir(parents=True)
    (ss_dir / "001.png").write_bytes(b"old-screenshot")
    dl_dir = tmp_path / "downloads" / old_id
    dl_dir.mkdir(parents=True)
    (dl_dir / "report.csv").write_bytes(b"old-download")

    removed = await cleanup_old_artifacts(
        session_factory=cleanup_db,
        retention_days=30,
        screenshot_dir=str(tmp_path / "screenshots"),
        download_dir=str(tmp_path / "downloads"),
    )

    assert removed == 2  # one screenshot dir + one download dir
    assert not ss_dir.exists()
    assert not dl_dir.exists()


@pytest.mark.asyncio
async def test_cleanup_preserves_recent_dirs(cleanup_db, tmp_path):
    """Recent execution artifact directories are preserved."""
    recent_id = await _insert_execution(
        cleanup_db,
        created_at=datetime.now(timezone.utc) - timedelta(days=5),
    )

    ss_dir = tmp_path / "screenshots" / recent_id
    ss_dir.mkdir(parents=True)
    (ss_dir / "001.png").write_bytes(b"recent-screenshot")

    removed = await cleanup_old_artifacts(
        session_factory=cleanup_db,
        retention_days=30,
        screenshot_dir=str(tmp_path / "screenshots"),
        download_dir=str(tmp_path / "downloads"),
    )

    assert removed == 0
    assert ss_dir.exists()


@pytest.mark.asyncio
async def test_cleanup_mixed_old_and_recent(cleanup_db, tmp_path):
    """Only old dirs are removed when both old and recent exist."""
    old_id = await _insert_execution(
        cleanup_db,
        created_at=datetime.now(timezone.utc) - timedelta(days=60),
    )
    recent_id = await _insert_execution(
        cleanup_db,
        created_at=datetime.now(timezone.utc) - timedelta(days=5),
    )

    for exec_id in (old_id, recent_id):
        d = tmp_path / "screenshots" / exec_id
        d.mkdir(parents=True)
        (d / "001.png").write_bytes(b"data")

    removed = await cleanup_old_artifacts(
        session_factory=cleanup_db,
        retention_days=30,
        screenshot_dir=str(tmp_path / "screenshots"),
        download_dir=str(tmp_path / "downloads"),
    )

    assert removed == 1
    assert not (tmp_path / "screenshots" / old_id).exists()
    assert (tmp_path / "screenshots" / recent_id).exists()


@pytest.mark.asyncio
async def test_cleanup_no_dirs_on_disk(cleanup_db, tmp_path):
    """Cleanup handles missing directories gracefully (returns 0)."""
    await _insert_execution(
        cleanup_db,
        created_at=datetime.now(timezone.utc) - timedelta(days=60),
    )

    removed = await cleanup_old_artifacts(
        session_factory=cleanup_db,
        retention_days=30,
        screenshot_dir=str(tmp_path / "screenshots"),
        download_dir=str(tmp_path / "downloads"),
    )

    assert removed == 0
