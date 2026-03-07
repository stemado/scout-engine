"""Tests for the workflow CRUD API."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


SAMPLE_WORKFLOW = {
    "schema_version": "1.0",
    "name": "test-workflow",
    "description": "A test workflow",
    "variables": {
        "USERNAME": {"type": "credential", "default": "admin"},
    },
    "settings": {"headless": True, "on_error": "stop"},
    "steps": [
        {"order": 1, "name": "Navigate", "action": "navigate", "value": "https://example.com"},
        {"order": 2, "name": "Type", "action": "type", "selector": "#user", "value": "${USERNAME}"},
    ],
}


@pytest.fixture
async def client():
    """Async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_upload_workflow(client):
    resp = await client.post("/api/workflows", json=SAMPLE_WORKFLOW)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "test-workflow"
    assert "id" in data


@pytest.mark.asyncio
async def test_list_workflows(client):
    # Upload one first
    await client.post("/api/workflows", json=SAMPLE_WORKFLOW)
    resp = await client.get("/api/workflows")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_get_workflow(client):
    create_resp = await client.post("/api/workflows", json=SAMPLE_WORKFLOW)
    workflow_id = create_resp.json()["id"]

    resp = await client.get(f"/api/workflows/{workflow_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "test-workflow"


@pytest.mark.asyncio
async def test_get_workflow_not_found(client):
    resp = await client.get("/api/workflows/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_workflow(client):
    create_resp = await client.post("/api/workflows", json=SAMPLE_WORKFLOW)
    workflow_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/workflows/{workflow_id}")
    assert resp.status_code == 204

    # Verify it's gone
    resp = await client.get(f"/api/workflows/{workflow_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_invalid_workflow(client):
    resp = await client.post("/api/workflows", json={"not": "a workflow"})
    assert resp.status_code == 422
