# Phase 1 Production Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix four critical gaps that prevent scout-engine from being production-ready: APScheduler not synced at runtime, fake execution cancellation, stubbed wait_for_download/wait_for_response actions, and missing Alembic migration.

**Architecture:** Four independent tasks, each with TDD. Tasks 1-3 touch existing modules surgically — no rewrites. Task 3 ports Scout's `NetworkMonitor` directly to avoid drift. All tests use the existing in-memory SQLite + mock-driver pattern from conftest.py.

**Tech Stack:** FastAPI, APScheduler 3.x, SQLAlchemy async, botasaurus-driver CDP, threading.Event for cooperative cancellation, pytest-asyncio (auto mode)

---

## Task 1: APScheduler Runtime Sync

Schedule mutations (create/update/delete) currently write to the DB but never notify the live APScheduler instance. Jobs only load at server startup. This task wires the two together.

**Files:**
- Modify: `app/services/scheduler.py`
- Modify: `app/api/schedules.py`
- Modify: `app/main.py`
- Modify: `tests/conftest.py` (add scheduler reset fixture)
- Modify: `tests/test_api_schedules.py` (add 5 new tests)

---

### Task 1 Step 1: Write failing tests

Add these 5 tests to the **bottom** of `tests/test_api_schedules.py`:

```python
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
```

### Task 1 Step 2: Add scheduler-reset fixture to `tests/conftest.py`

The `scheduler` singleton in `app/services/scheduler.py` is module-level — its in-memory job store persists across tests. Without a reset, every schedule API call in every test accumulates orphaned jobs in the singleton. Add this fixture so each test starts with a clean job store:

```python
from app.services.scheduler import scheduler as apscheduler_instance

@pytest.fixture(autouse=True)
def reset_scheduler():
    """Clear all APScheduler jobs between tests.

    The scheduler singleton persists across tests because it is module-level.
    Without this fixture, schedule API calls in one test leave orphaned jobs
    that accumulate across the session.
    """
    yield
    apscheduler_instance.remove_all_jobs()
```

Add this fixture to `tests/conftest.py` alongside the existing `test_db` fixture.

### Task 1 Step 3: Run to verify new schedule tests fail

```bash
cd d:/Projects/scout-engine
uv run pytest tests/test_api_schedules.py::test_create_enabled_schedule_registers_job -v
```

Expected: `FAILED` — `add_or_replace_schedule` doesn't exist yet.

### Task 1 Step 4: Refactor `app/services/scheduler.py`

Replace `add_schedule_job` with `add_or_replace_schedule`. Remove the `func` and `args` parameters — the function always schedules `execute_scheduled_workflow`. Remove the now-unused `execute_scheduled_workflow` import in callers.

In `app/services/scheduler.py`, replace lines 60-71:

```python
def add_or_replace_schedule(schedule_id: str, cron_expression: str, timezone: str) -> None:
    """Register or replace a cron job with APScheduler."""
    fields = parse_cron_expression(cron_expression)
    scheduler.add_job(
        execute_scheduled_workflow,
        trigger=CronTrigger(timezone=timezone, **fields),
        id=schedule_id,
        args=(schedule_id,),
        replace_existing=True,
    )
```

Also delete the old `add_schedule_job` function entirely.

### Task 1 Step 5: Update `app/api/schedules.py`

Change the import at the top:

```python
from app.services.scheduler import (
    add_or_replace_schedule,
    compute_next_run,
    parse_cron_expression,
    remove_schedule_job,
)
```

At the end of `create_schedule`, after `await db.refresh(schedule)`, add:

```python
    if schedule.enabled:
        add_or_replace_schedule(str(schedule.id), schedule.cron_expression, schedule.timezone)
```

At the end of `update_schedule`, after `await db.refresh(schedule)`, add:

```python
    if schedule.enabled:
        add_or_replace_schedule(str(schedule.id), schedule.cron_expression, schedule.timezone)
    else:
        remove_schedule_job(str(schedule.id))
```

At the end of `delete_schedule`, after `await db.commit()`, add:

```python
    remove_schedule_job(str(schedule_id))
```

### Task 1 Step 6: Update `app/main.py`

Change the import block at lines 13-18:

