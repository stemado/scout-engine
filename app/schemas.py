"""Workflow JSON schema models -- vendored from Scout's workflow.py.

These are the canonical Pydantic models for Scout's workflow format (schema v1.0).
Vendored here to avoid importing the full Scout package as a dependency.

Source: https://github.com/stemado/scout/blob/master/src/scout/workflow.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkflowVariable(BaseModel):
    """A declared variable for parameterizing workflow steps."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["credential", "string", "url"] = "string"
    default: str = ""
    description: str = ""


class WorkflowSettings(BaseModel):
    """Global execution settings for a workflow."""

    model_config = ConfigDict(extra="forbid")

    headless: bool = False
    human_mode: bool = True
    default_timeout_ms: int = Field(default=30000, ge=0)
    step_delay_ms: int = Field(default=500, ge=0)
    on_error: Literal["stop", "continue", "retry"] = "stop"
    pause_on_error: bool = False


class WorkflowStep(BaseModel):
    """A single step in a workflow sequence."""

    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1)
    name: str
    action: Literal[
        "navigate", "click", "type", "select", "scroll", "wait",
        "wait_for_download", "wait_for_response",
        "press_key", "hover", "clear", "run_js", "handoff",
    ]

    # Common optional fields
    selector: str | None = None
    value: str | None = None
    frame_context: str | None = None
    on_error: Literal["stop", "continue", "retry"] | None = None
    timeout_ms: int | None = Field(default=None, ge=0)

    # Action-specific optional fields
    clear_first: bool | None = None            # type
    filename_pattern: str | None = None        # wait_for_download
    download_dir: str | None = None            # wait_for_download
    url_pattern: str | None = None             # wait_for_response
    method: str | None = None                  # wait_for_response


class WorkflowSource(BaseModel):
    """Provenance of a workflow -- which tool generated it."""

    model_config = ConfigDict(extra="forbid")

    tool: str = "scout"
    session_id: str = ""


class Workflow(BaseModel):
    """A portable, replayable browser automation workflow."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    name: str
    description: str = ""
    created: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: WorkflowSource = Field(default_factory=WorkflowSource)
    variables: dict[str, WorkflowVariable] = Field(default_factory=dict)
    settings: WorkflowSettings = Field(default_factory=WorkflowSettings)
    steps: list[WorkflowStep] = Field(default_factory=list)
    cleanup_steps: list[WorkflowStep] = Field(default_factory=list)


class UploadWorkflowRequest(BaseModel):
    """Request envelope for uploading a workflow."""

    workflow: Workflow
