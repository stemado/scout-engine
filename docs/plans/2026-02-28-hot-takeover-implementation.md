# Hot Takeover Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable interactive browser session attachment by adding pause/resume lifecycle to scout-engine's executor, so external tools (Scout plugin, Chrome DevTools) can take over a running workflow mid-execution.

**Architecture:** Queue-based pause registry (mirrors cancellation.py pattern) with three triggers: explicit `handoff` steps, `pause_on_error` setting, and external `POST /pause` requests. The executor thread blocks on `queue.get()` while paused, keeping the browser alive. Resume instructions (retry/continue/abort/jump) dispatch from a new `POST /resume` endpoint.

**Tech Stack:** Python `threading`, `queue.Queue`, FastAPI endpoints, botasaurus-driver internals

**Design doc:** `docs/plans/2026-02-28-hot-takeover-design.md`

---

### Task 1: Pause Registry Module

**Files:**
- Create: `app/services/pause.py`
- Create: `tests/test_pause.py`

**Step 1: Write the failing tests**

Create `tests/test_pause.py`:

```python
"""Tests for the pause/resume registry service."""

import queue

from app.services.pause import (
    ResumeInstruction,
    register,
    request_pause,
    resume,
    unregister,
)


def test_register_returns_queue_and_event():
    pause_queue, pause_requested = register("exec-1")
    assert isinstance(pause_queue, queue.Queue)
    assert not pause_requested.is_set()
    unregister("exec-1")


def test_resume_puts_instruction_on_queue():
    pause_queue, _ = register("exec-2")
    instruction = ResumeInstruction(action="retry")
    result = resume("exec-2", instruction)
    assert result is True
    assert pause_queue.get_nowait() is instruction
    unregister("exec-2")


def test_resume_unknown_id_returns_false():
    result = resume("does-not-exist", ResumeInstruction(action="abort"))
    assert result is False


def test_request_pause_sets_event():
    _, pause_requested = register("exec-3")
    result = request_pause("exec-3")
    assert result is True
    assert pause_requested.is_set()
    unregister("exec-3")


def test_request_pause_unknown_id_returns_false():
    result = request_pause("does-not-exist")
    assert result is False


def test_unregister_prevents_future_resume():
    register("exec-4")
    unregister("exec-4")
    result = resume("exec-4", ResumeInstruction(action="continue"))
    assert result is False


def test_resume_instruction_with_step_index():
    pause_queue, _ = register("exec-5")
    instruction = ResumeInstruction(action="jump", step_index=6)
    resume("exec-5", instruction)
    got = pause_queue.get_nowait()
    assert got.action == "jump"
    assert got.step_index == 6
    unregister("exec-5")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pause.py -v`
Expected: `ModuleNotFoundError: No module named 'app.services.pause'`

**Step 3: Write the registry module**

Create `app/services/pause.py`:

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_pause.py -v`
Expected: 7 passed

**Step 5: Commit**

```
feat: add pause/resume registry with Queue-based instruction delivery
```

---

### Task 2: Schema Changes

**Files:**
- Modify: `app/schemas.py:27-36` (WorkflowSettings) and `app/schemas.py:46-50` (action Literal)
- Modify: `tests/test_schemas.py` (add 3 tests)

**Step 1: Write the failing tests**

Add to `tests/test_schemas.py`:

```python
def test_handoff_action_accepted():
    """handoff is a valid action type."""
    step = WorkflowStep(order=1, name="Manual entry", action="handoff", value="Check the form")
    assert step.action == "handoff"
    assert step.value == "Check the form"


def test_pause_on_error_defaults_false():
    """pause_on_error defaults to False for backward compatibility."""
    settings = WorkflowSettings()
    assert settings.pause_on_error is False


def test_pause_on_error_explicit_true():
    """pause_on_error can be set to True."""
    settings = WorkflowSettings(pause_on_error=True)
    assert settings.pause_on_error is True
