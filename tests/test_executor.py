"""Tests for the workflow executor service.

These tests mock botasaurus-driver to avoid needing a real browser.
Integration tests with a real browser are marked separately.
"""

import asyncio
import queue as _queue

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from app.schemas import Workflow, WorkflowSettings, WorkflowStep
from app.services.executor import ExecutionResult, StepResult, execute_workflow
from app.services.pause import ResumeInstruction


def _make_workflow(steps, **kwargs) -> Workflow:
    defaults = {"name": "test", "variables": {}, "steps": steps}
    defaults.update(kwargs)
    return Workflow(**defaults)


@pytest.mark.asyncio
async def test_execute_navigate_step():
    """Should execute a navigate step and report success."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Navigate", action="navigate", value="https://example.com"),
    ])

    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)

    assert result.status == "completed"
    assert result.passed == 1
    assert result.failed == 0
    assert len(result.steps) == 1
    assert result.steps[0].status == "passed"
    mock_driver.get.assert_called_once_with("https://example.com")
    mock_driver.close.assert_called_once()


@pytest.mark.asyncio
async def test_execute_click_step():
    """Should execute a click step."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Click button", action="click", selector="#submit"),
    ])

    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)

    assert result.status == "completed"
    mock_driver.click.assert_called_once_with("#submit")


@pytest.mark.asyncio
async def test_execute_type_step_with_clear():
    """Should clear then type when clear_first is True (non-human mode)."""
    wf = _make_workflow(
        [WorkflowStep(order=1, name="Type", action="type", selector="#input", value="hello", clear_first=True)],
        settings=WorkflowSettings(human_mode=False),
    )

    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)

    assert result.status == "completed"
    mock_driver.clear.assert_called_once_with("#input")
    mock_driver.type.assert_called_once_with("#input", "hello")


@pytest.mark.asyncio
async def test_step_failure_stops_execution():
    """With on_error='stop', a failing step should halt execution."""
    wf = _make_workflow(
        [
            WorkflowStep(order=1, name="Click bad", action="click", selector="#bad"),
            WorkflowStep(order=2, name="Click good", action="click", selector="#good"),
        ],
        settings=WorkflowSettings(on_error="stop"),
    )

    mock_driver = MagicMock()
    mock_driver.click.side_effect = [Exception("Element not found"), None]
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)

    assert result.status == "failed"
    assert result.failed == 1
    assert result.passed == 0
    # Second step should not have been attempted
    assert len(result.steps) == 1


@pytest.mark.asyncio
async def test_step_failure_continues():
    """With on_error='continue', execution should proceed past failures."""
    wf = _make_workflow(
        [
            WorkflowStep(order=1, name="Click bad", action="click", selector="#bad"),
            WorkflowStep(order=2, name="Click good", action="click", selector="#good"),
        ],
        settings=WorkflowSettings(on_error="continue"),
    )

    mock_driver = MagicMock()
    mock_driver.click.side_effect = [Exception("Not found"), None]
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)

    assert result.status == "failed"  # Overall still failed because 1 step failed
    assert result.failed == 1
    assert result.passed == 1
    assert len(result.steps) == 2


@pytest.mark.asyncio
async def test_step_delay_applied():
    """Step delay should be applied between steps (non-human mode, fixed delay)."""
    wf = _make_workflow(
        [
            WorkflowStep(order=1, name="Step 1", action="click", selector="#a"),
            WorkflowStep(order=2, name="Step 2", action="click", selector="#b"),
        ],
        settings=WorkflowSettings(human_mode=False, step_delay_ms=100),
    )

    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver), \
         patch("time.sleep") as mock_sleep:
        result = await execute_workflow(wf)

    assert result.status == "completed"
    mock_sleep.assert_called_once_with(0.1)


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


@pytest.mark.asyncio
async def test_human_mode_enables_human_mode_on_driver():
    """When human_mode is True, executor must call driver.enable_human_mode()."""
    wf = _make_workflow(
        [WorkflowStep(order=1, name="Type username", action="type", selector="input#user", value="admin")],
        settings=WorkflowSettings(human_mode=True, step_delay_ms=0),
    )
    mock_driver = MagicMock()
    mock_elem = MagicMock()
    mock_driver.wait_for_element.return_value = mock_elem
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        await execute_workflow(wf)
    mock_driver.enable_human_mode.assert_called_once()


