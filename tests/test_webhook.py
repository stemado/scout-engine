"""Tests for the webhook callback sender service."""

import httpx
import pytest
from pytest_httpx import HTTPXMock

from app.services.webhook import send_webhook_callback


COMMON_KWARGS = {
    "execution_id": "exec-456",
    "status": "completed",
    "steps_passed": 5,
    "steps_failed": 0,
    "error_message": None,
    "started_at": "2026-03-13T10:00:00Z",
    "finished_at": "2026-03-13T10:05:00Z",
    "duration_ms": 300000,
}


@pytest.mark.asyncio
async def test_sends_post_to_callback_url(httpx_mock: HTTPXMock):
    """Sends HTTP POST with execution results to the callback URL."""
    httpx_mock.add_response(status_code=200)

    await send_webhook_callback(
        callback_url="http://sentinel:8100/api/callbacks/test-123",
        **COMMON_KWARGS,
    )

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    req = requests[0]
    assert req.method == "POST"
    assert str(req.url) == "http://sentinel:8100/api/callbacks/test-123"

    import json
    payload = json.loads(req.content)
    assert payload["executionId"] == "exec-456"
    assert payload["type"] == "completion"
    assert payload["status"] == "completed"
    assert payload["stepsPassed"] == 5
    assert payload["stepsFailed"] == 0
    assert payload["errorMessage"] is None
    assert payload["durationMs"] == 300000


@pytest.mark.asyncio
async def test_failed_webhook_does_not_raise(httpx_mock: HTTPXMock):
    """Webhook failure is logged but does not raise or affect execution status."""
    httpx_mock.add_response(status_code=500)

    # Should not raise
    await send_webhook_callback(
        callback_url="http://sentinel:8100/api/callbacks/test-123",
        execution_id="exec-456",
        status="failed",
        steps_passed=3,
        steps_failed=1,
        error_message="Element not found",
        started_at="2026-03-13T10:00:00Z",
        finished_at="2026-03-13T10:03:00Z",
        duration_ms=180000,
    )


@pytest.mark.asyncio
async def test_connection_error_does_not_raise(httpx_mock: HTTPXMock):
    """Connection errors (service unreachable) are caught and logged."""
    httpx_mock.add_exception(httpx.ConnectError("Connection refused"))

    await send_webhook_callback(
        callback_url="http://unreachable:9999/callback",
        **COMMON_KWARGS,
    )


@pytest.mark.asyncio
async def test_empty_callback_url_is_noop(httpx_mock: HTTPXMock):
    """If callback_url is None or empty, no HTTP call is made."""
    await send_webhook_callback(callback_url=None, **COMMON_KWARGS)
    await send_webhook_callback(callback_url="", **COMMON_KWARGS)

    assert len(httpx_mock.get_requests()) == 0