```

**Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_schemas.py::test_handoff_action_accepted -v`
Expected: `ValidationError` — `'handoff'` not in Literal

**Step 3: Modify the schema**

In `app/schemas.py`, two changes:

**A. Add `pause_on_error` to WorkflowSettings** (line 36, after `on_error`):

```python
class WorkflowSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headless: bool = False
    human_mode: bool = True
    default_timeout_ms: int = Field(default=30000, ge=0)
    step_delay_ms: int = Field(default=500, ge=0)
    on_error: Literal["stop", "continue", "retry"] = "stop"
    pause_on_error: bool = False
```

**B. Add `"handoff"` to the action Literal** (lines 46-50):

```python
    action: Literal[
        "navigate", "click", "type", "select", "scroll", "wait",
        "wait_for_download", "wait_for_response",
        "press_key", "hover", "clear", "run_js", "handoff",
    ]
```

**Step 4: Run tests**

Run: `uv run pytest tests/test_schemas.py -v`
Expected: All tests pass (existing + 3 new)

**Step 5: Commit**

```
feat: add handoff action and pause_on_error setting to workflow schema
```

---

### Task 3: Browser Session Enhancements

**Files:**
- Modify: `app/services/browser_session.py` (add state, PausedStepInfo, new functions)
- Modify: `tests/test_browser_session.py` (add 5 tests)

**Step 1: Write the failing tests**

Add to `tests/test_browser_session.py`:

```python
from app.services.browser_session import (
    BrowserSessionInfo,
    PausedStepInfo,
    StepProgress,
    get_session,
    register,
    set_paused,
    set_running,
    unregister,
    update_step,
)


def test_initial_state_is_running():
    info = _make_info("exec-state-1")
    register("exec-state-1", info)
    assert info.state == "running"
    assert info.paused_at_step is None
    unregister("exec-state-1")


def test_set_paused_updates_state_and_step_info():
    register("exec-state-2", _make_info("exec-state-2"))
    paused_info = PausedStepInfo(
        step_order=3, step_name="Click submit", action="click",
        reason="Step failed", error="Element not found: #submit-btn",
    )
    set_paused("exec-state-2", "paused_error", paused_info)
    info = get_session("exec-state-2")
    assert info.state == "paused_error"
    assert info.paused_at_step.step_order == 3
    assert info.paused_at_step.error == "Element not found: #submit-btn"
    assert info.paused_at_step.reason == "Step failed"
    unregister("exec-state-2")


def test_set_running_clears_paused_state():
    register("exec-state-3", _make_info("exec-state-3"))
    set_paused("exec-state-3", "paused_handoff", PausedStepInfo(
        step_order=5, step_name="Manual", action="handoff",
        reason="Check form", error=None,
    ))
    set_running("exec-state-3")
    info = get_session("exec-state-3")
    assert info.state == "running"
    assert info.paused_at_step is None
    unregister("exec-state-3")


def test_set_paused_noop_for_unknown_id():
    set_paused("does-not-exist", "paused_error", PausedStepInfo(
        step_order=1, step_name="X", action="click", reason="X", error="X",
    ))


def test_set_running_noop_for_unknown_id():
    set_running("does-not-exist")
```

**Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_browser_session.py::test_initial_state_is_running -v`
Expected: `ImportError` — `PausedStepInfo` does not exist

**Step 3: Modify browser_session.py**

Update `app/services/browser_session.py` — add `PausedStepInfo` dataclass, add `state` and `paused_at_step` fields to `BrowserSessionInfo`, add `set_paused()` and `set_running()` functions:

```python
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
```

**Step 4: Update the existing test imports**

At the top of `tests/test_browser_session.py`, update the import block to include the new symbols:

```python
from app.services.browser_session import (
    BrowserSessionInfo,
    PausedStepInfo,
    StepProgress,
    get_session,
    register,
    set_paused,
    set_running,
    unregister,
    update_step,
)
```

**Step 5: Run tests**

Run: `uv run pytest tests/test_browser_session.py -v`
Expected: All tests pass (7 existing + 5 new = 12)

**Step 6: Commit**

```
feat: add pause state and PausedStepInfo to browser session registry
```

---

### Task 4: Executor Pause/Resume Integration

This is the largest task. The executor's step loop must be converted from a `for` loop to a `while` loop (to support retry and jump), and three pause triggers must be wired in.

**Files:**
- Modify: `app/services/executor.py:344-497` (execute_workflow + _run_sync)
- Modify: `tests/test_executor.py` (add 6 tests)

**Step 1: Write the failing tests**

Add to `tests/test_executor.py`:

```python
import queue as _queue
from app.services.pause import ResumeInstruction


@pytest.mark.asyncio
async def test_handoff_step_pauses_and_resumes_with_continue():
    """A handoff step pauses execution; resume with 'continue' advances to next step."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Nav", action="navigate", value="https://example.com"),
        WorkflowStep(order=2, name="Manual", action="handoff", value="Check the form"),
        WorkflowStep(order=3, name="Click", action="click", selector="#btn"),
    ])
    mock_driver = _mock_driver_with_cdp()
    pause_q = _queue.Queue(maxsize=1)

    async def resume_later():
        await asyncio.sleep(0.1)
        pause_q.put(ResumeInstruction(action="continue"))

    with patch("app.services.executor._create_driver", return_value=mock_driver):
        task = asyncio.create_task(resume_later())
        result = await execute_workflow(
            wf, execution_id="test-handoff", pause_queue=pause_q,
        )
        await task

    assert result.status == "completed"
    assert result.passed == 3  # navigate + handoff + click
    assert len(result.steps) == 3
    assert result.steps[1].action == "handoff"
    assert result.steps[1].status == "passed"


@pytest.mark.asyncio
async def test_handoff_step_without_pause_queue_passes_through():
    """A handoff step without a pause_queue is a no-op (passes immediately)."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Manual", action="handoff", value="Check"),
        WorkflowStep(order=2, name="Click", action="click", selector="#btn"),
    ])
    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)
    assert result.status == "completed"
    assert result.passed == 2


@pytest.mark.asyncio
async def test_pause_on_error_pauses_then_retries():
    """When pause_on_error=True and a step fails, executor pauses; retry re-runs the step."""
    wf = _make_workflow(
        [WorkflowStep(order=1, name="Click", action="click", selector="#btn")],
        settings=WorkflowSettings(pause_on_error=True, human_mode=False, step_delay_ms=0),
    )
    mock_driver = _mock_driver_with_cdp()
    # First call fails, second call (retry) succeeds
    mock_driver.click.side_effect = [Exception("Not found"), None]
    pause_q = _queue.Queue(maxsize=1)

    async def resume_with_retry():
        await asyncio.sleep(0.1)
        pause_q.put(ResumeInstruction(action="retry"))

    with patch("app.services.executor._create_driver", return_value=mock_driver):
        task = asyncio.create_task(resume_with_retry())
        result = await execute_workflow(
            wf, execution_id="test-retry", pause_queue=pause_q,
        )
        await task

    assert result.status == "completed"
    assert result.passed == 1
    # Two step results: first failed, second passed (retry)
    assert len(result.steps) == 2
    assert result.steps[0].status == "failed"
    assert result.steps[1].status == "passed"


