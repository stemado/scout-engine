"""Tests for the execution API."""

import asyncio

import pytest
from unittest.mock import patch, AsyncMock
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.executor import ExecutionResult, StepResult


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
    resp = await client.post("/api/workflows", json={"workflow": SAMPLE_WORKFLOW})
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_run_workflow(client, workflow_id):
    """POST /api/workflows/{id}/run should return 202 with execution ID."""
    mock_result = ExecutionResult(
        status="completed", passed=1, failed=0, total_ms=500,
        steps=[StepResult(step_order=1, step_name="Navigate", action="navigate", status="passed", elapsed_ms=500)],
    )
    with patch("app.api.executions.execute_workflow", new_callable=AsyncMock, return_value=mock_result):
        resp = await client.post(f"/api/workflows/{workflow_id}/run")
    assert resp.status_code == 202
    data = resp.json()
    assert "execution_id" in data
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_run_workflow_not_found(client):
    resp = await client.post("/api/workflows/00000000-0000-0000-0000-000000000000/run")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_executions(client, workflow_id):
    """GET /api/executions should return execution list."""
    mock_result = ExecutionResult(status="completed", passed=1, failed=0, total_ms=500, steps=[])
    with patch("app.api.executions.execute_workflow", new_callable=AsyncMock, return_value=mock_result):
        await client.post(f"/api/workflows/{workflow_id}/run")

    # Wait for the background task to complete
    await asyncio.sleep(0.2)

    resp = await client.get("/api/executions")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_execution(client, workflow_id):
    """GET /api/executions/{id} should return execution detail."""
    mock_result = ExecutionResult(
        status="completed", passed=1, failed=0, total_ms=500,
        steps=[StepResult(step_order=1, step_name="Navigate", action="navigate", status="passed", elapsed_ms=500)],
    )
    with patch("app.api.executions.execute_workflow", new_callable=AsyncMock, return_value=mock_result):
        run_resp = await client.post(f"/api/workflows/{workflow_id}/run")

    execution_id = run_resp.json()["execution_id"]
    await asyncio.sleep(0.2)

    resp = await client.get(f"/api/executions/{execution_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == execution_id
    assert data["workflow_id"] == workflow_id


@pytest.mark.asyncio
async def test_get_execution_not_found(client):
    """GET /api/executions/{id} with invalid ID should return 404."""
    resp = await client.get("/api/executions/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_stop_pending_execution(client, workflow_id):
    """POST /api/executions/{id}/stop should cancel a pending execution.

    We create a "pending" execution record directly via the run endpoint
    but mock _run_execution itself (the background task) to be a no-op,
    leaving the execution in "pending" state for the stop endpoint to cancel.
    """
    with patch("app.api.executions._run_execution", new_callable=AsyncMock):
        run_resp = await client.post(f"/api/workflows/{workflow_id}/run")
    execution_id = run_resp.json()["execution_id"]

    resp = await client.post(f"/api/executions/{execution_id}/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_stop_completed_execution_fails(client, workflow_id):
    """POST /api/executions/{id}/stop should return 409 for a completed execution."""
    mock_result = ExecutionResult(status="completed", passed=1, failed=0, total_ms=500, steps=[])
    with patch("app.api.executions.execute_workflow", new_callable=AsyncMock, return_value=mock_result):
        run_resp = await client.post(f"/api/workflows/{workflow_id}/run")
    execution_id = run_resp.json()["execution_id"]

    # Wait for background task to finish and update status to "completed"
    await asyncio.sleep(0.2)

    resp = await client.post(f"/api/executions/{execution_id}/stop")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_get_browser_session_active(client, workflow_id):
    """GET /browser returns CDP info when a browser session is active."""
    from app.services.browser_session import (
        BrowserSessionInfo, StepProgress, register as reg, unregister as unreg,
    )
    with patch("app.api.executions._run_execution", new_callable=AsyncMock):
        run_resp = await client.post(f"/api/workflows/{workflow_id}/run")
    eid = run_resp.json()["execution_id"]

    reg(eid, BrowserSessionInfo(
        execution_id=eid, cdp_host="127.0.0.1", cdp_port=51234,
        cdp_websocket_url="ws://127.0.0.1:51234/devtools/browser/fake",
        targets_url="http://127.0.0.1:51234/json/list",
        devtools_frontend_url="http://127.0.0.1:51234",
        current_step=StepProgress(step_order=1, step_name="Nav", action="navigate", started_at=0.0),
    ))
    try:
        resp = await client.get(f"/api/executions/{eid}/browser")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cdp_port"] == 51234
        assert data["current_step"]["step_order"] == 1
    finally:
        unreg(eid)


@pytest.mark.asyncio
async def test_get_browser_session_not_active(client):
    """GET /browser returns 404 when no browser is running."""
    resp = await client.get("/api/executions/00000000-0000-0000-0000-000000000000/browser")
    assert resp.status_code == 404
    assert "No active browser session" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_pause_running_execution(client, workflow_id):
    """POST /pause should return pause_requested for a running execution."""
    from app.services.pause import register as pause_reg, unregister as pause_unreg
    with patch("app.api.executions._run_execution", new_callable=AsyncMock):
        run_resp = await client.post(f"/api/workflows/{workflow_id}/run")
    eid = run_resp.json()["execution_id"]
    # Manually register pause handle (normally done by _run_execution)
    pause_reg(eid)
    try:
        resp = await client.post(f"/api/executions/{eid}/pause")
        assert resp.status_code == 200
        assert resp.json()["status"] == "pause_requested"
    finally:
        pause_unreg(eid)


@pytest.mark.asyncio
async def test_pause_unknown_execution(client):
    """POST /pause for unknown execution returns 404."""
    resp = await client.post("/api/executions/00000000-0000-0000-0000-000000000000/pause")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resume_paused_execution(client, workflow_id):
    """POST /resume should deliver instruction to a paused execution."""
    from app.services.pause import register as pause_reg, unregister as pause_unreg
    with patch("app.api.executions._run_execution", new_callable=AsyncMock):
        run_resp = await client.post(f"/api/workflows/{workflow_id}/run")
    eid = run_resp.json()["execution_id"]
    pause_q, _ = pause_reg(eid)
    try:
        resp = await client.post(
            f"/api/executions/{eid}/resume",
            json={"action": "retry"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resumed"
        # Verify instruction was placed on queue
        instruction = pause_q.get_nowait()
        assert instruction.action == "retry"
    finally:
        pause_unreg(eid)


@pytest.mark.asyncio
async def test_resume_unknown_execution(client):
    """POST /resume for unknown execution returns 404."""
    resp = await client.post(
        "/api/executions/00000000-0000-0000-0000-000000000000/resume",
        json={"action": "continue"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_run_workflow_accepts_callback_url(client, workflow_id):
    """POST /api/workflows/{id}/run with callback_url stores it on execution."""
    mock_result = ExecutionResult(status="completed", passed=1, failed=0, total_ms=500, steps=[])
    with patch("app.api.executions._run_execution", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/workflows/{workflow_id}/run",
            json={"callback_url": "http://sentinel:8100/api/callbacks/test-123"},
        )
    assert resp.status_code == 202
    execution_id = resp.json()["execution_id"]

    # Verify callback_url stored on execution
    exec_resp = await client.get(f"/api/executions/{execution_id}")
    assert exec_resp.json()["callback_url"] == "http://sentinel:8100/api/callbacks/test-123"


@pytest.mark.asyncio
async def test_run_workflow_without_callback_url_backward_compatible(client, workflow_id):
    """Omitting callback_url is valid — no body or empty body both work."""
    mock_result = ExecutionResult(status="completed", passed=1, failed=0, total_ms=500, steps=[])
    with patch("app.api.executions._run_execution", new_callable=AsyncMock):
        # No body at all (backward compatible)
        resp = await client.post(f"/api/workflows/{workflow_id}/run")
    assert resp.status_code == 202

    execution_id = resp.json()["execution_id"]
    exec_resp = await client.get(f"/api/executions/{execution_id}")
    assert exec_resp.json()["callback_url"] is None


@pytest.mark.asyncio
async def test_browser_session_includes_paused_state(client, workflow_id):
    """GET /browser should include state and paused_at_step when paused."""
    from app.services.browser_session import (
        BrowserSessionInfo, PausedStepInfo,
        register as bs_reg, set_paused, unregister as bs_unreg,
    )
    with patch("app.api.executions._run_execution", new_callable=AsyncMock):
        run_resp = await client.post(f"/api/workflows/{workflow_id}/run")
    eid = run_resp.json()["execution_id"]

    bs_reg(eid, BrowserSessionInfo(
        execution_id=eid, cdp_host="127.0.0.1", cdp_port=51234,
        cdp_websocket_url="ws://127.0.0.1:51234/devtools/browser/fake",
        targets_url="http://127.0.0.1:51234/json/list",
        devtools_frontend_url="http://127.0.0.1:51234",
    ))
    set_paused(eid, "paused_error", PausedStepInfo(
        step_order=3, step_name="Click submit", action="click",
        reason="Step failed", error="Element not found: #submit-btn",
    ))
    try:
        resp = await client.get(f"/api/executions/{eid}/browser")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "paused_error"
        assert data["paused_at_step"]["step_order"] == 3
        assert data["paused_at_step"]["reason"] == "Step failed"
        assert data["paused_at_step"]["error"] == "Element not found: #submit-btn"
    finally:
        bs_unreg(eid)
