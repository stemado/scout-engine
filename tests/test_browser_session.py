"""Tests for the browser session registry service."""

import time

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


def _make_info(execution_id: str = "exec-1") -> BrowserSessionInfo:
    return BrowserSessionInfo(
        execution_id=execution_id,
        cdp_host="127.0.0.1",
        cdp_port=51234,
        cdp_websocket_url="ws://127.0.0.1:51234/devtools/browser/fake-guid",
        targets_url="http://127.0.0.1:51234/json/list",
        devtools_frontend_url="http://127.0.0.1:51234",
    )


def test_register_and_get_session():
    info = _make_info("exec-1")
    register("exec-1", info)
    result = get_session("exec-1")
    assert result is info
    assert result.cdp_port == 51234
    assert result.registered_at > 0
    unregister("exec-1")


def test_get_session_unknown_id_returns_none():
    assert get_session("does-not-exist") is None


def test_unregister_removes_session():
    register("exec-2", _make_info("exec-2"))
    unregister("exec-2")
    assert get_session("exec-2") is None


def test_update_step_sets_current_step():
    register("exec-3", _make_info("exec-3"))
    step = StepProgress(step_order=1, step_name="Navigate", action="navigate", started_at=time.monotonic())
    update_step("exec-3", step)
    info = get_session("exec-3")
    assert info.current_step is step
    assert info.current_step.step_order == 1
    unregister("exec-3")


def test_update_step_clears_with_none():
    register("exec-4", _make_info("exec-4"))
    update_step("exec-4", StepProgress(step_order=1, step_name="Step", action="click", started_at=time.monotonic()))
    update_step("exec-4", None)
    assert get_session("exec-4").current_step is None
    unregister("exec-4")


def test_update_step_noop_for_unknown_id():
    update_step("does-not-exist", StepProgress(step_order=1, step_name="S", action="click", started_at=time.monotonic()))


def test_register_sets_registered_at():
    before = time.monotonic()
    info = _make_info("exec-5")
    register("exec-5", info)
    after = time.monotonic()
    assert before <= info.registered_at <= after
    unregister("exec-5")


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