@pytest.mark.asyncio
async def test_pause_on_error_no_infinite_loop():
    """After retry fails again, on_error policy applies (no second pause)."""
    wf = _make_workflow(
        [WorkflowStep(order=1, name="Click", action="click", selector="#btn")],
        settings=WorkflowSettings(pause_on_error=True, on_error="stop", human_mode=False, step_delay_ms=0),
    )
    mock_driver = _mock_driver_with_cdp()
    mock_driver.click.side_effect = Exception("Always fails")
    pause_q = _queue.Queue(maxsize=1)

    async def resume_with_retry():
        await asyncio.sleep(0.1)
        pause_q.put(ResumeInstruction(action="retry"))

    with patch("app.services.executor._create_driver", return_value=mock_driver):
        task = asyncio.create_task(resume_with_retry())
        result = await execute_workflow(
            wf, execution_id="test-no-loop", pause_queue=pause_q,
        )
        await task

    assert result.status == "failed"
    assert result.failed == 2  # original fail + retry fail
    assert len(result.steps) == 2


@pytest.mark.asyncio
async def test_resume_with_abort_cancels_execution():
    """Resume with 'abort' terminates execution."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Manual", action="handoff", value="Check"),
        WorkflowStep(order=2, name="Click", action="click", selector="#btn"),
    ])
    mock_driver = _mock_driver_with_cdp()
    pause_q = _queue.Queue(maxsize=1)

    async def resume_with_abort():
        await asyncio.sleep(0.1)
        pause_q.put(ResumeInstruction(action="abort"))

    with patch("app.services.executor._create_driver", return_value=mock_driver):
        task = asyncio.create_task(resume_with_abort())
        result = await execute_workflow(
            wf, execution_id="test-abort", pause_queue=pause_q,
        )
        await task

    assert result.status == "cancelled"
    mock_driver.click.assert_not_called()


@pytest.mark.asyncio
async def test_resume_with_jump_skips_to_step():
    """Resume with 'jump' skips to the specified step index."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Nav", action="navigate", value="https://example.com"),
        WorkflowStep(order=2, name="Manual", action="handoff", value="Check"),
        WorkflowStep(order=3, name="Skip me", action="click", selector="#skip"),
        WorkflowStep(order=4, name="Target", action="click", selector="#target"),
    ])
    mock_driver = _mock_driver_with_cdp()
    pause_q = _queue.Queue(maxsize=1)

    async def resume_with_jump():
        await asyncio.sleep(0.1)
        pause_q.put(ResumeInstruction(action="jump", step_index=3))

    with patch("app.services.executor._create_driver", return_value=mock_driver):
        task = asyncio.create_task(resume_with_jump())
        result = await execute_workflow(
            wf, execution_id="test-jump", pause_queue=pause_q,
        )
        await task

    assert result.status == "completed"
    # Step 3 "Skip me" should NOT have been executed
    mock_driver.click.assert_called_once_with("#target")
```

Note: These tests require `import asyncio` at the top of `tests/test_executor.py`. Add it if not already present.

**Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_executor.py::test_handoff_step_pauses_and_resumes_with_continue -v`
Expected: `TypeError` — `execute_workflow() got an unexpected keyword argument 'pause_queue'`

**Step 3: Modify the executor**

The changes to `app/services/executor.py` are substantial. Here is the complete modified section.

**A. Add `pause_queue` parameter** to `execute_workflow()` (line 344):

```python
async def execute_workflow(
    workflow: Workflow,
    headless: bool | None = None,
    on_step_complete: Callable[[StepResult], None] | None = None,
    cancel_event: threading.Event | None = None,
    download_dir: str = "./downloads",
    execution_id: str | None = None,
    pause_queue: queue.Queue | None = None,
    pause_requested: threading.Event | None = None,
) -> ExecutionResult:
```

Add `import queue` to the imports at the top of the file.

**B. Add `_wait_for_resume` helper** after `_human_delay` (after line 163):