```python
from .services.scheduler import (
    add_or_replace_schedule,
    shutdown_scheduler,
    start_scheduler,
)
```

Change the lifespan body at lines 40-46:

```python
            for sched in schedules:
                add_or_replace_schedule(
                    str(sched.id),
                    sched.cron_expression,
                    sched.timezone,
                )
```

### Task 1 Step 7: Run all tests

```bash
uv run pytest tests/test_api_schedules.py tests/test_scheduler.py -v
```

Expected: All pass (including the 5 new ones).

### Task 1 Step 8: Commit

```bash
git add app/services/scheduler.py app/api/schedules.py app/main.py \
        tests/conftest.py tests/test_api_schedules.py
git commit -m "feat: sync APScheduler with schedule API mutations at runtime"
```

---

## Task 2: Real Execution Cancellation

`stop_execution` currently only flips a DB flag — the browser keeps running. This task adds cooperative cancellation via `threading.Event` checked between steps.

**Files:**
- Create: `app/services/cancellation.py`
- Modify: `app/services/executor.py`
- Modify: `app/api/executions.py`
- Create: `tests/test_cancellation.py`
- Modify: `tests/test_executor.py` (add 1 new test)

---

### Task 2 Step 1: Write failing tests for CancellationRegistry

Create `tests/test_cancellation.py`:

```python
"""Tests for the CancellationRegistry service."""

import threading

from app.services.cancellation import cancel, register, unregister


def test_register_returns_unset_threading_event():
    event = register("exec-1")
    assert isinstance(event, threading.Event)
    assert not event.is_set()
    unregister("exec-1")  # cleanup


def test_cancel_sets_the_event():
    event = register("exec-2")
    result = cancel("exec-2")
    assert result is True
    assert event.is_set()
    unregister("exec-2")


def test_cancel_unknown_id_returns_false():
    result = cancel("does-not-exist")
    assert result is False


def test_unregister_prevents_future_cancel():
    register("exec-3")
    unregister("exec-3")
    result = cancel("exec-3")
    assert result is False
```

### Task 2 Step 2: Run to verify they fail

```bash
uv run pytest tests/test_cancellation.py -v
```

Expected: `FAILED` — `app.services.cancellation` doesn't exist.

### Task 2 Step 3: Create `app/services/cancellation.py`

```python
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
```

### Task 2 Step 4: Run cancellation tests to verify they pass

```bash
uv run pytest tests/test_cancellation.py -v
```

Expected: All 4 pass.

### Task 2 Step 5: Write failing executor cancellation test

Add this to `tests/test_executor.py`:

```python
@pytest.mark.asyncio
async def test_cancel_event_stops_execution_before_first_step():
    """A pre-set cancel event must result in status=cancelled with no steps run."""
    import threading

    cancel_event = threading.Event()
    cancel_event.set()  # already cancelled before execution starts

    wf = _make_workflow([
        WorkflowStep(order=1, name="Step 1", action="click", selector="#a"),
        WorkflowStep(order=2, name="Step 2", action="click", selector="#b"),
    ])

    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf, cancel_event=cancel_event)

    assert result.status == "cancelled"
    mock_driver.click.assert_not_called()
    mock_driver.close.assert_called_once()
```

### Task 2 Step 6: Run to verify it fails

```bash
uv run pytest tests/test_executor.py::test_cancel_event_stops_execution_before_first_step -v
```

Expected: `FAILED` — `execute_workflow` doesn't accept `cancel_event` yet.

### Task 2 Step 7: Update `app/services/executor.py`

Add `import threading` to the imports at the top.

Change `execute_workflow` signature (line 244):

```python
async def execute_workflow(
    workflow: Workflow,
    headless: bool | None = None,
    on_step_complete: Callable[[StepResult], None] | None = None,
    cancel_event: threading.Event | None = None,
    download_dir: str = "./downloads",
) -> ExecutionResult:
```

Inside `_run_sync`, add a cancellation check at the **top** of the step loop, before executing each step:

```python
        for i, step in enumerate(workflow.steps):
            # Cooperative cancellation — check between every step
            if cancel_event and cancel_event.is_set():
                break

            result = _execute_step_sync(driver, step, default_timeout)
            # ... rest of loop unchanged
```

