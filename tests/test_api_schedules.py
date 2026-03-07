"""Tests for the schedule API."""

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from app.main import app


SAMPLE_WORKFLOW = {
    "schema_version": "1.0",
    "name": "test-workflow",
    "steps": [
        {"order": 1, "name": "Navigate", "action": "navigate", "value": "https://example.com"},
    ],
}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def workflow_id(client):
    resp = await client.post("/api/workflows", json=SAMPLE_WORKFLOW)
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_create_schedule(client, workflow_id):
    resp = await client.post("/api/schedules", json={
        "workflow_id": workflow_id,
        "name": "Daily 6am",
        "cron_expression": "0 6 * * *",
        "timezone": "US/Eastern",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Daily 6am"
    assert data["cron_expression"] == "0 6 * * *"
    assert data["enabled"] is True
    assert "next_run_at" in data


@pytest.mark.asyncio
async def test_list_schedules(client, workflow_id):
    await client.post("/api/schedules", json={
        "workflow_id": workflow_id,
        "name": "Daily 6am",
        "cron_expression": "0 6 * * *",
    })
    resp = await client.get("/api/schedules")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.asyncio
async def test_update_schedule(client, workflow_id):
    create_resp = await client.post("/api/schedules", json={
        "workflow_id": workflow_id,
        "name": "Daily 6am",
        "cron_expression": "0 6 * * *",
    })
    schedule_id = create_resp.json()["id"]

    resp = await client.put(f"/api/schedules/{schedule_id}", json={
        "cron_expression": "0 8 * * 1-5",
        "name": "Weekdays 8am",
    })
    assert resp.status_code == 200
    assert resp.json()["cron_expression"] == "0 8 * * 1-5"
    assert resp.json()["name"] == "Weekdays 8am"


@pytest.mark.asyncio
async def test_delete_schedule(client, workflow_id):
    create_resp = await client.post("/api/schedules", json={
        "workflow_id": workflow_id,
        "name": "Daily 6am",
        "cron_expression": "0 6 * * *",
    })
    schedule_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/schedules/{schedule_id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_create_schedule_invalid_cron(client, workflow_id):
    resp = await client.post("/api/schedules", json={
        "workflow_id": workflow_id,
        "name": "Bad schedule",
        "cron_expression": "not valid",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_schedule_workflow_not_found(client):
    resp = await client.post("/api/schedules", json={
        "workflow_id": "00000000-0000-0000-0000-000000000000",
        "name": "Orphan schedule",
        "cron_expression": "0 6 * * *",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_schedule_not_found(client):
    resp = await client.put("/api/schedules/00000000-0000-0000-0000-000000000000", json={
        "name": "Ghost",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_schedule_not_found(client):
    resp = await client.delete("/api/schedules/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


# ── New tests: APScheduler runtime sync ──────────────────────────────────────


@pytest.mark.asyncio
async def test_create_enabled_schedule_registers_job(client, workflow_id):
    """Creating an enabled schedule must register a job with APScheduler."""
    with patch("app.api.schedules.add_or_replace_schedule") as mock_add:
        resp = await client.post("/api/schedules", json={
            "workflow_id": workflow_id,
            "name": "Daily",
            "cron_expression": "0 6 * * *",
            "timezone": "UTC",
        })
    assert resp.status_code == 201
    mock_add.assert_called_once()
    call_args = mock_add.call_args[0]
    assert call_args[1] == "0 6 * * *"
    assert call_args[2] == "UTC"


@pytest.mark.asyncio
async def test_create_disabled_schedule_does_not_register_job(client, workflow_id):
    """Creating a disabled schedule must NOT register a job with APScheduler."""
    with patch("app.api.schedules.add_or_replace_schedule") as mock_add:
        resp = await client.post("/api/schedules", json={
            "workflow_id": workflow_id,
            "name": "Daily",
            "cron_expression": "0 6 * * *",
            "enabled": False,
        })
    assert resp.status_code == 201
    mock_add.assert_not_called()


@pytest.mark.asyncio
async def test_update_cron_replaces_scheduler_job(client, workflow_id):
    """Updating cron on an enabled schedule must call add_or_replace_schedule."""
    create_resp = await client.post("/api/schedules", json={
        "workflow_id": workflow_id, "name": "Daily", "cron_expression": "0 6 * * *",
    })
    schedule_id = create_resp.json()["id"]

    with patch("app.api.schedules.add_or_replace_schedule") as mock_add:
        resp = await client.put(f"/api/schedules/{schedule_id}", json={
            "cron_expression": "0 8 * * 1-5",
        })
    assert resp.status_code == 200
    mock_add.assert_called_once()


@pytest.mark.asyncio
async def test_disable_schedule_removes_scheduler_job(client, workflow_id):
    """Setting enabled=False must remove the job from APScheduler."""
    create_resp = await client.post("/api/schedules", json={
        "workflow_id": workflow_id, "name": "Daily", "cron_expression": "0 6 * * *",
    })
    schedule_id = create_resp.json()["id"]

    with patch("app.api.schedules.remove_schedule_job") as mock_remove:
        resp = await client.put(f"/api/schedules/{schedule_id}", json={"enabled": False})
    assert resp.status_code == 200
    mock_remove.assert_called_once_with(schedule_id)


@pytest.mark.asyncio
async def test_delete_schedule_removes_scheduler_job(client, workflow_id):
    """Deleting a schedule must remove the job from APScheduler."""
    create_resp = await client.post("/api/schedules", json={
        "workflow_id": workflow_id, "name": "Daily", "cron_expression": "0 6 * * *",
    })
    schedule_id = create_resp.json()["id"]

    with patch("app.api.schedules.remove_schedule_job") as mock_remove:
        resp = await client.delete(f"/api/schedules/{schedule_id}")
    assert resp.status_code == 204
    mock_remove.assert_called_once_with(schedule_id)
