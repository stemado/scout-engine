"""Variable resolution for workflow execution.

Resolution order: overrides > environment variables > defaults.
Adapted from Scout's executor.py but designed for service context (raises
exceptions instead of sys.exit).
"""

from __future__ import annotations

import os
import re

from app.schemas import Workflow, WorkflowStep


_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


class UnresolvedVariableError(Exception):
    """Raised when a workflow variable cannot be resolved."""

    def __init__(self, variables: list[str]):
        self.variables = variables
        super().__init__(f"Unresolved variables: {', '.join(variables)}")


def resolve_variables(
    workflow: Workflow, overrides: dict[str, str] | None = None
) -> Workflow:
    """Resolve all ``${VAR}`` references in a workflow's steps.

    Resolution order: overrides > environment variables > defaults.
    Returns a **new** Workflow with all references resolved -- the original
    workflow is never mutated.

    Raises:
        UnresolvedVariableError: If any declared variable cannot be resolved
            through overrides, environment, or defaults.
    """
    overrides = overrides or {}
    resolved_values: dict[str, str] = {}
    unresolved: list[str] = []

    for var_name, var_def in workflow.variables.items():
        if var_name in overrides:
            resolved_values[var_name] = overrides[var_name]
        elif var_name in os.environ:
            resolved_values[var_name] = os.environ[var_name]
        elif var_def.default:
            resolved_values[var_name] = var_def.default
        else:
            unresolved.append(var_name)

    if unresolved:
        raise UnresolvedVariableError(unresolved)

    resolved_steps = [_resolve_step(step, resolved_values) for step in workflow.steps]
    resolved_cleanup = [_resolve_step(step, resolved_values) for step in workflow.cleanup_steps]
    return workflow.model_copy(update={"steps": resolved_steps, "cleanup_steps": resolved_cleanup})


def _resolve_step(step: WorkflowStep, values: dict[str, str]) -> WorkflowStep:
    """Resolve ``${VAR}`` references in a step's string fields."""
    updates: dict[str, str] = {}

    for field_name in ("selector", "value", "url_pattern"):
        field_val = getattr(step, field_name)
        if field_val and "${" in field_val:
            resolved = _VAR_PATTERN.sub(
                lambda m: values.get(m.group(1), m.group(0)),
                field_val,
            )
            updates[field_name] = resolved

    if updates:
        return step.model_copy(update=updates)
    return step
