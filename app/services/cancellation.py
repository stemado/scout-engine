"""Cooperative cancellation registry for in-flight workflow executions.

Each execution registers a threading.Event at start. The stop API sets the
event. The executor checks it between steps and exits cleanly if set.
"""

import threading

# execution_id (str) -> threading.Event
_registry: dict[str, threading.Event] = {}
_lock = threading.Lock()


def register(execution_id: str) -> threading.Event:
    """Create and register a cancel event for an execution. Returns the event."""
    event = threading.Event()
    with _lock:
        _registry[execution_id] = event
    return event


def cancel(execution_id: str) -> bool:
    """Signal cancellation for an execution. Returns True if found, False if not."""
    with _lock:
        event = _registry.get(execution_id)
    if event:
        event.set()
        return True
    return False


def unregister(execution_id: str) -> None:
    """Remove the cancel event when an execution finishes."""
    with _lock:
        _registry.pop(execution_id, None)
