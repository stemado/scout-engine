"""Schedule API -- create and manage cron schedules for workflows."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Schedule, WorkflowRecord
from app.services.scheduler import (
    add_or_replace_schedule,
    compute_next_run,
    parse_cron_expression,
    remove_schedule_job,
)

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


class CreateScheduleRequest(BaseModel):
    workflow_id: UUID
    name: str
    cron_expression: str
    timezone: str = "UTC"
    enabled: bool = True
    variables: dict[str, str] | None = None


class UpdateScheduleRequest(BaseModel):
    name: str | None = None
    cron_expression: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    variables: dict[str, str] | None = None


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_schedule(req: CreateScheduleRequest, db: AsyncSession = Depends(get_db)):
    """Create a cron schedule for a workflow."""
    # Validate cron expression
    try:
        parse_cron_expression(req.cron_expression)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Verify workflow exists
    result = await db.execute(select(WorkflowRecord).where(WorkflowRecord.id == req.workflow_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Workflow not found")

    next_run = compute_next_run(req.cron_expression, timezone=req.timezone)

    schedule = Schedule(
        workflow_id=req.workflow_id,
        name=req.name,
        cron_expression=req.cron_expression,
        timezone=req.timezone,
        enabled=req.enabled,
        variables=req.variables,
        next_run_at=next_run,
    )
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)

    if schedule.enabled:
        add_or_replace_schedule(str(schedule.id), schedule.cron_expression, schedule.timezone)

    return {
        "id": str(schedule.id),
        "workflow_id": str(schedule.workflow_id),
        "name": schedule.name,
        "cron_expression": schedule.cron_expression,
        "timezone": schedule.timezone,
        "enabled": schedule.enabled,
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "created_at": schedule.created_at.isoformat(),
    }


@router.get("")
async def list_schedules(db: AsyncSession = Depends(get_db)):
    """List all schedules."""
    result = await db.execute(select(Schedule).order_by(Schedule.created_at.desc()))
    schedules = result.scalars().all()
    return [
        {
            "id": str(s.id),
            "workflow_id": str(s.workflow_id),
            "name": s.name,
            "cron_expression": s.cron_expression,
            "timezone": s.timezone,
            "enabled": s.enabled,
            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
        }
        for s in schedules
    ]


@router.put("/{schedule_id}")
async def update_schedule(
    schedule_id: UUID, req: UpdateScheduleRequest, db: AsyncSession = Depends(get_db)
):
    """Update a schedule."""
    result = await db.execute(select(Schedule).where(Schedule.id == schedule_id))
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    if req.cron_expression is not None:
        try:
            parse_cron_expression(req.cron_expression)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        schedule.cron_expression = req.cron_expression
        schedule.next_run_at = compute_next_run(
            req.cron_expression,
            timezone=req.timezone or schedule.timezone,
        )

    if req.name is not None:
        schedule.name = req.name
    if req.timezone is not None:
        schedule.timezone = req.timezone
    if req.enabled is not None:
        schedule.enabled = req.enabled
    if req.variables is not None:
        schedule.variables = req.variables

    await db.commit()
    await db.refresh(schedule)

    if schedule.enabled:
        add_or_replace_schedule(str(schedule.id), schedule.cron_expression, schedule.timezone)
    else:
        remove_schedule_job(str(schedule.id))

    return {
        "id": str(schedule.id),
        "workflow_id": str(schedule.workflow_id),
        "name": schedule.name,
        "cron_expression": schedule.cron_expression,
        "timezone": schedule.timezone,
        "enabled": schedule.enabled,
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
    }


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: UUID, db: AsyncSession = Depends(get_db)):
    """Delete a schedule."""
    result = await db.execute(select(Schedule).where(Schedule.id == schedule_id))
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await db.delete(schedule)
    await db.commit()

    remove_schedule_job(str(schedule_id))
