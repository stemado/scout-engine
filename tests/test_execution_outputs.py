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


async def test_outputs_prefers_finalized_over_crdownload_partial(tmp_path, monkeypatch):
    """A newer .crdownload partial must not win over a finalized file.

    Headless in-browser blob downloads never rename off .crdownload, so
    file_path must point at the finalized export; the partial still appears
    in download_files (full inventory preserved).
    """
    monkeypatch.setattr(settings, "download_dir", str(tmp_path))
    execution = await _seed_completed_execution()
    exec_dir = tmp_path / str(execution.id)
    exec_dir.mkdir()
    finalized = exec_dir / "export.csv"
    finalized.write_text("a,b\n1,2\n")
    partial = exec_dir / "data.crdownload"
    partial.write_text("a,b\n3,4\n")
    os.utime(finalized, (1_700_000_000, 1_700_000_000))  # force finalized OLDER than partial

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/executions/{execution.id}")

    body = resp.json()
    assert body["outputs"]["file_path"] == str(finalized)
    assert sorted(body["outputs"]["download_files"]) == ["data.crdownload", "export.csv"]


async def test_outputs_falls_back_to_partial_when_only_crdownload(tmp_path, monkeypatch):
    """When only partials exist, fall back to the newest partial (no hard skip)."""
    monkeypatch.setattr(settings, "download_dir", str(tmp_path))
    execution = await _seed_completed_execution()
    exec_dir = tmp_path / str(execution.id)
    exec_dir.mkdir()
    partial = exec_dir / "partial.crdownload"
    partial.write_text("a,b\n3,4\n")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/executions/{execution.id}")

    body = resp.json()
    assert body["outputs"]["file_path"] == str(partial)
    assert body["outputs"]["download_files"] == ["partial.crdownload"]


async def test_outputs_file_path_is_absolute_with_relative_download_dir(tmp_path, monkeypatch):
    """file_path must be absolute even when download_dir is configured relative.

    The engine's default download_dir is relative (``./downloads``); a
    different-CWD consumer (the .NET client) can't resolve a relative path.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "download_dir", "./dl_rel")
    execution = await _seed_completed_execution()
    exec_dir = tmp_path / "dl_rel" / str(execution.id)
    exec_dir.mkdir(parents=True)
    downloaded = exec_dir / "export.csv"
    downloaded.write_text("a,b\n1,2\n")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/executions/{execution.id}")

    file_path = resp.json()["outputs"]["file_path"]
    assert os.path.isabs(file_path)
    assert os.path.isfile(file_path)
