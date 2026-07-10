"""Workflow execution engine -- runs workflow steps via botasaurus-driver.

Adapted from Scout's executor.py for async service context. The actual browser
operations are synchronous (botasaurus-driver is sync), so they run in a thread
pool via asyncio.to_thread().
"""

from __future__ import annotations

import asyncio
import glob as glob_module
import json
import logging
import os
import queue
import random
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests as _requests_lib

from app.schemas import Workflow, WorkflowStep
from app.services.network_monitor import NetworkMonitor
from app.services.variables import resolve_step_runtime


@dataclass
class StepResult:
    """Result of executing a single workflow step."""

    step_order: int
    step_name: str
    action: str
    status: str  # passed / failed
    elapsed_ms: int = 0
    error: str | None = None
    screenshot_path: str | None = None
    output_data: Any | None = None


@dataclass
class ExecutionResult:
    """Result of executing an entire workflow."""

    status: str  # completed / failed / cancelled
    passed: int = 0
    failed: int = 0
    total_ms: int = 0
    steps: list[StepResult] = field(default_factory=list)
    error: str | None = None
    outputs: dict[str, Any] | None = None


@dataclass
class RuntimeContext:
    """Inter-step data flow context that travels through execution."""

    step_outputs: dict[str, Any] = field(default_factory=dict)
    loop_stack: list = field(default_factory=list)  # list[LoopFrame]
    files: dict[str, str] = field(default_factory=dict)


@dataclass
class LoopFrame:
    """State for a single loop iteration."""

    items: list
    current_index: int
    loop_var: str
    loop_index_var: str | None
    body_start: int
    body_end: int


def _create_driver(headless: bool):
    """Create a botasaurus-driver instance.

    Isolated as a factory function so tests can mock it without importing
    botasaurus-driver (which requires Chrome).
    """
    from botasaurus_driver import Driver

    return Driver(headless=headless)


def _resolve_target(driver, frame_context: str | None):
    """Resolve iframe context. Returns driver or iframe handle.

    Supports chained iframe selectors separated by '>>>'.
    """
    if not frame_context:
        return driver
    parts = [p.strip() for p in frame_context.split(">>>")]
    target = driver
    for part in parts:
        iframe = target.select_iframe(part)
        if iframe is None:
            raise ValueError(f"Iframe not found: {part}")
        target = iframe
    return target


# Key map for press_key action -- maps key names to CDP key event parameters
_KEY_MAP = {
    "enter": {"key": "Enter", "code": "Enter", "keyCode": 13},
    "tab": {"key": "Tab", "code": "Tab", "keyCode": 9},
    "escape": {"key": "Escape", "code": "Escape", "keyCode": 27},
    "space": {"key": " ", "code": "Space", "keyCode": 32},
    "backspace": {"key": "Backspace", "code": "Backspace", "keyCode": 8},
    "delete": {"key": "Delete", "code": "Delete", "keyCode": 46},
    "arrowup": {"key": "ArrowUp", "code": "ArrowUp", "keyCode": 38},
    "arrowdown": {"key": "ArrowDown", "code": "ArrowDown", "keyCode": 40},
    "arrowleft": {"key": "ArrowLeft", "code": "ArrowLeft", "keyCode": 37},
    "arrowright": {"key": "ArrowRight", "code": "ArrowRight", "keyCode": 39},
    "home": {"key": "Home", "code": "Home", "keyCode": 36},
    "end": {"key": "End", "code": "End", "keyCode": 35},
    "pageup": {"key": "PageUp", "code": "PageUp", "keyCode": 33},
    "pagedown": {"key": "PageDown", "code": "PageDown", "keyCode": 34},
}

# Pattern to detect selectors that target <iframe> elements
_IFRAME_PATTERN = re.compile(r"^iframe(?:$|[#.\[:(])", re.IGNORECASE)


def _selector_targets_iframe(selector: str) -> bool:
    """Check if a CSS selector's final target is an <iframe> element."""
    parts = re.split(r"[\s>+~]+", selector.strip())
    final = parts[-1] if parts else selector
    return bool(_IFRAME_PATTERN.match(final))


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


def _human_type(target, selector: str, text: str) -> None:
    """Type text with per-keystroke jitter to mimic human typing.

    Matches the human_type() pattern from Scout's export-workflow command:
    30-120ms random delay between each keystroke.
    """
    elem = target.wait_for_element(selector, wait=10)
    elem.click()
    for char in text:
        elem.type(char)
        time.sleep(random.uniform(0.03, 0.12))