```python
def _wait_for_resume(
    execution_id: str,
    pause_queue: queue.Queue,
    state: str,
    step: WorkflowStep,
    reason: str | None = None,
    error: str | None = None,
    timeout: int = 1800,
):
    """Block the executor thread until a resume instruction arrives.

    Updates browser session state to paused, blocks on queue.get(),
    then restores state to running. Returns the ResumeInstruction.
    On timeout, returns an abort instruction.
    """
    from app.services.browser_session import PausedStepInfo, set_paused, set_running
    from app.services.pause import ResumeInstruction

    set_paused(execution_id, state, PausedStepInfo(
        step_order=step.order,
        step_name=step.name,
        action=step.action,
        reason=reason,
        error=error,
    ))

    try:
        instruction = pause_queue.get(timeout=timeout)
    except queue.Empty:
        logging.getLogger(__name__).warning(
            "Pause timeout for %s after %ds — aborting", execution_id, timeout,
        )
        instruction = ResumeInstruction(action="abort")

    set_running(execution_id)
    return instruction


def _apply_resume(instruction, current_idx: int) -> tuple[int | None, bool]:
    """Apply a resume instruction. Returns (new_step_idx, advance_normally).

    new_step_idx is None for abort.
    advance_normally indicates whether the caller should increment step_idx after.
    """
    match instruction.action:
        case "retry":
            return current_idx, False
        case "continue":
            return current_idx + 1, False
        case "abort":
            return None, False
        case "jump":
            return instruction.step_index, False
        case _:
            return current_idx + 1, False
```

**C. Replace the entire `_run_sync` inner function** (lines 378-481) with the while-loop version:

