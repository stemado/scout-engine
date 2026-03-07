"""Browser session registry -- exposes CDP connection info for in-flight executions.

Follows the same module-level registry pattern as cancellation.py.
An entry exists ONLY while a browser is alive and accepting CDP connections.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class StepProgress:
    """Live progress of the currently executing step."""

    step_order: int
    step_name: str
    action: str
    started_at: float


@dataclass
class PausedStepInfo:
    """Context about the step where execution paused."""

    step_order: int
    step_name: str
    action: str
    reason: str | None = None
    error: str | None = None


@dataclass
class BrowserSessionInfo:
    """CDP connection info for an in-flight browser session."""

    execution_id: str
    cdp_host: str
    cdp_port: int
    cdp_websocket_url: str
    targets_url: str
    devtools_frontend_url: str
    current_step: StepProgress | None = None
    registered_at: float = 0.0
    state: str = "running"
    paused_at_step: PausedStepInfo | None = None


_registry: dict[str, BrowserSessionInfo] = {}
_lock = threading.Lock()


def register(execution_id: str, info: BrowserSessionInfo) -> None:
    """Register a browser session. Called from executor thread after driver creation."""
    info.registered_at = time.monotonic()
    with _lock:
        _registry[execution_id] = info


def get_session(execution_id: str) -> BrowserSessionInfo | None:
    """Look up browser session info. Returns None if no browser is active."""
    with _lock:
        return _registry.get(execution_id)


def update_step(execution_id: str, step: StepProgress | None) -> None:
    """Update the current step progress for an execution."""
    with _lock:
        info = _registry.get(execution_id)
        if info:
            info.current_step = step


def set_paused(execution_id: str, state: str, paused_step: PausedStepInfo) -> None:
    """Mark a session as paused with context about the paused step."""
    with _lock:
        info = _registry.get(execution_id)
        if info:
            info.state = state
            info.paused_at_step = paused_step
            info.current_step = None


def set_running(execution_id: str) -> None:
    """Mark a session as running again after a pause."""
    with _lock:
        info = _registry.get(execution_id)
        if info:
            info.state = "running"
            info.paused_at_step = None


def unregister(execution_id: str) -> None:
    """Remove the session info when the browser is about to close."""
    with _lock:
        _registry.pop(execution_id, None)
