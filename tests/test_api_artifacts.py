"""Tests for the artifact API endpoints."""

import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_list_artifacts_with_screenshots(client, tmp_path, monkeypatch):
    """GET /api/executions/{id}/artifacts returns screenshot files."""
    exec_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    ss_dir = tmp_path / "screenshots" / exec_id
    ss_dir.mkdir(parents=True)
    (ss_dir / "001_navigate.png").write_bytes(b"fake-png-data")
    (ss_dir / "002_click.png").write_bytes(b"fake-png-data-2")

    monkeypatch.setattr("app.api.artifacts.settings.screenshot_dir", str(tmp_path / "screenshots"))
    monkeypatch.setattr("app.api.artifacts.settings.download_dir", str(tmp_path / "downloads"))

    resp = await client.get(f"/api/executions/{exec_id}/artifacts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["execution_id"] == exec_id
    assert len(data["artifacts"]) == 2
    assert data["artifacts"][0]["filename"] == "001_navigate.png"
    assert data["artifacts"][0]["type"] == "screenshot"
    assert data["artifacts"][0]["size_bytes"] == len(b"fake-png-data")


@pytest.mark.asyncio
async def test_list_artifacts_empty(client, tmp_path, monkeypatch):
    """GET /api/executions/{id}/artifacts returns empty list for unknown ID."""
    monkeypatch.setattr("app.api.artifacts.settings.screenshot_dir", str(tmp_path / "screenshots"))
    monkeypatch.setattr("app.api.artifacts.settings.download_dir", str(tmp_path / "downloads"))

    exec_id = "aaaaaaaa-bbbb-cccc-dddd-ffffffffffff"
    resp = await client.get(f"/api/executions/{exec_id}/artifacts")
    assert resp.status_code == 200
    assert resp.json()["artifacts"] == []


@pytest.mark.asyncio
async def test_list_artifacts_includes_downloads(client, tmp_path, monkeypatch):
    """Downloads should appear alongside screenshots in artifact list."""
    exec_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    dl_dir = tmp_path / "downloads" / exec_id
    dl_dir.mkdir(parents=True)
    (dl_dir / "report.csv").write_bytes(b"col1,col2")

    monkeypatch.setattr("app.api.artifacts.settings.screenshot_dir", str(tmp_path / "screenshots"))
    monkeypatch.setattr("app.api.artifacts.settings.download_dir", str(tmp_path / "downloads"))

    resp = await client.get(f"/api/executions/{exec_id}/artifacts")
    assert resp.status_code == 200
    artifacts = resp.json()["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["type"] == "download"
    assert artifacts[0]["filename"] == "report.csv"