After the step loop (before `finally`), add tracking for cancellation:

```python
        cancelled = cancel_event is not None and cancel_event.is_set()
```

Change the `ExecutionResult` construction after `await asyncio.to_thread(_run_sync)`:

```python
    # Check if cancelled (nonlocal variable set in _run_sync)
    # _run_sync sets 'cancelled' via closure
    total_ms = int((time.perf_counter() - start_time) * 1000)
    if cancelled:
        status = "cancelled"
    else:
        status = "completed" if failed == 0 else "failed"
```

> **Note on closures:** `_run_sync` is a nested function that shares `passed`, `failed`, and `results` via `nonlocal`. Add `cancelled = False` before `_run_sync` is defined, then set `nonlocal cancelled` inside `_run_sync` and assign `cancelled = cancel_event is not None and cancel_event.is_set()` after the loop.

Full updated `_run_sync` inner function with cancellation (replace the existing function body):

```python
    cancelled = False

    def _run_sync():
        """Synchronous execution in thread pool."""
        nonlocal passed, failed, cancelled

        driver = _create_driver(effective_headless)
        try:
            for i, step in enumerate(workflow.steps):
                # Cooperative cancellation — check before each step
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    break

                result = _execute_step_sync(driver, step, default_timeout)
                results.append(result)

                if on_step_complete:
                    on_step_complete(result)

                if result.status == "passed":
                    passed += 1
                else:
                    failed += 1
                    policy = step.on_error or global_policy
                    if policy == "retry":
                        policy = "stop"
                    if policy == "stop":
                        break

                if step_delay > 0 and i < len(workflow.steps) - 1:
                    time.sleep(step_delay / 1000)

        finally:
            try:
                driver.close()
            except Exception:
                pass

    await asyncio.to_thread(_run_sync)

    total_ms = int((time.perf_counter() - start_time) * 1000)
    if cancelled:
        status = "cancelled"
    else:
        status = "completed" if failed == 0 else "failed"
```

### Task 2 Step 8: Run executor tests

```bash
uv run pytest tests/test_executor.py -v
```

Expected: All pass including the new cancellation test.

### Task 2 Step 9: Update `app/api/executions.py`

Add to the imports:

```python
from app.services.cancellation import cancel, register, unregister
```

Update `_run_execution` to use **two short-lived sessions** with the browser execution in between. This is the session-per-unit-of-work pattern: never hold a DB connection open during long I/O. A 20-minute workflow with a single open session would exhaust the asyncpg connection pool under any real concurrency.

```python
async def _run_execution(
    execution_id: UUID,
    workflow: Workflow,
    session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, str] | None = None,
):
    from app.config import settings

    # --- Session 1: Mark as running, then close immediately ---
    async with session_factory() as db:
        result = await db.execute(select(Execution).where(Execution.id == execution_id))
        execution = result.scalar_one()
        execution.status = "running"
        execution.started_at = datetime.now(timezone.utc)
        await db.commit()
    # Session 1 closed — no connection held during browser execution

    # --- Browser execution — no DB connection held ---
    cancel_event = register(str(execution_id))
    exec_result: ExecutionResult | None = None
    try:
        resolved = resolve_variables(workflow, overrides=overrides)
        exec_result = await execute_workflow(
            resolved,
            cancel_event=cancel_event,
            download_dir=settings.download_dir,
        )
    except UnresolvedVariableError as e:
        exec_result = ExecutionResult(status="failed", error=str(e))
    except Exception as e:
        exec_result = ExecutionResult(status="failed", error=str(e))
    finally:
        unregister(str(execution_id))

    # --- Session 2: Write results, then close immediately ---
    async with session_factory() as db:
        result = await db.execute(select(Execution).where(Execution.id == execution_id))
        execution = result.scalar_one()

        # Respect external cancellation: stop_execution may have already
        # written "cancelled" to the DB while we were running.
        if execution.status != "cancelled":
            execution.status = exec_result.status
            execution.passed_steps = exec_result.passed
            execution.failed_steps = exec_result.failed
            execution.finished_at = datetime.now(timezone.utc)
            if exec_result.error:
                execution.error_message = exec_result.error

        for step_result in exec_result.steps:
            step_record = ExecutionStep(
                execution_id=execution_id,
                step_order=step_result.step_order,
                step_name=step_result.step_name,
                action=step_result.action,
                status=step_result.status,
                elapsed_ms=step_result.elapsed_ms,
                error_message=step_result.error,
                screenshot_path=step_result.screenshot_path,
            )
            db.add(step_record)

        await db.commit()
```

