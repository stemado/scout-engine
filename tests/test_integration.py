"""Integration test -- full API flow with a real workflow JSON file.

Does NOT launch a real browser. Tests the complete API lifecycle:
upload -> schedule -> run -> check status -> list resources -> cleanup.
"""

import asyncio
import json
from pathlib import Path

import pytest
from unittest.mock import patch, AsyncMock
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.executor import ExecutionResult, StepResult


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_full_workflow_lifecycle(client):
    """Upload a workflow, schedule it, run it, check results."""
    # 1. Upload workflow from fixture file
    workflow_json = json.loads((FIXTURES / "sample-workflow.json").read_text())
    resp = await client.post("/api/workflows", json={"workflow": workflow_json})
    assert resp.status_code == 201
    workflow_id = resp.json()["id"]

    # 2. Create a schedule
    resp = await client.post("/api/schedules", json={
        "workflow_id": workflow_id,
        "name": "Every minute (test)",
        "cron_expression": "* * * * *",
    })
    assert resp.status_code == 201
    schedule_id = resp.json()["id"]

    # 3. Run immediately (mocked executor)
    mock_result = ExecutionResult(
        status="completed", passed=2, failed=0, total_ms=1500,
        steps=[
            StepResult(step_order=1, step_name="Navigate", action="navigate", status="passed", elapsed_ms=1000),
            StepResult(step_order=2, step_name="Wait", action="wait", status="passed", elapsed_ms=500),
        ],
    )
    with patch("app.api.executions.execute_workflow", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.post(f"/api/workflows/{workflow_id}/run")
    assert resp.status_code == 202
    execution_id = resp.json()["execution_id"]

    # 4. Wait for background task to complete, then check execution
    await asyncio.sleep(0.2)

    resp = await client.get(f"/api/executions/{execution_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["passed_steps"] == 2
    assert data["failed_steps"] == 0
    assert len(data["steps"]) == 2

    # 5. List workflows, schedules, executions
    resp = await client.get("/api/workflows")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1

    resp = await client.get("/api/schedules")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1

    resp = await client.get("/api/executions")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1

    # 6. Cleanup -- delete schedule first, then workflow (cascades executions)
    resp = await client.delete(f"/api/schedules/{schedule_id}")
    assert resp.status_code == 204

    resp = await client.delete(f"/api/workflows/{workflow_id}")
    assert resp.status_code == 204

    # 7. Health check
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
