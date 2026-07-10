"""Fragment resolution -- expands include directives into flat step lists.

This operates on raw dicts (before Pydantic validation) because include steps
lack the ``action`` field that WorkflowStep requires.
"""

from __future__ import annotations

import copy
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WorkflowFragment

_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


async def resolve_fragments(raw_workflow_json: dict, db: AsyncSession) -> dict:
    """Resolve fragment includes in workflow JSON before Pydantic validation.

    Steps with ``{"include": "fragment_id", "variables": {...}}`` are replaced
    with the fragment's steps, with variables substituted into step fields.

    Returns a new dict -- the original is not mutated.
    """
    steps = raw_workflow_json.get("steps", [])

    # Collect all referenced fragment_ids
    fragment_ids = {
        step["include"] for step in steps if "include" in step and "action" not in step
    }

    if not fragment_ids:
        return raw_workflow_json

    # Fetch all referenced fragments in a single query
    result = await db.execute(
        select(WorkflowFragment).where(
            WorkflowFragment.fragment_id.in_(fragment_ids)
        )
    )
    fragments = {f.fragment_id: f for f in result.scalars().all()}

    # Verify all fragments exist
    missing = fragment_ids - set(fragments.keys())
    if missing:
        raise ValueError(f"Fragment not found: {', '.join(sorted(missing))}")

    # Build resolved step list
    resolved_steps: list[dict] = []
    merged_variables: dict = {}

    for step in steps:
        if "include" in step and "action" not in step:
            frag_id = step["include"]
            fragment = fragments[frag_id]

            # Merge fragment variable declarations into workflow variables
            if fragment.variables:
                merged_variables.update(fragment.variables)

            # Get variable overrides from the include directive
            var_overrides = step.get("variables", {})

            # Deep-copy fragment steps and substitute variables
            for frag_step in fragment.steps:
                resolved_step = copy.deepcopy(frag_step)
                if var_overrides:
                    _substitute_variables(resolved_step, var_overrides)
                resolved_steps.append(resolved_step)
        else:
            resolved_steps.append(copy.deepcopy(step))

    # Re-number all steps sequentially
    for i, step in enumerate(resolved_steps, start=1):
        step["order"] = i

    # Build result dict
    result_json = copy.deepcopy(raw_workflow_json)
    result_json["steps"] = resolved_steps

    # Merge fragment variable declarations into workflow variables
    if merged_variables:
        workflow_vars = result_json.get("variables", {})
        # Fragment defaults don't override existing workflow declarations
        for var_name, var_def in merged_variables.items():
            if var_name not in workflow_vars:
                workflow_vars[var_name] = var_def
        result_json["variables"] = workflow_vars

    return result_json


def _substitute_variables(step: dict, variables: dict[str, str]) -> None:
    """Substitute ``${VAR}`` tokens in a step's string values in-place."""
    for key, value in step.items():
        if isinstance(value, str) and "${" in value:
            step[key] = _VAR_PATTERN.sub(
                lambda m: variables.get(m.group(1), m.group(0)),
                value,
            )
