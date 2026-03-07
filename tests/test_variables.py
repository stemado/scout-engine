"""Tests for variable resolution."""

import os
from unittest.mock import patch

import pytest

from app.schemas import Workflow, WorkflowStep, WorkflowVariable
from app.services.variables import UnresolvedVariableError, resolve_variables


def _make_workflow(**kwargs) -> Workflow:
    defaults = {"name": "test", "variables": {}, "steps": []}
    defaults.update(kwargs)
    return Workflow(**defaults)


def test_resolve_from_overrides():
    wf = _make_workflow(
        variables={"USERNAME": WorkflowVariable(type="credential", default="placeholder")},
        steps=[WorkflowStep(order=1, name="Type", action="type", selector="#user", value="${USERNAME}")],
    )
    resolved = resolve_variables(wf, overrides={"USERNAME": "admin"})
    assert resolved.steps[0].value == "admin"


def test_resolve_from_env_var():
    wf = _make_workflow(
        variables={"API_KEY": WorkflowVariable(type="string", default="placeholder")},
        steps=[WorkflowStep(order=1, name="Type", action="type", selector="#key", value="${API_KEY}")],
    )
    with patch.dict(os.environ, {"API_KEY": "env-value-123"}):
        resolved = resolve_variables(wf, overrides={})
    assert resolved.steps[0].value == "env-value-123"


def test_resolve_from_default():
    wf = _make_workflow(
        variables={"REPORT": WorkflowVariable(type="string", default="monthly")},
        steps=[WorkflowStep(order=1, name="Nav", action="navigate", value="https://example.com/${REPORT}")],
    )
    resolved = resolve_variables(wf, overrides={})
    assert resolved.steps[0].value == "https://example.com/monthly"


def test_override_beats_env():
    wf = _make_workflow(
        variables={"USERNAME": WorkflowVariable(type="credential", default="placeholder")},
        steps=[WorkflowStep(order=1, name="Type", action="type", selector="#user", value="${USERNAME}")],
    )
    with patch.dict(os.environ, {"USERNAME": "env-user"}):
        resolved = resolve_variables(wf, overrides={"USERNAME": "override-user"})
    assert resolved.steps[0].value == "override-user"


def test_unresolved_variable_raises():
    wf = _make_workflow(
        variables={"MISSING": WorkflowVariable(type="string")},
        steps=[WorkflowStep(order=1, name="Nav", action="navigate", value="${MISSING}")],
    )
    with pytest.raises(UnresolvedVariableError) as exc_info:
        resolve_variables(wf, overrides={})
    assert "MISSING" in str(exc_info.value)


def test_multiple_vars_in_one_field():
    wf = _make_workflow(
        variables={
            "HOST": WorkflowVariable(type="url", default="example.com"),
            "REPORT_PATH": WorkflowVariable(type="string", default="/reports"),
        },
        steps=[WorkflowStep(order=1, name="Nav", action="navigate", value="https://${HOST}${REPORT_PATH}")],
    )
    resolved = resolve_variables(wf, overrides={})
    assert resolved.steps[0].value == "https://example.com/reports"


def test_var_in_selector():
    wf = _make_workflow(
        variables={"REPORT_NAME": WorkflowVariable(type="string", default="Unum_Report")},
        steps=[WorkflowStep(order=1, name="Click", action="click", selector="[aria-label='${REPORT_NAME}']")],
    )
    resolved = resolve_variables(wf, overrides={})
    assert resolved.steps[0].selector == "[aria-label='Unum_Report']"


def test_no_vars_passthrough():
    wf = _make_workflow(
        steps=[WorkflowStep(order=1, name="Click", action="click", selector="#btn")],
    )
    resolved = resolve_variables(wf, overrides={})
    assert resolved.steps[0].selector == "#btn"


def test_cleanup_steps_resolved():
    """Cleanup steps must also have their ${VAR} placeholders resolved."""
    wf = _make_workflow(
        variables={"BASE_URL": WorkflowVariable(type="url", default="https://example.com")},
        steps=[WorkflowStep(order=1, name="Nav", action="navigate", value="${BASE_URL}/app")],
        cleanup_steps=[WorkflowStep(order=1, name="Logout", action="navigate", value="${BASE_URL}/logout")],
    )
    resolved = resolve_variables(wf, overrides={})
    assert resolved.steps[0].value == "https://example.com/app"
    assert resolved.cleanup_steps[0].value == "https://example.com/logout"