Update `stop_execution` to also signal the cancel event:

```python
@router.post("/api/executions/{execution_id}/stop")
async def stop_execution(execution_id: UUID, db: AsyncSession = Depends(get_db)):
    """Cancel a running execution."""
    result = await db.execute(select(Execution).where(Execution.id == execution_id))
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if execution.status not in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot stop execution in state: {execution.status}",
        )

    # Signal the executor thread to stop after the current step
    cancel(str(execution_id))

    # Update DB immediately for UI feedback (executor will also write "cancelled"
    # when it finishes the current step and sees the cancel event)
    execution.status = "cancelled"
    execution.finished_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "cancelled"}
```

### Task 2 Step 10: Run all execution and cancellation tests

```bash
uv run pytest tests/test_api_executions.py tests/test_cancellation.py tests/test_executor.py -v
```

Expected: All pass. The existing `test_stop_pending_execution` should still pass because `stop_execution` still writes "cancelled" to DB and returns `{"status": "cancelled"}`.

### Task 2 Step 11: Commit

```bash
git add app/services/cancellation.py app/services/executor.py app/api/executions.py \
        tests/test_cancellation.py tests/test_executor.py
git commit -m "feat: cooperative execution cancellation via threading.Event registry"
```

---

## Task 3: Real wait_for_download and wait_for_response

These two actions currently just sleep for the timeout. This task ports Scout's `NetworkMonitor` and wires the look-ahead pre-monitoring pattern from Scout's own CLI executor.

**Files:**
- Create: `app/services/network_monitor.py`
- Create: `tests/test_network_monitor.py`
- Modify: `app/services/executor.py`
- Modify: `tests/test_executor.py` (add 2 new tests)

---

### Task 3 Step 1: Write failing NetworkMonitor tests

Create `tests/test_network_monitor.py`:

```python
"""Tests for the NetworkMonitor service.

NetworkMonitor uses botasaurus-driver CDP callbacks. We test it by calling
the internal _on_response handler directly with mock objects — no browser needed.
"""

import threading
from unittest.mock import MagicMock

from app.services.network_monitor import NetworkMonitor


def _make_mock_response(url: str, content_disposition: str = "", status: int = 200):
    """Build a minimal mock response object."""
    mock = MagicMock()
    mock.url = url
    mock.status = status
    mock.headers = {"content-disposition": content_disposition} if content_disposition else {}
    mock.mime_type = "application/json"
    return mock


def test_query_returns_empty_before_any_events():
    monitor = NetworkMonitor()
    assert monitor.query() == []


def test_on_response_captures_regular_response():
    monitor = NetworkMonitor()
    monitor._monitoring = True

    monitor._on_response("req-1", _make_mock_response("https://api.example.com/data"), MagicMock())

    events = monitor.query()
    assert len(events) == 1
    assert events[0].url == "https://api.example.com/data"
    assert events[0].triggered_download is False


def test_on_response_detects_download_via_content_disposition():
    monitor = NetworkMonitor()
    monitor._monitoring = True

    monitor._on_response(
        "req-1",
        _make_mock_response(
            "https://example.com/report",
            content_disposition='attachment; filename="report.csv"',
        ),
        MagicMock(),
    )

    events = monitor.query()
    assert len(events) == 1
    assert events[0].triggered_download is True
    assert events[0].download_filename == "report.csv"


def test_query_filters_by_url_pattern():
    monitor = NetworkMonitor()
    monitor._monitoring = True

    monitor._on_response("r1", _make_mock_response("https://api.example.com/data"), MagicMock())
    monitor._on_response("r2", _make_mock_response("https://other.com/thing"), MagicMock())

    matched = monitor.query(url_pattern="api.example.com")
    assert len(matched) == 1
    assert "api.example.com" in matched[0].url


def test_wait_for_download_times_out_with_no_download():
    monitor = NetworkMonitor()
    events = monitor.wait_for_download(timeout_ms=100)
    assert events == []


def test_wait_for_download_returns_immediately_when_event_already_set():
    """Simulate: download fires in background, wait_for_download picks it up."""
    monitor = NetworkMonitor()
    monitor._monitoring = True

    def fire():
        monitor._on_response(
            "req-dl",
            _make_mock_response(
                "https://example.com/file.csv",
                content_disposition='attachment; filename="data.csv"',
            ),
            MagicMock(),
        )

    t = threading.Thread(target=fire)
    t.start()
    t.join()

    events = monitor.wait_for_download(timeout_ms=1000)
    assert len(events) == 1
    assert events[0].download_filename == "data.csv"


def test_stop_prevents_new_events_from_being_captured():
    monitor = NetworkMonitor()
    monitor._monitoring = True
    monitor.stop()

    monitor._on_response("req-1", _make_mock_response("https://example.com"), MagicMock())

    assert monitor.query() == []


def test_internal_chrome_urls_are_filtered():
    monitor = NetworkMonitor()
    monitor._monitoring = True

    monitor._on_response("r1", _make_mock_response("chrome://settings/"), MagicMock())
    monitor._on_response("r2", _make_mock_response("devtools://inspector"), MagicMock())

    assert monitor.query() == []
```

