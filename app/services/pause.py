"""Pause/resume registry for in-flight workflow executions.

Follows the same module-level registry pattern as cancellation.py.
Uses queue.Queue to combine the wake signal and instruction payload
into a single atomic operation — no race between event and shared variable.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass


@dataclass
class ResumeInstruction:
    """Instruction sent to a paused executor thread."""

    action: str  # "retry" | "continue" | "abort" | "jump"
    step_index: int | None = None  # required only for "jump"


_registry: dict[str, queue.Queue[ResumeInstruction]] = {}
_pause_requested: dict[str, threading.Event] = {}
_lock = threading.Lock()


def register(execution_id: str) -> tuple[queue.Queue[ResumeInstruction], threading.Event]:
    """Register a pause queue and pause-requested event for an execution.

    Returns (pause_queue, pause_requested_event).
    The executor blocks on pause_queue.get() when paused.
    The API sets pause_requested_event to signal an external pause request.
    """
    q: queue.Queue[ResumeInstruction] = queue.Queue(maxsize=1)
    event = threading.Event()
    with _lock:
        _registry[execution_id] = q
        _pause_requested[execution_id] = event
    return q, event


def resume(execution_id: str, instruction: ResumeInstruction) -> bool:
    """Send a resume instruction to a paused execution. Returns True if found."""
    with _lock:
        q = _registry.get(execution_id)
    if q:
        q.put(instruction)
        return True
    return False


def request_pause(execution_id: str) -> bool:
    """Signal an external pause request. Returns True if found."""
    with _lock:
        event = _pause_requested.get(execution_id)
    if event:
        event.set()
        return True
    return False


def unregister(execution_id: str) -> None:
    """Remove pause queue and event when execution finishes."""
    with _lock:
        _registry.pop(execution_id, None)
        _pause_requested.pop(execution_id, None)