@pytest.mark.asyncio
async def test_human_mode_delegates_typing_to_driver():
    """When human_mode is True, type action should delegate to driver.type()
    with the full string, letting botasaurus's enable_human_mode() handle
    per-keystroke timing internally (avoids double humanization)."""
    wf = _make_workflow(
        [WorkflowStep(order=1, name="Type username", action="type", selector="input#user", value="admin")],
        settings=WorkflowSettings(human_mode=True, step_delay_ms=0),
    )
    mock_driver = MagicMock()

    with patch("app.services.executor._create_driver", return_value=mock_driver), \
         patch("app.services.executor.random"):
        await execute_workflow(wf)

    # Should call driver.type() with the full string, not char-by-char
    mock_driver.type.assert_called_once_with("input#user", "admin")


@pytest.mark.asyncio
async def test_human_mode_false_skips_human_mode():
    """When human_mode is False, should NOT call enable_human_mode and should type normally."""
    wf = _make_workflow(
        [WorkflowStep(order=1, name="Type", action="type", selector="input#user", value="admin")],
        settings=WorkflowSettings(human_mode=False, step_delay_ms=0),
    )
    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        await execute_workflow(wf)
    mock_driver.enable_human_mode.assert_not_called()
    mock_driver.type.assert_called_once_with("input#user", "admin")


@pytest.mark.asyncio
async def test_human_mode_uses_randomized_delays():
    """When human_mode is True, delays should use _human_delay, not fixed sleep."""
    wf = _make_workflow(
        [
            WorkflowStep(order=1, name="Nav", action="navigate", value="https://a.com"),
            WorkflowStep(order=2, name="Click", action="click", selector="#btn"),
        ],
        settings=WorkflowSettings(human_mode=True, step_delay_ms=500),
    )
    mock_driver = MagicMock()
    mock_driver.wait_for_element.return_value = MagicMock()

    with patch("app.services.executor._create_driver", return_value=mock_driver), \
         patch("app.services.executor._human_delay") as mock_delay:
        await execute_workflow(wf)

    # _human_delay should have been called once (between step 1 and step 2)
    mock_delay.assert_called_once()
    # The first arg should be the navigate step (delay is applied AFTER the step)
    call_args = mock_delay.call_args
    assert call_args[0][0].action == "navigate"
    assert call_args[0][1] == 500  # base_delay_ms


@pytest.mark.asyncio
async def test_human_mode_false_uses_fixed_delays():
    """When human_mode is False, step delays should be fixed (original behavior)."""
    wf = _make_workflow(
        [
            WorkflowStep(order=1, name="Nav", action="navigate", value="https://a.com"),
            WorkflowStep(order=2, name="Click", action="click", selector="#btn"),
        ],
        settings=WorkflowSettings(human_mode=False, step_delay_ms=200),
    )
    mock_driver = MagicMock()

    with patch("app.services.executor._create_driver", return_value=mock_driver), \
         patch("app.services.executor._human_delay") as mock_human_delay, \
         patch("time.sleep") as mock_sleep:
        await execute_workflow(wf)

    # _human_delay should NOT have been called
    mock_human_delay.assert_not_called()
    # Fixed sleep should have been called with 0.2 (200ms)
    mock_sleep.assert_called_once_with(0.2)


@pytest.mark.asyncio
async def test_cleanup_steps_run_on_failure():
    """cleanup_steps should execute even when a main step fails."""
    wf = Workflow.model_validate({
        "schema_version": "1.0",
        "name": "cleanup-test",
        "settings": {"human_mode": False, "step_delay_ms": 0},
        "steps": [
            {"order": 1, "name": "Will fail", "action": "click", "selector": "#missing"},
        ],
        "cleanup_steps": [
            {"order": 1, "name": "Logout", "action": "navigate", "value": "https://example.com/logout"},
        ],
    })
    mock_driver = MagicMock()
    mock_driver.click.side_effect = Exception("Element not found")
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)

    assert result.status == "failed"
    # Cleanup step should still have been called
    mock_driver.get.assert_called_once_with("https://example.com/logout")