### Task 3 Step 2: Run to verify they fail

```bash
uv run pytest tests/test_network_monitor.py -v
```

Expected: `FAILED` — `app.services.network_monitor` doesn't exist.

### Task 3 Step 3: Create `app/services/network_monitor.py`

This is a focused port of Scout's `NetworkMonitor`. Body capture is omitted (MCP-only feature). The public API matches Scout's CLI executor usage exactly.

```python
"""CDP network monitoring — captures requests, responses, and download events.

Ported from Scout's NetworkMonitor (scout/src/scout/network.py).
Simplified: no body capture (not needed for workflow execution).
Public API matches Scout's CLI executor: start(), stop(), query(), wait_for_download().
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from botasaurus_driver import Driver

# Internal Chrome URL prefixes to filter out of captured events
_INTERNAL_PREFIXES = (
    "chrome://",
    "chrome-extension://",
    "chrome-untrusted://",
    "devtools://",
    "data:",
    "about:",
)


@dataclass
class NetworkEvent:
    """A single captured network response event."""

    url: str
    method: str = "GET"
    status: int | None = None
    triggered_download: bool = False
    download_filename: str | None = None


class NetworkMonitor:
    """Monitors network activity via botasaurus-driver's CDP callback system.

    Usage pattern (must start monitoring BEFORE the step that triggers the
    download/response — the look-ahead pattern from Scout's executor):

        monitor = NetworkMonitor()
        monitor.start(driver, url_pattern="api/export")  # before trigger step
        # ... execute trigger step (e.g. click download button) ...
        events = monitor.wait_for_download(timeout_ms=30000)
        monitor.stop()
    """

    def __init__(self) -> None:
        self._events: list[NetworkEvent] = []
        self._monitoring = False
        self._url_pattern: re.Pattern | None = None
        self._driver: Driver | None = None
        self._download_event = threading.Event()
        self._lock = threading.Lock()
        self._pending_requests: dict[str, dict] = {}

    def start(self, driver: Driver, url_pattern: str | None = None) -> None:
        """Start monitoring. Registers CDP callbacks on the driver.

        Args:
            driver: Active botasaurus Driver instance.
            url_pattern: Optional regex to filter captured events by URL.
        """
        self._driver = driver
        self._url_pattern = re.compile(url_pattern) if url_pattern else None
        self._monitoring = True
        self._download_event.clear()

        driver.before_request_sent(self._on_request)
        driver.after_response_received(self._on_response)

    def stop(self) -> None:
        """Stop capturing new events. Existing events are preserved for query()."""
        self._monitoring = False

    def query(self, url_pattern: str | None = None) -> list[NetworkEvent]:
        """Return captured events, optionally filtered by URL regex pattern."""
        with self._lock:
            events = list(self._events)
        if url_pattern:
            pat = re.compile(url_pattern)
            return [e for e in events if pat.search(e.url)]
        return events

    def wait_for_download(self, timeout_ms: int = 30000) -> list[NetworkEvent]:
        """Block until a download response is detected or timeout elapses.

        Returns list of events with triggered_download=True. Empty list on timeout.
        """
        self._download_event.wait(timeout=timeout_ms / 1000)
        with self._lock:
            return [e for e in self._events if e.triggered_download]

    # ── CDP callbacks (called on the websocket thread) ────────────────────────

    def _on_request(self, request_id: str, request, event) -> None:
        """Handle Network.requestWillBeSent — store request metadata."""
        if not self._monitoring:
            return

        url = request.url if hasattr(request, "url") else str(request)
        if any(url.startswith(p) for p in _INTERNAL_PREFIXES):
            return
        if self._url_pattern and not self._url_pattern.search(url):
            return

        method = request.method if hasattr(request, "method") else "GET"
        with self._lock:
            self._pending_requests[request_id] = {"url": url, "method": method}

    def _on_response(self, request_id: str, response, event) -> None:
        """Handle Network.responseReceived — build and store a NetworkEvent."""
        if not self._monitoring:
            return

        url = response.url if hasattr(response, "url") else ""
        if any(url.startswith(p) for p in _INTERNAL_PREFIXES):
            return
        if self._url_pattern and not self._url_pattern.search(url):
            return

        with self._lock:
            req_meta = self._pending_requests.pop(request_id, {})

        status = response.status if hasattr(response, "status") else None
        headers: dict = {}
        if hasattr(response, "headers") and response.headers:
            headers = (
                dict(response.headers)
                if not isinstance(response.headers, dict)
                else response.headers
            )

        # Detect file download via Content-Disposition: attachment
        content_disposition = headers.get(
            "content-disposition", headers.get("Content-Disposition", "")
        )
        is_download = (
            "attachment" in content_disposition.lower() if content_disposition else False
        )
        download_filename = None
        if is_download and "filename=" in content_disposition:
            download_filename = content_disposition.split("filename=")[-1].strip('" ')

        net_event = NetworkEvent(
            url=url or req_meta.get("url", ""),
            method=req_meta.get("method", "GET"),
            status=status,
            triggered_download=is_download,
            download_filename=download_filename,
        )

        with self._lock:
            self._events.append(net_event)

        if is_download:
            self._download_event.set()
```

