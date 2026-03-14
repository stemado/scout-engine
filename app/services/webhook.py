"""Webhook callback sender — fire-and-forget POST to callback URLs."""

import logging

import httpx

logger = logging.getLogger(__name__)


async def send_webhook_callback(
    callback_url: str | None,
    execution_id: str,
    status: str,
    steps_passed: int,
    steps_failed: int,
    error_message: str | None,
    started_at: str,
    finished_at: str,
    duration_ms: int,
) -> None:
    """Send execution result to callback URL. Fire-and-forget with logging.

    Callback payload contract:
    {
        "executionId": "uuid",
        "type": "completion",
        "status": "completed" | "failed" | "cancelled",
        "stepsPassed": 5,
        "stepsFailed": 0,
        "errorMessage": null,
        "startedAt": "2026-03-13T10:00:00Z",
        "finishedAt": "2026-03-13T10:05:00Z",
        "durationMs": 300000
    }
    """
    if not callback_url:
        return

    payload = {
        "executionId": execution_id,
        "type": "completion",
        "status": status,
        "stepsPassed": steps_passed,
        "stepsFailed": steps_failed,
        "errorMessage": error_message,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "durationMs": duration_ms,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(callback_url, json=payload)
            if response.status_code >= 400:
                logger.warning(
                    "Webhook callback returned %d for execution %s: %s",
                    response.status_code,
                    execution_id,
                    response.text[:200],
                )
            else:
                logger.info(
                    "Webhook callback sent for execution %s -> %s",
                    execution_id,
                    callback_url,
                )
    except httpx.HTTPError as e:
        logger.warning(
            "Webhook callback failed for execution %s to %s: %s",
            execution_id,
            callback_url,
            str(e),
        )
