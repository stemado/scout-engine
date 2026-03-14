"""Execution API -- run workflows and track results."""

import os
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db, get_session_factory
from app.models import Execution, ExecutionStep, WorkflowRecord
from app.schemas import Workflow
from app.services.cancellation import cancel, register, unregister
from app.services.executor import ExecutionResult, execute_workflow
from app.services.variables import UnresolvedVariableError, resolve_variables

router = APIRouter(tags=["executions"])


class RunWorkflowRequest(BaseModel):
    """Optional request body for POST /api/workflows/{id}/run."""
    variables: dict[str, str] = {}
    callback_url: str | None = None


async def _run_execution(
    execution_id: UUID,
    workflow: Workflow,
    session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, str] | None = None,
):
    """Background task that runs a workflow and updates the database.

    Uses the provided session_factory (not get_db) because this task outlives
    the originating HTTP request and needs its own independent session.

    Uses session-per-unit-of-work pattern: never hold a DB connection during
    the long browser execution.
    """
    from app.config import settings

    # --- Session 1: Mark as running, then close immediately ---
    async with session_factory() as db:
        result = await db.execute(select(Execution).where(Execution.id == execution_id))
        execution = result.scalar_one()
        execution.status = "running"
        execution.started_at = datetime.now(timezone.utc)
        await db.commit()
    # Session 1 closed -- no connection held during browser execution

    # --- Browser execution -- no DB connection held ---
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
            screenshot_dir=settings.screenshot_dir,
        )
    except UnresolvedVariableError as e:
        exec_result = ExecutionResult(status="failed", error=str(e))
    except Exception as e:
        exec_result = ExecutionResult(status="failed", error=str(e))
    finally:
        unregister(str(execution_id))
        pause_unregister(str(execution_id))

    # --- Session 2: Write results, then close immediately ---
    async with session_factory() as db:
        result = await db.execute(select(Execution).where(Execution.id == execution_id))
        execution = result.scalar_one()

        # Respect external cancellation: stop_execution may have already
        # written "cancelled" to the DB while we were running — preserve that
        # status but always write the factual telemetry (counts, timestamps).
        if execution.status != "cancelled":
            execution.status = exec_result.status
            if exec_result.error:
                execution.error_message = exec_result.error

        # Always write step counts and finish time regardless of cancellation —
        # a cancelled execution that completed 3 of 5 steps should reflect that.
        execution.passed_steps = exec_result.passed
        execution.failed_steps = exec_result.failed
        execution.finished_at = datetime.now(timezone.utc)

        for step_result in exec_result.steps:
            step_record = ExecutionStep(
                execution_id=execution_id,
                step_order=step_result.step_order,
                step_name=step_result.step_name,
                action=step_result.action,
                status=step_result.status,
                elapsed_ms=step_result.elapsed_ms,
                error_message=step_result.error,
                screenshot_path=step_result.screenshot_path,
            )
            db.add(step_record)

        await db.commit()


@router.post("/api/workflows/{workflow_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_workflow(
    workflow_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    """Submit a workflow for execution.

    Accepts an optional JSON body with:
    - callback_url: URL to POST execution results to when complete
    - variables: variable overrides for the workflow
    """
    # Parse optional body (backward compatible — no body is valid)
    body = RunWorkflowRequest()
    try:
        raw = await request.json()
        if raw:
            body = RunWorkflowRequest.model_validate(raw)
    except Exception:
        pass  # No body or invalid JSON — use defaults

    # Load workflow
    result = await db.execute(select(WorkflowRecord).where(WorkflowRecord.id == workflow_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Workflow not found")

    workflow = Workflow.model_validate(record.workflow_json)

    # Create execution record
    execution = Execution(
        workflow_id=workflow_id,
        status="pending",
        total_steps=len(workflow.steps),
        created_by_key_id=getattr(request.state, "api_key_id", None),
        callback_url=body.callback_url,
    )
    db.add(execution)
    await db.commit()
    await db.refresh(execution)

    # Launch background execution with the session factory
    overrides = body.variables if body.variables else None
    background_tasks.add_task(
        _run_execution, execution.id, workflow, session_factory, overrides
    )

    return {"execution_id": str(execution.id), "status": "pending"}


@router.get("/api/executions")
async def list_executions(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """List recent executions."""
    result = await db.execute(
        select(Execution).order_by(Execution.created_at.desc()).limit(limit)
    )
    executions = result.scalars().all()
    return [
        {
            "id": str(ex.id),
            "workflow_id": str(ex.workflow_id),
            "status": ex.status,
            "total_steps": ex.total_steps,
            "passed_steps": ex.passed_steps,
            "failed_steps": ex.failed_steps,
            "started_at": ex.started_at.isoformat() if ex.started_at else None,
            "finished_at": ex.finished_at.isoformat() if ex.finished_at else None,
            "created_at": ex.created_at.isoformat(),
        }
        for ex in executions
    ]


@router.get("/api/executions/{execution_id}")
async def get_execution(execution_id: UUID, db: AsyncSession = Depends(get_db)):
    """Get execution detail with step results."""
    result = await db.execute(select(Execution).where(Execution.id == execution_id))
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    # Load steps
    steps_result = await db.execute(
        select(ExecutionStep)
        .where(ExecutionStep.execution_id == execution_id)
        .order_by(ExecutionStep.step_order)
    )
    steps = steps_result.scalars().all()

    return {
        "id": str(execution.id),
        "workflow_id": str(execution.workflow_id),
        "status": execution.status,
        "total_steps": execution.total_steps,
        "passed_steps": execution.passed_steps,
        "failed_steps": execution.failed_steps,
        "error_message": execution.error_message,
        "callback_url": execution.callback_url,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
        "steps": [
            {
                "step_order": s.step_order,
                "step_name": s.step_name,
                "action": s.action,
                "status": s.status,
                "elapsed_ms": s.elapsed_ms,
                "error_message": s.error_message,
                "screenshot_url": (
                    f"/api/executions/{execution_id}/artifacts/screenshot/{os.path.basename(s.screenshot_path)}"
                    if s.screenshot_path else None
                ),
            }
            for s in steps
        ],
    }


@router.post("/api/executions/{execution_id}/stop")
async def stop_execution(execution_id: UUID, db: AsyncSession = Depends(get_db)):
    """Cancel a running execution."""
    result = await db.execute(select(Execution).where(Execution.id == execution_id))
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if execution.status not in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot stop execution in state: {execution.status}",
        )

    # Signal the executor thread to stop after the current step
    cancel(str(execution_id))

    # Update DB immediately for UI feedback (executor will also write "cancelled"
    # when it finishes the current step and sees the cancel event)
    execution.status = "cancelled"
    execution.finished_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "cancelled"}


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


@router.get("/api/executions/{execution_id}/browser")
async def get_browser_session(execution_id: UUID):
    """Get CDP connection info for a running execution's browser."""
    from app.services.browser_session import get_session

    info = get_session(str(execution_id))
    if info is None:
        raise HTTPException(
            status_code=404,
            detail="No active browser session for this execution. "
            "The browser may not have started yet, or the execution may have already finished.",
        )
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