### Task 3 Step 4: Run NetworkMonitor tests to verify they pass

```bash
uv run pytest tests/test_network_monitor.py -v
```

Expected: All 8 pass.

### Task 3 Step 5: Write failing executor tests for the two new actions

Add to `tests/test_executor.py`:

These tests use malformed workflows where the monitoring step is first (no preceding trigger), so `monitor._monitoring` is False and the guard fires immediately with a `RuntimeError`:

```python
@pytest.mark.asyncio
async def test_wait_for_download_without_preceding_trigger_fails_step():
    """wait_for_download as the first step (no trigger) must fail with a clear error."""
    wf = _make_workflow([
        WorkflowStep(
            order=1, name="Wait Download", action="wait_for_download",
            timeout_ms=100,
        ),
    ])

    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)

    assert result.status == "failed"
    assert result.steps[0].status == "failed"
    assert "network monitoring" in result.steps[0].error


@pytest.mark.asyncio
async def test_wait_for_response_without_preceding_trigger_fails_step():
    """wait_for_response as the first step (no trigger) must fail with a clear error."""
    wf = _make_workflow([
        WorkflowStep(
            order=1, name="Wait Response", action="wait_for_response",
            url_pattern="api/export", timeout_ms=100,
        ),
    ])

    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)

    assert result.status == "failed"
    assert result.steps[0].status == "failed"
    assert "network monitoring" in result.steps[0].error
```

### Task 3 Step 6: Run to verify they fail

```bash
uv run pytest tests/test_executor.py::test_wait_for_download_without_preceding_trigger_fails_step \
              tests/test_executor.py::test_wait_for_response_without_preceding_trigger_fails_step -v
```

Expected: `FAILED` — actions currently just sleep (no `_monitoring` check exists yet).

### Task 3 Step 7: Update `app/services/executor.py`

Add imports at the top of the file:

```python
import logging
import os

from app.services.network_monitor import NetworkMonitor
```