```python
    def _run_sync():
        """Synchronous execution in thread pool."""
        nonlocal passed, failed, cancelled

        driver = _create_driver(effective_headless)
        human_mode = workflow.settings.human_mode
        if human_mode:
            driver.enable_human_mode()
        monitor = NetworkMonitor()

        # Configure Chrome download directory (no-op if no download steps)
        _setup_download_dir(driver, download_dir)

        # Browser session attachment: import once, use throughout
        _bs_register = _bs_unregister = _bs_update_step = _bs_StepProgress = None
        if execution_id:
            from app.services.browser_session import (
                BrowserSessionInfo,
                StepProgress as _BSStepProgress,
                register as _bs_register,
                unregister as _bs_unregister,
                update_step as _bs_update_step,
            )
            _bs_StepProgress = _BSStepProgress
            try:
                _host = driver._browser.config.host
                _port = driver._browser.config.port
                _ws = driver._browser.websocket_url
                _bs_register(execution_id, BrowserSessionInfo(
                    execution_id=execution_id,
                    cdp_host=_host,
                    cdp_port=_port,
                    cdp_websocket_url=_ws,
                    targets_url=f"http://{_host}:{_port}/json/list",
                    devtools_frontend_url=f"http://{_host}:{_port}",
                ))
            except Exception:
                logging.getLogger(__name__).warning(
                    "Could not register browser session for %s", execution_id, exc_info=True,
                )

        pause_on_error = workflow.settings.pause_on_error

        try:
            step_idx = 0
            while step_idx < len(workflow.steps):
                step = workflow.steps[step_idx]

                # Cooperative cancellation -- check before each step
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    break

                # External pause request -- check before each step
                if pause_requested and pause_requested.is_set():
                    pause_requested.clear()
                    if pause_queue and execution_id:
                        instruction = _wait_for_resume(
                            execution_id, pause_queue, "paused_requested", step,
                            reason="Developer-requested pause",
                        )
                        new_idx, _ = _apply_resume(instruction, step_idx)
                        if new_idx is None:
                            cancelled = True
                            break
                        step_idx = new_idx
                        continue

                # Network monitor look-ahead
                next_step = (
                    workflow.steps[step_idx + 1]
                    if step_idx + 1 < len(workflow.steps) else None
                )
                if next_step and next_step.action in (
                    "wait_for_download", "wait_for_response",
                ):
                    monitor.start(driver, url_pattern=next_step.url_pattern)

                # --- Handoff step: pause for interactive takeover ---
                if step.action == "handoff":
                    if _bs_update_step:
                        _bs_update_step(execution_id, _bs_StepProgress(
                            step_order=step.order, step_name=step.name,
                            action=step.action, started_at=time.monotonic(),
                        ))

                    handoff_result = StepResult(
                        step_order=step.order, step_name=step.name,
                        action="handoff", status="passed", elapsed_ms=0,
                    )

                    if pause_queue and execution_id:
                        instruction = _wait_for_resume(
                            execution_id, pause_queue, "paused_handoff", step,
                            reason=step.value or "Handoff to agent",
                        )
                        new_idx, _ = _apply_resume(instruction, step_idx)
                        if new_idx is None:
                            cancelled = True
                            handoff_result.status = "failed"
                            handoff_result.error = "Aborted during handoff"
                            results.append(handoff_result)
                            if on_step_complete:
                                on_step_complete(handoff_result)
                            break

                    results.append(handoff_result)
                    passed += 1
                    if on_step_complete:
                        on_step_complete(handoff_result)
                    if _bs_update_step:
                        _bs_update_step(execution_id, None)

                    # Apply delay if advancing to next step
                    if step_idx + 1 < len(workflow.steps):
                        if human_mode:
                            _human_delay(step, step_delay)
                        elif step_delay > 0:
                            time.sleep(step_delay / 1000)

                    step_idx = new_idx if (pause_queue and execution_id) else step_idx + 1
                    continue

                # --- Normal step execution ---
                if _bs_update_step:
                    _bs_update_step(execution_id, _bs_StepProgress(
                        step_order=step.order, step_name=step.name,
                        action=step.action, started_at=time.monotonic(),
                    ))

                result = _execute_step_sync(
                    driver, step, default_timeout,
                    monitor=monitor, human_mode=human_mode,
                )
                results.append(result)

                if _bs_update_step:
                    _bs_update_step(execution_id, None)

                if on_step_complete:
                    on_step_complete(result)

                if result.status == "passed":
                    passed += 1
                else:
                    failed += 1
                    # Pause-on-error: pause instead of applying on_error
                    already_paused = getattr(step, '_already_paused', False)
                    if (
                        pause_on_error and pause_queue and execution_id
                        and not already_paused
                    ):
                        step._already_paused = True
                        instruction = _wait_for_resume(
                            execution_id, pause_queue, "paused_error", step,
                            reason="Step failed",
                            error=result.error,
                        )
                        new_idx, _ = _apply_resume(instruction, step_idx)
                        if new_idx is None:
                            cancelled = True
                            break
                        step_idx = new_idx
                        continue
                    else:
                        # Apply on_error policy (original behavior)
                        policy = step.on_error or global_policy
                        if policy == "retry":
                            policy = "stop"
                        if policy == "stop":
                            break

                # Apply delay between steps (not after the last one)
                if step_idx < len(workflow.steps) - 1:
                    if human_mode:
                        _human_delay(step, step_delay)
                    elif step_delay > 0:
                        time.sleep(step_delay / 1000)

                step_idx += 1

        finally:
            monitor.stop()
            # Execute cleanup steps (best-effort, errors don't change final status)
            for cleanup_step in workflow.cleanup_steps:
                try:
                    _execute_step_sync(
                        driver, cleanup_step, default_timeout,
                        human_mode=human_mode,
                    )
                except Exception:
                    pass  # best-effort — don't mask the original error
            # Unregister browser session after cleanup but before driver.close()
            if _bs_unregister:
                _bs_unregister(execution_id)
            try:
                driver.close()
            except Exception:
                pass
```

**Important note on `already_paused`:** The plan uses `step._already_paused` as an attribute on the Pydantic model. Since WorkflowStep uses `extra="forbid"`, we cannot set arbitrary attributes. Instead, use a local `set` to track which step indices have been paused:

Replace `step._already_paused = True` / `getattr(step, '_already_paused', False)` with:

```python
# Before the while loop (inside _run_sync, after pause_on_error):
_paused_step_indices: set[int] = set()

# In the failure handling:
if (
    pause_on_error and pause_queue and execution_id
    and step_idx not in _paused_step_indices
):
    _paused_step_indices.add(step_idx)
    ...
```

**Step 4: Run all executor tests**

Run: `uv run pytest tests/test_executor.py -v`
Expected: All tests pass (23 existing + 6 new = 29)

**Step 5: Run full suite to check for regressions**

Run: `uv run pytest -v`
Expected: All tests pass

**Step 6: Commit**

```
feat: add pause/resume lifecycle to executor with handoff, pause-on-error, and external pause
```

---

### Task 5: API Endpoints

**Files:**
- Modify: `app/api/executions.py` (add 2 endpoints, enhance 1, wire pause to _run_execution)
- Modify: `tests/test_api_executions.py` (add 5 tests)

**Step 1: Write the failing tests**

Add to `tests/test_api_executions.py`:

```python
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


async def test_pause_unknown_execution(client):
    """POST /pause for unknown execution returns 404."""
    resp = await client.post("/api/executions/00000000-0000-0000-0000-000000000000/pause")
    assert resp.status_code == 404


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


async def test_resume_unknown_execution(client):
    """POST /resume for unknown execution returns 404."""
    resp = await client.post(
        "/api/executions/00000000-0000-0000-0000-000000000000/resume",
        json={"action": "continue"},
    )
    assert resp.status_code == 404


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
```

**Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_api_executions.py::test_pause_running_execution -v`
Expected: 404 — endpoint doesn't exist

**Step 3: Modify the API**

**A. Wire pause registration in `_run_execution`** (~line 46):

Replace the existing cancellation + execute_workflow block with:

```python
    cancel_event = register(str(execution_id))
    exec_result: ExecutionResult | None = None

    # Register pause/resume handle
    from app.services.pause import (
        register as pause_register,
        unregister as pause_unregister,
    )
    pause_queue, pause_requested = pause_register(str(execution_id))

    try:
        resolved = resolve_variables(workflow, overrides=overrides)
        exec_result = await execute_workflow(
            resolved,
            cancel_event=cancel_event,
            download_dir=settings.download_dir,
            execution_id=str(execution_id),
            pause_queue=pause_queue,
            pause_requested=pause_requested,
        )
    except UnresolvedVariableError as e:
        exec_result = ExecutionResult(status="failed", error=str(e))
    except Exception as e:
        exec_result = ExecutionResult(status="failed", error=str(e))
    finally:
        unregister(str(execution_id))
        pause_unregister(str(execution_id))
```

**B. Add `POST /pause` endpoint** (after the stop endpoint):

```python
@router.post("/api/executions/{execution_id}/pause")
async def pause_execution(execution_id: UUID):
    """Request a pause for a running execution (takes effect after current step)."""
    from app.services.pause import request_pause

    if not request_pause(str(execution_id)):
        raise HTTPException(
            status_code=404,
            detail="No active execution found. It may have already finished.",
        )
    return {"status": "pause_requested"}
```

**C. Add `POST /resume` endpoint:**

```python
@router.post("/api/executions/{execution_id}/resume")
async def resume_execution(execution_id: UUID, body: dict):
    """Send a resume instruction to a paused execution."""
    from app.services.pause import ResumeInstruction, resume

    action = body.get("action")
    if action not in ("retry", "continue", "abort", "jump"):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid action: {action}. Must be retry, continue, abort, or jump.",
        )

    instruction = ResumeInstruction(
        action=action,
        step_index=body.get("step_index"),
    )

    if not resume(str(execution_id), instruction):
        raise HTTPException(
            status_code=404,
            detail="No active execution found. It may have already finished.",
        )
    return {"status": "resumed", "action": action}