@pytest.mark.asyncio
async def test_cleanup_steps_run_on_success():
    """cleanup_steps should also run when the workflow succeeds."""
    wf = Workflow.model_validate({
        "schema_version": "1.0",
        "name": "cleanup-success-test",
        "settings": {"human_mode": False, "step_delay_ms": 0},
        "steps": [
            {"order": 1, "name": "Navigate", "action": "navigate", "value": "https://example.com"},
        ],
        "cleanup_steps": [
            {"order": 1, "name": "Logout", "action": "navigate", "value": "https://example.com/logout"},
        ],
    })
    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)

    assert result.status == "completed"
    # Both the main navigate and cleanup navigate should have been called
    assert len(mock_driver.get.call_args_list) == 2
    mock_driver.get.assert_any_call("https://example.com")
    mock_driver.get.assert_any_call("https://example.com/logout")


@pytest.mark.asyncio
async def test_cleanup_step_failure_does_not_mask_main_error():
    """If a cleanup step fails, it should not change the workflow result."""
    wf = Workflow.model_validate({
        "schema_version": "1.0",
        "name": "cleanup-error-test",
        "settings": {"human_mode": False, "step_delay_ms": 0},
        "steps": [
            {"order": 1, "name": "Navigate", "action": "navigate", "value": "https://example.com"},
        ],
        "cleanup_steps": [
            {"order": 1, "name": "Bad cleanup", "action": "click", "selector": "#nonexistent"},
        ],
    })
    mock_driver = MagicMock()
    mock_driver.click.side_effect = Exception("Cleanup failed")
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)

    # Main workflow should still be "completed" despite cleanup failure
    assert result.status == "completed"
    assert result.passed == 1


@pytest.mark.asyncio
async def test_run_js_action():
    """run_js action should execute JavaScript via driver.run_js()."""
    wf = _make_workflow(
        [WorkflowStep(order=1, name="Run JS", action="run_js", value="return document.title")],
        settings=WorkflowSettings(human_mode=False, step_delay_ms=0),
    )
    mock_driver = MagicMock()
    mock_driver.run_js.return_value = "Test Page"
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)
    assert result.status == "completed"
    assert result.steps[0].status == "passed"
    mock_driver.run_js.assert_called_once_with("return document.title")


@pytest.mark.asyncio
async def test_run_js_action_with_frame_context():
    """run_js with frame_context should execute JS in the iframe."""
    wf = _make_workflow(
        [WorkflowStep(order=1, name="Run JS in iframe", action="run_js",
                      value="return document.title", frame_context="iframe#content")],
        settings=WorkflowSettings(human_mode=False, step_delay_ms=0),
    )
    mock_driver = MagicMock()
    mock_iframe = MagicMock()
    mock_driver.select_iframe.return_value = mock_iframe
    mock_iframe.run_js.return_value = "Iframe Title"
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)
    assert result.status == "completed"
    mock_iframe.run_js.assert_called_once_with("return document.title")


def _mock_driver_with_cdp():
    """Create a mock driver with CDP browser internals for session tests."""
    mock_driver = MagicMock()
    mock_browser = MagicMock()
    mock_config = MagicMock()
    mock_config.host = "127.0.0.1"
    mock_config.port = 51234
    mock_browser.config = mock_config
    mock_browser.websocket_url = "ws://127.0.0.1:51234/devtools/browser/fake-guid"
    mock_driver._browser = mock_browser
    return mock_driver