Add a new setup function after `_selector_targets_iframe`:

```python
def _setup_download_dir(driver, download_dir: str) -> None:
    """Configure Chrome to route downloads to download_dir using GUID filenames.

    Uses 'allowAndName' behavior so Chrome assigns GUID-based filenames,
    preventing collisions across concurrent executions. Matches Scout's pattern.
    """
    from botasaurus_driver import cdp

    os.makedirs(download_dir, exist_ok=True)
    try:
        driver.run_cdp_command(
            cdp.browser.set_download_behavior(
                behavior="allowAndName",
                download_path=os.path.realpath(download_dir),
                events_enabled=True,
            )
        )
    except Exception as e:
        logging.getLogger(__name__).warning("Failed to configure download dir: %s", e)
```

Update `_execute_step_sync` signature to accept the monitor:

```python
def _execute_step_sync(
    driver, step: WorkflowStep, default_timeout: int, monitor: NetworkMonitor | None = None
) -> StepResult:
```

Replace the `wait_for_download | wait_for_response` case (lines 221-225) with:

The guard uses `if not monitor._monitoring` rather than `if monitor is None` — the monitor object always exists (created at the top of `_run_sync`), but it is only started by the look-ahead when the previous step triggers a download/response. A workflow that places `wait_for_download` first with no preceding trigger step should get a clear error message, not a confusing timeout that looks like a network issue.

```python
            case "wait_for_download":
                timeout = step.timeout_ms or default_timeout
                if not monitor._monitoring:
                    raise RuntimeError(
                        "wait_for_download requires network monitoring to be active. "
                        "Ensure a click/navigate/download-trigger step immediately precedes this step."
                    )
                events = monitor.wait_for_download(timeout_ms=timeout)
                monitor.stop()
                if not events:
                    raise TimeoutError(
                        f"No download detected within {timeout}ms"
                    )

            case "wait_for_response":
                timeout = step.timeout_ms or default_timeout
                if not monitor._monitoring:
                    raise RuntimeError(
                        "wait_for_response requires network monitoring to be active. "
                        "Ensure a click/navigate step immediately precedes this step."
                    )
                deadline = time.perf_counter() + timeout / 1000
                matched = []
                while time.perf_counter() < deadline:
                    matched = monitor.query(step.url_pattern)
                    if matched:
                        break
                    time.sleep(0.5)
                monitor.stop()
                if not matched:
                    raise TimeoutError(
                        f"No response matching '{step.url_pattern}' within {timeout}ms"
                    )
```

Update `_run_sync` inside `execute_workflow` to wire the monitor and look-ahead:

```python
    def _run_sync():
        """Synchronous execution in thread pool."""
        nonlocal passed, failed, cancelled

        driver = _create_driver(effective_headless)
        monitor = NetworkMonitor()

        # Configure Chrome download directory (no-op if no download steps)
        _setup_download_dir(driver, download_dir)

        try:
            for i, step in enumerate(workflow.steps):
                # Cooperative cancellation
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    break

                # Look-ahead: start network monitoring BEFORE the step that
                # TRIGGERS the download/response (same pattern as Scout's executor).
                # The next step will call monitor.wait_for_download() or monitor.query().
                next_step = workflow.steps[i + 1] if i + 1 < len(workflow.steps) else None
                if next_step and next_step.action in ("wait_for_download", "wait_for_response"):
                    monitor.start(driver, url_pattern=next_step.url_pattern)

                result = _execute_step_sync(driver, step, default_timeout, monitor=monitor)
                results.append(result)

                if on_step_complete:
                    on_step_complete(result)

                if result.status == "passed":
                    passed += 1
                else:
                    failed += 1
                    policy = step.on_error or global_policy
                    if policy == "retry":
                        policy = "stop"
                    if policy == "stop":
                        break

                if step_delay > 0 and i < len(workflow.steps) - 1:
                    time.sleep(step_delay / 1000)

        finally:
            monitor.stop()
            try:
                driver.close()
            except Exception:
                pass
```

### Task 3 Step 8: Run all executor tests

```bash
uv run pytest tests/test_executor.py tests/test_network_monitor.py -v
```

Expected: All pass.

