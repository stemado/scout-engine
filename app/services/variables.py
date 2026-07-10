"""Variable resolution for workflow execution.

Resolution order: overrides > environment variables > defaults.
Adapted from Scout's executor.py but designed for service context (raises
exceptions instead of sys.exit).
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

from app.schemas import Workflow, WorkflowStep

if TYPE_CHECKING:
    from app.services.executor import RuntimeContext


_logger = logging.getLogger(__name__)
_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")
_VAULT_PREFIX = "vault:"


class UnresolvedVariableError(Exception):
    """Raised when a workflow variable cannot be resolved."""

    def __init__(self, variables: list[str]):
        self.variables = variables
        super().__init__(f"Unresolved variables: {', '.join(variables)}")


def _resolve_vault_reference(value: str) -> str:
    """Resolve a ``vault:NAME`` reference via secret-vault."""
    name = value[len(_VAULT_PREFIX):]

    try:
        from secret_vault import CredentialResolver  # type: ignore[import-not-found]
    except ImportError as exc:
        raise UnresolvedVariableError([name]) from exc

    try:
        return CredentialResolver(interactive=False).get(name)
    except KeyError as exc:
        raise UnresolvedVariableError([name]) from exc


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

    # Resolve vault references (overrides may carry "vault:NAME" placeholders
    # so that caller systems like the Census API never see cleartext secrets).
    for var_name, value in list(resolved_values.items()):
        if isinstance(value, str) and value.startswith(_VAULT_PREFIX):
            try:
                resolved_values[var_name] = _resolve_vault_reference(value)
            except UnresolvedVariableError:
                _logger.warning(
                    "Vault reference for '%s' (%s) could not be resolved",
                    var_name, value,
                )
                raise

    resolved_steps = [_resolve_step(step, resolved_values) for step in workflow.steps]
    resolved_cleanup = [_resolve_step(step, resolved_values) for step in workflow.cleanup_steps]
    return workflow.model_copy(update={"steps": resolved_steps, "cleanup_steps": resolved_cleanup})


def _resolve_step(step: WorkflowStep, values: dict[str, str]) -> WorkflowStep:
    """Resolve ``${VAR}`` references in a step's string fields."""
    updates: dict[str, str] = {}

    for field_name in (
        "selector", "value", "url_pattern",
        "auth", "source", "destination", "content", "body",
        "compare_to", "pattern",
    ):
        field_val = getattr(step, field_name, None)
        if field_val and isinstance(field_val, str) and "${" in field_val:
            resolved = _VAR_PATTERN.sub(
                lambda m: values.get(m.group(1), m.group(0)),
                field_val,
            )
            updates[field_name] = resolved

    if updates:
        return step.model_copy(update=updates)
    return step


# Fields eligible for runtime interpolation (step_outputs from extract/http_request)
_RUNTIME_INTERPOLATABLE = (
    "selector", "value", "url_pattern", "source", "destination",
    "content", "body", "auth", "pattern", "compare_to",
)


def resolve_step_runtime(step: WorkflowStep, context: RuntimeContext) -> WorkflowStep:
    """Interpolate ``${...}`` tokens using runtime context (step_outputs).

    Called before each step executes. Only resolves tokens that match keys
    in ``context.step_outputs`` — tokens from the initial variable pass
    are already baked in by ``resolve_variables()``.

    Returns a new step via ``model_copy`` if changes were made.
    """
    if not context.step_outputs:
        return step

    updates: dict[str, str] = {}

    for field_name in _RUNTIME_INTERPOLATABLE:
        field_val = getattr(step, field_name, None)
        if field_val and isinstance(field_val, str) and "${" in field_val:
            resolved = _VAR_PATTERN.sub(
                lambda m: (
                    str(context.step_outputs[m.group(1)])
                    if m.group(1) in context.step_outputs
                    else m.group(0)
                ),
                field_val,
            )
            if resolved != field_val:
                updates[field_name] = resolved

    if updates:
        return step.model_copy(update=updates)
    return step
