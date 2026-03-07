"""Tests for the vendored workflow schema."""

import pytest
from pydantic import ValidationError

from app.schemas import Workflow, WorkflowSettings, WorkflowStep, WorkflowVariable


def test_minimal_workflow():
    """A workflow with just a name and one step should validate."""
    wf = Workflow(
        name="test",
        steps=[WorkflowStep(order=1, name="Navigate", action="navigate", value="https://example.com")],
    )
    assert wf.name == "test"
    assert wf.schema_version == "1.0"
    assert len(wf.steps) == 1


def test_full_workflow_from_json():
    """A complete workflow JSON should round-trip through the model."""
    raw = {
        "schema_version": "1.0",
        "name": "adp-report",
        "description": "Download ADP report",
        "variables": {
            "USERNAME": {"type": "credential", "default": "admin", "description": "Login username"},
            "PASSWORD": {"type": "credential", "default": "your_password"},
        },
        "settings": {
            "headless": True,
            "default_timeout_ms": 15000,
            "step_delay_ms": 1000,
            "on_error": "continue",
        },
        "steps": [
            {"order": 1, "name": "Navigate", "action": "navigate", "value": "https://example.com"},
            {"order": 2, "name": "Type user", "action": "type", "selector": "#user", "value": "${USERNAME}", "clear_first": True},
            {"order": 3, "name": "Click login", "action": "click", "selector": "#login-btn"},
            {"order": 4, "name": "Wait for download", "action": "wait_for_download", "timeout_ms": 60000, "filename_pattern": "*.csv"},
        ],
    }
    wf = Workflow.model_validate(raw)
    assert wf.name == "adp-report"
    assert wf.variables["USERNAME"].type == "credential"
    assert wf.settings.headless is True
    assert wf.steps[1].clear_first is True
    assert wf.steps[3].filename_pattern == "*.csv"

    # Round-trip to JSON and back
    json_str = wf.model_dump_json()
    wf2 = Workflow.model_validate_json(json_str)
    assert wf2.name == wf.name
    assert len(wf2.steps) == len(wf.steps)


def test_extra_fields_rejected():
    """Schema should reject unknown fields (extra='forbid')."""
    with pytest.raises(ValidationError):
        WorkflowStep(order=1, name="X", action="click", bogus_field="bad")


def test_workflow_variable_types():
    """Variable types should be constrained to credential/string/url."""
    v1 = WorkflowVariable(type="credential")
    assert v1.type == "credential"
    v2 = WorkflowVariable(type="url", default="https://example.com")
    assert v2.type == "url"


def test_settings_defaults():
    """Settings should have sensible defaults."""
    s = WorkflowSettings()
    assert s.headless is False
    assert s.human_mode is True
    assert s.default_timeout_ms == 30000
    assert s.step_delay_ms == 500
    assert s.on_error == "stop"


def test_workflow_settings_human_mode_defaults_true():
    """human_mode should default to True -- anti-detection by default."""
    settings = WorkflowSettings()
    assert settings.human_mode is True


def test_workflow_settings_human_mode_explicit_false():
    """Allow opting out of human mode for speed in trusted environments."""
    settings = WorkflowSettings(human_mode=False)
    assert settings.human_mode is False


def test_workflow_with_human_mode_in_settings():
    """Full workflow with human_mode in settings round-trips correctly."""
    wf_json = {
        "schema_version": "1.0",
        "name": "test",
        "steps": [
            {"order": 1, "name": "nav", "action": "navigate", "value": "https://example.com"}
        ],
        "settings": {"human_mode": False, "step_delay_ms": 200},
    }
    wf = Workflow.model_validate(wf_json)
    assert wf.settings.human_mode is False
    assert wf.settings.step_delay_ms == 200


def test_run_js_action_accepted():
    """run_js should be a valid action type."""
    step = WorkflowStep(order=1, name="Execute JS", action="run_js", value="return true")
    assert step.action == "run_js"


def test_workflow_with_cleanup_steps():
    """Workflow should accept cleanup_steps."""
    wf = Workflow.model_validate({
        "schema_version": "1.0",
        "name": "test",
        "steps": [
            {"order": 1, "name": "nav", "action": "navigate", "value": "https://example.com"}
        ],
        "cleanup_steps": [
            {"order": 1, "name": "logout", "action": "navigate", "value": "https://example.com/logout"}
        ],
    })
    assert len(wf.cleanup_steps) == 1
    assert wf.cleanup_steps[0].action == "navigate"


def test_workflow_without_cleanup_steps_defaults_empty():
    """Workflow without cleanup_steps should default to empty list."""
    wf = Workflow.model_validate({
        "schema_version": "1.0",
        "name": "test",
        "steps": [
            {"order": 1, "name": "nav", "action": "navigate", "value": "https://example.com"}
        ],
    })
    assert wf.cleanup_steps == []


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