def _human_delay(step: WorkflowStep, base_delay_ms: int) -> None:
    """Apply a randomized inter-step delay based on the action type.

    Mirrors the anti-detection timing from Scout's export-workflow:
    - After navigate: 2-4s (page loads vary; fixed waits are a bot signal)
    - After click (form submit): 0.8-2.0s (general interaction pacing)
    - Between same-page interactions: 0.3-0.8s
    """
    match step.action:
        case "navigate":
            time.sleep(random.uniform(2.0, 4.0))
        case "click":
            time.sleep(random.uniform(0.8, 2.0))
        case "type":
            # human_type already includes per-keystroke delays, keep gap short
            time.sleep(random.uniform(0.2, 0.5))
        case "select":
            time.sleep(random.uniform(0.3, 0.8))
        case _:
            # Fallback: randomize around the base delay
            base_s = base_delay_ms / 1000
            time.sleep(random.uniform(base_s * 0.5, base_s * 1.5))


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


def _execute_step_sync(
    driver, step: WorkflowStep, default_timeout: int,
    monitor: NetworkMonitor | None = None,
    human_mode: bool = False,
    screenshot_dir: str | None = None,
    context: RuntimeContext | None = None,
) -> StepResult:
    """Execute a single step synchronously. Called from thread pool."""
    start = time.perf_counter()
    error = None
    output_data = None

    try:
        target = _resolve_target(driver, step.frame_context)
        timeout_s = (step.timeout_ms or default_timeout) / 1000

        match step.action:
            case "navigate":
                driver.get(step.value)

            case "navigate_new_tab":
                # Opens the URL in a new tab and switches driver focus to it.
                # Needed after a cross-domain navigate, where staying in the
                # original tab breaks CDP DOM sync (observed with Google warmup
                # followed by a different-origin navigate).
                driver.open_link_in_new_tab(step.value)

            case "click":
                if step.selector and _selector_targets_iframe(step.selector):
                    raise ValueError(
                        f"Cannot click an <iframe> directly (selector: {step.selector}). "
                        f"Use frame_context to interact with iframe content."
                    )
                target.click(step.selector)

            case "type":
                if step.clear_first:
                    target.clear(step.selector)
                if human_mode:
                    # Let botasaurus's enable_human_mode() handle keystroke
                    # timing internally — do NOT use _human_type() here, as
                    # it loops single chars and botasaurus applies its own
                    # human-mode overhead to each one (double humanization).
                    target.type(step.selector, step.value)
                else:
                    target.type(step.selector, step.value)

            case "select":
                target.select_option(step.selector, value=step.value)

            case "scroll":
                lowered = (step.value or "").strip().lower()
                if lowered == "top":
                    target.run_js("window.scrollTo(0, 0)")
                elif lowered == "bottom":
                    target.run_js(
                        "window.scrollTo(0, document.body.scrollHeight)"
                    )
                else:
                    amount = 500  # default pixels
                    if step.value:
                        lowered_val = step.value.strip().lower()
                        if lowered_val == "down":
                            amount = 500
                        elif lowered_val == "up":
                            amount = -500
                        else:
                            amount = int(step.value)
                    target.run_js(f"window.scrollBy(0, {json.dumps(amount)})")

            case "wait":
                if step.selector:
                    target.wait_for_element(step.selector, wait=timeout_s)
                elif step.value:
                    time.sleep(int(step.value) / 1000)
                else:
                    time.sleep(1)

            case "press_key":
                from botasaurus_driver import cdp

                key_info = _KEY_MAP.get(
                    step.value.lower(),
                    {
                        "key": step.value,
                        "code": f"Key{step.value.upper()}",
                        "keyCode": 0,
                    },
                )
                driver.run_cdp_command(
                    cdp.input_.dispatch_key_event(
                        type_="keyDown",
                        key=key_info["key"],
                        code=key_info["code"],
                        windows_virtual_key_code=key_info.get("keyCode", 0),
                    )
                )
                driver.run_cdp_command(
                    cdp.input_.dispatch_key_event(
                        type_="keyUp",
                        key=key_info["key"],
                        code=key_info["code"],
                        windows_virtual_key_code=key_info.get("keyCode", 0),
                    )
                )

            case "hover":
                sel_js = json.dumps(step.selector)
                hover_js = (
                    "const el = document.querySelector("
                    + sel_js
                    + ");"
                    + "if (!el) return null;"
                    + "const rect = el.getBoundingClientRect();"
                    + "return {x: rect.x + rect.width/2, y: rect.y + rect.height/2};"
                )
                pos = driver.run_js(hover_js)
                if pos:
                    from botasaurus_driver import cdp

                    driver.run_cdp_command(
                        cdp.input_.dispatch_mouse_event(
                            type_="mouseMoved",
                            x=pos["x"],
                            y=pos["y"],
                        )
                    )
                else:
                    raise ValueError(
                        f"Element not found for hover: {step.selector}"
                    )

            case "clear":
                target.clear(step.selector)

            case "run_js":
                ret = target.run_js(step.value)
                if step.output_var:
                    output_data = ret

            case "extract":
                if not step.output_var:
                    raise ValueError("extract action requires output_var field")
                result_value = target.run_js(step.value)
                output_data = result_value

            case "conditional":
                val = step.value
                cond = step.condition or "truthy"
                condition_met = False
                match cond:
                    case "truthy":
                        condition_met = bool(
                            val is not None
                            and str(val).lower() not in ("", "false", "null", "0")
                        )
                    case "falsy":
                        condition_met = (
                            val is None
                            or str(val).lower() in ("", "false", "null", "0")
                        )
                    case "equals":
                        condition_met = str(val) == str(step.compare_to)
                    case "contains":
                        condition_met = str(step.compare_to) in str(val)
                    case "matches":
                        condition_met = re.search(str(step.compare_to), str(val)) is not None
                    case _:
                        raise ValueError(f"Unknown condition: {cond}")
                output_data = {
                    "condition_met": condition_met,
                    "skip_steps": step.skip_steps,
                    "jump_to": step.jump_to,
                }

            case "loop":
                items = step.value
                if isinstance(items, str):
                    try:
                        items = json.loads(items)
                    except (json.JSONDecodeError, TypeError):
                        items = []
                if not isinstance(items, list):
                    items = [items] if items is not None else []
                output_data = {
                    "items": items,
                    "loop_var": step.loop_var,
                    "loop_index_var": step.loop_index_var,
                    "loop_steps": step.loop_steps,
                }

            case "fill_secret":
                if step.clear_first:
                    target.clear(step.selector)
                target.type(step.selector, step.value)
                output_data = {"chars_typed": len(step.value) if step.value else 0}

            case "upload_file":
                # Use CDP to set files on a file input element
                file_path = step.value
                elem = target.select(step.selector)
                if hasattr(elem, "upload") and callable(getattr(elem, "upload")):
                    elem.upload(file_path)
                else:
                    # Fallback: use CDP File.setFileInputFiles via run_js
                    # Get the backend node id
                    from botasaurus_driver import cdp
                    node_info = driver.run_cdp_command(
                        cdp.dom.get_document(depth=0)
                    )
                    js_selector = json.dumps(step.selector)
                    node_id = driver.run_js(
                        f"return document.querySelector({js_selector})"
                    )
                    if node_id is None:
                        raise ValueError(f"File input not found: {step.selector}")
                    # Use Input.dispatchDragEvent or set via JS
                    driver.run_cdp_command(
                        cdp.dom.set_file_input_files(
                            files=[os.path.realpath(file_path)],
                            object_id=node_id,
                        )
                    )
                output_data = {"file": file_path}

            case "http_request":
                method = (step.method or "GET").upper()
                headers = step.headers or {}
                timeout_val = (step.timeout_ms or 30000) / 1000
                auth = None
                if step.auth and step.auth.startswith("basic:"):
                    parts = step.auth.split(":", 2)
                    if len(parts) >= 3:
                        auth = (parts[1], parts[2])
                resp = _requests_lib.request(
                    method, step.value,
                    headers=headers, data=step.body,
                    auth=auth, timeout=timeout_val,
                )
                try:
                    resp_data = resp.json()
                except (ValueError, TypeError):
                    resp_data = resp.text
                output_data = {
                    "status_code": resp.status_code,
                    "body": resp_data,
                    "headers": dict(resp.headers),
                }

            case "file_op":
                match step.operation:
                    case "mkdir":
                        os.makedirs(step.destination, exist_ok=True)
                        output_data = step.destination
                    case "copy":
                        shutil.copy2(step.source, step.destination)
                        output_data = step.destination
                    case "write":
                        os.makedirs(os.path.dirname(step.destination), exist_ok=True)
                        with open(step.destination, "w", encoding="utf-8") as f:
                            f.write(step.content or "")
                        output_data = step.destination
                    case "glob":
                        matches = glob_module.glob(step.pattern, root_dir=step.source)
                        output_data = json.dumps([os.path.join(step.source, m) for m in matches])
                    case "list":
                        entries = os.listdir(step.source)
                        output_data = json.dumps([os.path.join(step.source, e) for e in sorted(entries)])
                    case _:
                        raise ValueError(f"Unknown file_op operation: {step.operation}")

            case "wait_for_download":
                timeout = step.timeout_ms or default_timeout
                if not monitor or not monitor.is_active:
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
                if not monitor or not monitor.is_active:
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

            case _:
                raise ValueError(f"Unknown action: {step.action}")

    except Exception as e:
        error = str(e)

    # Capture screenshot after step (regardless of pass/fail)
    screenshot_path = None
    if screenshot_dir:
        try:
            import base64
            from botasaurus_driver import cdp as _cdp

            os.makedirs(screenshot_dir, exist_ok=True)
            filename = f"{step.order:03d}_{step.action}.png"
            filepath = os.path.join(screenshot_dir, filename)
            b64_data = driver.run_cdp_command(
                _cdp.page.capture_screenshot(format_="png")
            )
            # CDP may return raw string or tuple — normalize
            if isinstance(b64_data, (list, tuple)):
                b64_data = b64_data[0]
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(b64_data))
            screenshot_path = filepath
        except Exception:
            logging.getLogger(__name__).debug(
                "Screenshot capture failed for step %d", step.order, exc_info=True,
            )

    elapsed = int((time.perf_counter() - start) * 1000)
    return StepResult(
        step_order=step.order,
        step_name=step.name,
        action=step.action,
        status="passed" if error is None else "failed",
        elapsed_ms=elapsed,
        error=error,
        screenshot_path=screenshot_path,
        output_data=output_data if error is None else None,
    )


