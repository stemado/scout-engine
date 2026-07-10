"""GET /api/executions/{id} must expose downloads as outputs.file_path.

The synced suite has no ``db_session`` fixture — the autouse ``test_db``
fixture in conftest patches ``app.database.async_session`` to the in-memory
SQLite factory, so we seed ``Execution`` rows directly through it.
"""
import os

from httpx import ASGITransport, AsyncClient

import app.database as app_database
from app.config import settings
from app.main import app
from app.models import Execution, WorkflowRecord


async def _seed_completed_execution(outputs: dict | None = None) -> Execution:
    """Insert a completed workflow + execution into the test DB and return it."""
    async with app_database.async_session() as session:
        record = WorkflowRecord(
            name="outputs-test",
            workflow_json={"name": "outputs-test", "version": "1.0", "steps": []},
        )
        session.add(record)
        await session.flush()
        execution = Execution(
            workflow_id=record.id,
            status="completed",
            total_steps=1,
            passed_steps=1,
            failed_steps=0,
            outputs=outputs,
        )
        session.add(execution)
        await session.commit()
        await session.refresh(execution)
        return execution


async def test_outputs_null_when_no_downloads(tmp_path, monkeypatch):
    """No download dir and no step outputs -> response outputs is null."""
    monkeypatch.setattr(settings, "download_dir", str(tmp_path))
    execution = await _seed_completed_execution()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/executions/{execution.id}")
    assert resp.status_code == 200
    assert resp.json()["outputs"] is None


async def test_outputs_file_path_is_newest_download(tmp_path, monkeypatch):
    """outputs.file_path is the most-recently-modified download; download_files lists all."""
    monkeypatch.setattr(settings, "download_dir", str(tmp_path))
    execution = await _seed_completed_execution()
    exec_dir = tmp_path / str(execution.id)
    exec_dir.mkdir()
    older = exec_dir / "older.csv"
    older.write_text("a,b\n1,2\n")
    newest = exec_dir / "export.csv"
    newest.write_text("a,b\n3,4\n")
    os.utime(older, (1_700_000_000, 1_700_000_000))  # force older mtime

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/executions/{execution.id}")

    body = resp.json()
    assert body["outputs"]["file_path"] == str(newest)
    assert sorted(body["outputs"]["download_files"]) == ["export.csv", "older.csv"]


async def test_outputs_merge_preserves_step_outputs_and_adds_downloads(tmp_path, monkeypatch):
    """DB step outputs and derived download info coexist; downloads win their keys."""
    monkeypatch.setattr(settings, "download_dir", str(tmp_path))
    execution = await _seed_completed_execution(outputs={"scraped_value": "abc"})
    exec_dir = tmp_path / str(execution.id)
    exec_dir.mkdir()
    downloaded = exec_dir / "export.csv"
    downloaded.write_text("a,b\n3,4\n")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/executions/{execution.id}")

    body = resp.json()
    assert body["outputs"]["scraped_value"] == "abc"
    assert body["outputs"]["file_path"] == str(downloaded)
    assert body["outputs"]["download_files"] == ["export.csv"]