@pytest.mark.asyncio
async def test_browser_session_registered_when_execution_id_provided():
    """Executor registers CDP session info when execution_id is provided."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Nav", action="navigate", value="https://example.com"),
    ])
    mock_driver = _mock_driver_with_cdp()

    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf, execution_id="test-exec-1")

    assert result.status == "completed"
    # Session should have been unregistered by the finally block
    from app.services.browser_session import get_session
    assert get_session("test-exec-1") is None


@pytest.mark.asyncio
async def test_browser_session_not_registered_without_execution_id():
    """No registration when execution_id is not provided (backward compat)."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Nav", action="navigate", value="https://example.com"),
    ])
    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_browser_session_unregistered_on_failure():
    """Session is unregistered even when a step fails."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Bad click", action="click", selector="#bad"),
    ])
    mock_driver = _mock_driver_with_cdp()
    mock_driver.click.side_effect = Exception("Not found")

    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf, execution_id="test-fail")

    assert result.status == "failed"
    from app.services.browser_session import get_session
    assert get_session("test-fail") is None


@pytest.mark.asyncio
async def test_browser_session_step_progress_updated():
    """Executor updates step progress before each step and clears after."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Nav", action="navigate", value="https://example.com"),
        WorkflowStep(order=2, name="Click", action="click", selector="#btn"),
    ])
    mock_driver = _mock_driver_with_cdp()

    with patch("app.services.executor._create_driver", return_value=mock_driver), \
         patch("app.services.browser_session.update_step") as mock_update:
        await execute_workflow(wf, execution_id="test-progress")

    # Pattern: set(step1), clear(None), set(step2), clear(None)
    assert mock_update.call_count == 4
    assert mock_update.call_args_list[0][0][1].step_order == 1
    assert mock_update.call_args_list[1][0][1] is None
    assert mock_update.call_args_list[2][0][1].step_order == 2
    assert mock_update.call_args_list[3][0][1] is None


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


@pytest.mark.asyncio
async def test_screenshot_captured_per_step(tmp_path):
    """Each step should produce a screenshot PNG in the screenshot dir."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Navigate", action="navigate", value="https://example.com"),
    ])
    mock_driver = MagicMock()
    # Simulate CDP returning base64 PNG data (1x1 transparent pixel)
    import base64
    fake_png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    mock_driver.run_cdp_command.return_value = fake_png

    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(
            wf, screenshot_dir=str(tmp_path), execution_id="ss-test",
        )

    assert result.status == "completed"
    assert result.steps[0].screenshot_path is not None
    from pathlib import Path
    ss_path = Path(result.steps[0].screenshot_path)
    assert ss_path.exists()
    assert ss_path.suffix == ".png"
    # File should be inside {screenshot_dir}/{execution_id}/
    assert ss_path.parent.name == "ss-test"


@pytest.mark.asyncio
async def test_screenshot_not_captured_without_screenshot_dir():
    """When screenshot_dir is empty, no screenshot should be captured."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Navigate", action="navigate", value="https://example.com"),
    ])
    mock_driver = MagicMock()
    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(wf)

    assert result.steps[0].screenshot_path is None


@pytest.mark.asyncio
async def test_download_dir_scoped_to_execution(tmp_path):
    """Downloads should go to {download_dir}/{execution_id}/."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Nav", action="navigate", value="https://example.com"),
    ])
    exec_id = "test-exec-dl-123"
    mock_driver = MagicMock()

    with patch("app.services.executor._create_driver", return_value=mock_driver):
        await execute_workflow(
            wf, download_dir=str(tmp_path), execution_id=exec_id,
        )

    expected_dir = tmp_path / exec_id
    assert expected_dir.exists()


@pytest.mark.asyncio
async def test_screenshot_failure_does_not_fail_step(tmp_path):
    """If screenshot capture fails, the step should still pass."""
    wf = _make_workflow([
        WorkflowStep(order=1, name="Navigate", action="navigate", value="https://example.com"),
    ])
    mock_driver = MagicMock()
    mock_driver.run_cdp_command.side_effect = Exception("CDP error")

    with patch("app.services.executor._create_driver", return_value=mock_driver):
        result = await execute_workflow(
            wf, screenshot_dir=str(tmp_path), execution_id="ss-fail",
        )

    assert result.status == "completed"
    assert result.steps[0].status == "passed"
    assert result.steps[0].screenshot_path is None