### Task 3 Step 9: Run the full test suite to check for regressions

```bash
uv run pytest -v
```

Expected: All tests pass.

### Task 3 Step 10: Commit

```bash
git add app/services/network_monitor.py app/services/executor.py \
        tests/test_network_monitor.py tests/test_executor.py
git commit -m "feat: port NetworkMonitor from Scout, implement wait_for_download/wait_for_response with look-ahead pre-monitoring"
```

---

## Task 4: Alembic Initial Migration

The `app/migrations/versions/` directory is empty — no migration has ever been generated. Running this task creates the versioned SQL migration file from the ORM models so any fresh PostgreSQL (or SQLite) database can be initialized with `alembic upgrade head`.

**Files:**
- Create: `app/migrations/versions/<hash>_initial_schema.py` (generated by Alembic)

---

### Task 4 Step 1: Ensure a test SQLite DB can be created

Verify the models are consistent before generating the migration:

```bash
cd d:/Projects/scout-engine
uv run pytest tests/test_models.py -v
```

Expected: All pass (SQLite in-memory schema creation works).

### Task 4 Step 2: Generate the migration

```bash
uv run alembic revision --autogenerate -m "initial schema"
```

Expected output: something like `Generating .../app/migrations/versions/abc123_initial_schema.py ... done`

### Task 4 Step 3: Review the generated file

Open the generated file in `app/migrations/versions/`. Verify it contains:
- `op.create_table("workflows", ...)` with all columns
- `op.create_table("schedules", ...)`
- `op.create_table("executions", ...)`
- `op.create_table("execution_steps", ...)`
- `op.create_index(...)` calls for indexed columns (`workflow_id`, `status`, `enabled`)
- A proper `downgrade()` that drops all tables in reverse order

If Alembic generated any `pass` in `upgrade()`, the `env.py` is not correctly pointing to `Base.metadata`. Check `app/migrations/env.py` — it should have `target_metadata = Base.metadata`.

### Task 4 Step 4: Run the migration against your local PostgreSQL

Ensure your `.env` has the correct local PostgreSQL URL (e.g. `postgresql+asyncpg://scout:scout@localhost:5432/scout_engine`) and the database exists. Then:

```bash
uv run alembic upgrade head
```

Expected: Output like:

```text
INFO  [alembic.runtime.migration] Running upgrade  -> abc123, initial schema
```

Verify the tables were created:

```bash
psql -U scout -d scout_engine -c "\dt"
```

Expected: `workflows`, `executions`, `execution_steps`, `schedules` all listed.

### Task 4 Step 5: Commit

```bash
git add app/migrations/versions/
git commit -m "feat: add initial Alembic schema migration"
```

---

## Final Verification

Run the complete test suite one last time:

```bash
uv run pytest -v --tb=short
```

Expected: All tests pass. No regressions.

---

## Summary of Changes

| File | Change |
|------|--------|
| `app/services/scheduler.py` | Rename `add_schedule_job` → `add_or_replace_schedule`, remove func/args params |
| `app/api/schedules.py` | Call `add_or_replace_schedule` / `remove_schedule_job` after each DB commit |
| `app/main.py` | Update import and lifespan call to use `add_or_replace_schedule` |
| `app/services/cancellation.py` | **New** — `register`, `cancel`, `unregister` with `threading.Lock`-guarded dict |
| `app/services/executor.py` | Accept `cancel_event` + `download_dir`; add look-ahead monitoring; implement download/response actions |
| `app/api/executions.py` | Wire cancellation registry in `_run_execution`; signal cancel event in `stop_execution` |
| `app/services/network_monitor.py` | **New** — Scout-compatible `NetworkMonitor` with CDP callbacks |
| `app/migrations/versions/*.py` | **New** — Alembic-generated initial schema migration |
| `tests/conftest.py` | +`reset_scheduler` autouse fixture (clears APScheduler jobs between tests) |
| `tests/test_api_schedules.py` | +5 scheduler-sync tests |
| `tests/test_cancellation.py` | **New** — 4 registry unit tests |
| `tests/test_executor.py` | +3 new tests (cancel, wait_for_download guard, wait_for_response guard) |
| `tests/test_network_monitor.py` | **New** — 8 NetworkMonitor unit tests |