```

**D. Enhance `GET /browser` response** to include `state` and `paused_at_step`:

Replace the return dict in `get_browser_session` with:

```python
    return {
        "execution_id": info.execution_id,
        "cdp_host": info.cdp_host,
        "cdp_port": info.cdp_port,
        "cdp_websocket_url": info.cdp_websocket_url,
        "targets_url": info.targets_url,
        "devtools_frontend_url": info.devtools_frontend_url,
        "state": info.state,
        "paused_at_step": {
            "step_order": info.paused_at_step.step_order,
            "step_name": info.paused_at_step.step_name,
            "action": info.paused_at_step.action,
            "reason": info.paused_at_step.reason,
            "error": info.paused_at_step.error,
        } if info.paused_at_step else None,
        "current_step": {
            "step_order": info.current_step.step_order,
            "step_name": info.current_step.step_name,
            "action": info.current_step.action,
        } if info.current_step else None,
    }
```

**Step 4: Run API tests**

Run: `uv run pytest tests/test_api_executions.py -v`
Expected: All tests pass (9 existing + 5 new = 14)

**Step 5: Run full test suite**

Run: `uv run pytest -v`
Expected: All tests pass

**Step 6: Commit**

```
feat: add POST /pause, POST /resume endpoints and wire pause lifecycle
```

---

### Task 6: Update CLAUDE.md and Final Verification

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Add new endpoints to CLAUDE.md API section**

Add after the existing browser endpoint line:

```
- `POST /api/executions/{id}/pause` -- Request pause (takes effect after current step)
- `POST /api/executions/{id}/resume` -- Resume a paused execution (retry/continue/abort/jump)
```

**Step 2: Run full test suite one final time**

Run: `uv run pytest -v`
Expected: All tests pass

**Step 3: Commit**

```
docs: add pause/resume endpoints to CLAUDE.md
```

---

### Task 7: Scout Plugin — AttachedDriver (separate codebase)

> **Note:** This task and Task 8 are in the Scout plugin codebase, not scout-engine. They should be implemented in a separate session against the Scout plugin repo.

**Files:**
- Create: `src/scout/attached_driver.py` (in Scout plugin repo)
- Create: `tests/test_attached_driver.py` (guard tests)

**Implementation:**

Create `AttachedBrowser(Browser)` subclass that overrides:
- `create_chrome_with_retries()` — skip subprocess.Popen, poll existing Chrome's `/json/version`
- `close()` — close CDP connections only, never kill Chrome

Create `AttachedDriver` that constructs a real `Config(headless=False)` with host/port overwritten, then creates `AttachedBrowser` instead of `Browser`.

**Guard tests** (run against botasaurus-driver updates):
- `Browser` has a callable `create_chrome_with_retries` method
- `Browser.close` calls `close_tab_connections` and `close_browser_connection` as discrete operations
- Verify `Config` accepts `host` and `port` parameters

**Commit:**
```
feat: add AttachedDriver for connecting to existing Chrome via CDP
```

---

### Task 8: Scout Plugin — /attach and /resume Commands (separate codebase)

> **Note:** This task is in the Scout plugin codebase.

**Files:**
- Create: `commands/attach.md` (slash command definition)
- Create: `commands/resume.md` (slash command definition)
- Modify: `src/scout/session.py` (add `owns_browser` flag, `detach()` method)
- Modify: `src/scout/server.py` (add `attach_session` MCP tool)

**Implementation:**

`/attach [execution_id]`:
1. Query scout-engine `GET /api/executions` for paused executions (if no ID given)
2. `GET /api/executions/{id}/browser` for CDP info
3. If running: offer to pause, poll until paused
4. Create `AttachedDriver(host, port)` and `BrowserSession(driver, owns_browser=False)`
5. Auto-scout, display pause context

`/resume <action> [step_index]`:
1. `POST /api/executions/{id}/resume` with instruction
2. Wait for 200 acknowledgment
3. `session.detach()` — drops CDP connection, does NOT close browser
4. Report result

**Commit:**
```
feat: add /attach and /resume commands for Hot Takeover
```