async def execute_workflow(
    workflow: Workflow,
    headless: bool | None = None,
    on_step_complete: Callable[[StepResult], None] | None = None,
    cancel_event: threading.Event | None = None,
    download_dir: str = "./downloads",
    execution_id: str | None = None,
    pause_queue: queue.Queue | None = None,
    pause_requested: threading.Event | None = None,
    screenshot_dir: str = "",
) -> ExecutionResult:
    """Execute a workflow asynchronously.

    Runs the synchronous botasaurus-driver operations in a thread pool.
    Calls on_step_complete(StepResult) after each step if provided.

    Args:
        workflow: The workflow to execute (with variables already resolved).
        headless: Override the workflow's headless setting. None uses workflow default.
        on_step_complete: Optional callback invoked after each step completes.

    Returns:
        ExecutionResult with overall status and per-step results.
    """
    effective_headless = (
        headless if headless is not None else workflow.settings.headless
    )
    default_timeout = workflow.settings.default_timeout_ms
    step_delay = workflow.settings.step_delay_ms
    global_policy = workflow.settings.on_error

    start_time = time.perf_counter()
    results: list[StepResult] = []
    passed = 0
    failed = 0
    cancelled = False
    context = RuntimeContext()

    def _run_sync():
        """Synchronous execution in thread pool."""
        nonlocal passed, failed, cancelled

        driver = _create_driver(effective_headless)
        human_mode = workflow.settings.human_mode
        if human_mode:
            driver.enable_human_mode()
        monitor = NetworkMonitor()

        # Configure Chrome download directory scoped per execution
        effective_download_dir = os.path.join(
            download_dir, execution_id or "default",
        )
        _setup_download_dir(driver, effective_download_dir)

        # Build per-execution screenshot directory
        effective_screenshot_dir = None
        if screenshot_dir:
            effective_screenshot_dir = os.path.join(
                screenshot_dir, execution_id or "default",
            )

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
            _paused_step_indices: set[int] = set()
            _retrying_after_pause = False
            while step_idx < len(workflow.steps):
                step = workflow.steps[step_idx]

                # Resolve runtime variables (from extract/http_request outputs)
                step = resolve_step_runtime(step, context)

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

                    new_idx = step_idx + 1  # default: advance to next step
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
                    if new_idx < len(workflow.steps):
                        if human_mode:
                            _human_delay(step, step_delay)
                        elif step_delay > 0:
                            time.sleep(step_delay / 1000)

                    step_idx = new_idx
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
                    screenshot_dir=effective_screenshot_dir,
                    context=context,
                )
                results.append(result)

                # Capture output_var into runtime context
                if step.output_var and result.status == "passed":
                    context.step_outputs[step.output_var] = result.output_data
                    # For http_request, also store the body as a JSON string
                    # under "{output_var}_body" so it can be interpolated into
                    # browser JS via ${var_body} and parsed with JSON.parse().
                    if (
                        step.action == "http_request"
                        and isinstance(result.output_data, dict)
                        and "body" in result.output_data
                    ):
                        body = result.output_data["body"]
                        if isinstance(body, (dict, list)):
                            context.step_outputs[f"{step.output_var}_body"] = json.dumps(body)
                        else:
                            context.step_outputs[f"{step.output_var}_body"] = str(body)

                if _bs_update_step:
                    _bs_update_step(execution_id, None)

                if on_step_complete:
                    on_step_complete(result)

                if result.status == "passed":
                    passed += 1
                    if _retrying_after_pause:
                        # Successful retry: un-count the original failure
                        failed -= 1
                        _retrying_after_pause = False
                else:
                    _retrying_after_pause = False
                    failed += 1
                    # Pause-on-error: pause instead of applying on_error
                    if (
                        pause_on_error and pause_queue and execution_id
                        and step_idx not in _paused_step_indices
                    ):
                        _paused_step_indices.add(step_idx)
                        instruction = _wait_for_resume(
                            execution_id, pause_queue, "paused_error", step,
                            reason="Step failed",
                            error=result.error,
                        )
                        new_idx, _ = _apply_resume(instruction, step_idx)
                        if new_idx is None:
                            cancelled = True
                            break
                        if instruction.action == "retry":
                            _retrying_after_pause = True
                        step_idx = new_idx
                        continue
                    else:
                        # Apply on_error policy (original behavior)
                        policy = step.on_error or global_policy
                        if policy == "retry":
                            policy = "stop"
                        if policy == "stop":
                            break

                # --- Conditional skip/jump logic ---
                if (
                    step.action == "conditional"
                    and result.status == "passed"
                    and result.output_data
                ):
                    od = result.output_data
                    if od.get("condition_met"):
                        if od.get("skip_steps"):
                            step_idx += od["skip_steps"]
                        elif od.get("jump_to") is not None:
                            step_idx = od["jump_to"] - 1

                # --- Loop initiation logic ---
                if (
                    step.action == "loop"
                    and result.status == "passed"
                    and result.output_data
                ):
                    od = result.output_data
                    items = od.get("items", [])
                    loop_steps = od.get("loop_steps") or 0
                    loop_var = od.get("loop_var", "_item")
                    loop_index_var = od.get("loop_index_var")
                    if not items:
                        # Empty list: skip the body
                        step_idx += loop_steps
                    else:
                        frame = LoopFrame(
                            items=items,
                            current_index=0,
                            loop_var=loop_var,
                            loop_index_var=loop_index_var,
                            body_start=step_idx + 1,
                            body_end=step_idx + loop_steps,
                        )
                        context.loop_stack.append(frame)
                        context.step_outputs[loop_var] = items[0]
                        if loop_index_var:
                            context.step_outputs[loop_index_var] = 0

                # --- Loop iteration check (after every step) ---
                if context.loop_stack and step.action != "loop":
                    frame = context.loop_stack[-1]
                    if step_idx == frame.body_end:
                        frame.current_index += 1
                        if frame.current_index < len(frame.items):
                            context.step_outputs[frame.loop_var] = frame.items[frame.current_index]
                            if frame.loop_index_var:
                                context.step_outputs[frame.loop_index_var] = frame.current_index
                            step_idx = frame.body_start - 1
                        else:
                            context.loop_stack.pop()

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

    await asyncio.to_thread(_run_sync)

    total_ms = int((time.perf_counter() - start_time) * 1000)
    if cancelled:
        status = "cancelled"
    else:
        status = "completed" if failed == 0 else "failed"

    return ExecutionResult(
        status=status,
        passed=passed,
        failed=failed,
        total_ms=total_ms,
        steps=results,
        outputs=dict(context.step_outputs) if context.step_outputs else None,
    )
