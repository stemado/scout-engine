"""APScheduler integration -- manages cron-based workflow scheduling.

Schedules are stored as database rows. APScheduler reads from these on startup
and when schedules are modified. This decouples our data from APScheduler's
internal job store serialization.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as tz

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def parse_cron_expression(expression: str) -> dict[str, str]:
    """Parse a cron expression into APScheduler CronTrigger fields.

    Supports:
    - 5-field: minute hour day month day_of_week
    - 6-field: second minute hour day month day_of_week
    """
    parts = expression.strip().split()
    if len(parts) == 5:
        return {
            "minute": parts[0],
            "hour": parts[1],
            "day": parts[2],
            "month": parts[3],
            "day_of_week": parts[4],
        }
    elif len(parts) == 6:
        return {
            "second": parts[0],
            "minute": parts[1],
            "hour": parts[2],
            "day": parts[3],
            "month": parts[4],
            "day_of_week": parts[5],
        }
    else:
        raise ValueError(
            f"Invalid cron expression (expected 5 or 6 fields, got {len(parts)}): {expression}"
        )


def compute_next_run(cron_expression: str, timezone: str = "UTC") -> datetime | None:
    """Compute the next run time for a cron expression."""
    fields = parse_cron_expression(cron_expression)
    trigger = CronTrigger(timezone=timezone, **fields)
    return trigger.get_next_fire_time(None, datetime.now(tz.utc))


def add_or_replace_schedule(schedule_id: str, cron_expression: str, timezone: str) -> None:
    """Register or replace a cron job with APScheduler."""
    fields = parse_cron_expression(cron_expression)
    scheduler.add_job(
        execute_scheduled_workflow,
        trigger=CronTrigger(timezone=timezone, **fields),
        id=schedule_id,
        args=(schedule_id,),
        replace_existing=True,
    )


def remove_schedule_job(schedule_id: str):
    """Remove a job from APScheduler."""
    try:
        scheduler.remove_job(schedule_id)
    except Exception:
        pass  # Job might not exist


def start_scheduler():
    """Start the APScheduler background scheduler."""
    if not scheduler.running:
        scheduler.start()


def shutdown_scheduler():
    """Shutdown the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)


async def execute_scheduled_workflow(schedule_id: str):
    """Called by APScheduler when a cron trigger fires.

    Loads the schedule and its workflow from the database, creates an execution
    record, updates schedule timing, then delegates to the same _run_execution
    function used by the REST API.
    """
    from app.database import async_session
    from app.models import Execution, Schedule, WorkflowRecord
    from app.schemas import Workflow
    from sqlalchemy import select

    async with async_session() as db:
        # Load schedule
        result = await db.execute(select(Schedule).where(Schedule.id == schedule_id))
        schedule = result.scalar_one_or_none()
        if not schedule or not schedule.enabled:
            logger.info("Schedule %s skipped (not found or disabled)", schedule_id)
            return

        # Load workflow
        result = await db.execute(
            select(WorkflowRecord).where(WorkflowRecord.id == schedule.workflow_id)
        )
        record = result.scalar_one_or_none()
        if not record:
            logger.warning(
                "Schedule %s references missing workflow %s",
                schedule_id,
                schedule.workflow_id,
            )
            return

        workflow = Workflow.model_validate(record.workflow_json)

        # Create execution record
        execution = Execution(
            workflow_id=schedule.workflow_id,
            schedule_id=schedule.id,
            status="pending",
            total_steps=len(workflow.steps),
        )
        db.add(execution)
        await db.commit()
        await db.refresh(execution)

        # Update schedule timing
        schedule.last_run_at = datetime.now(tz.utc)
        schedule.next_run_at = compute_next_run(
            schedule.cron_expression, timezone=schedule.timezone
        )
        await db.commit()

        execution_id = execution.id
        overrides = schedule.variables

    # Run execution using the same logic as the API.
    # Pass async_session as the session_factory so _run_execution can create
    # its own independent database sessions.
    from app.api.executions import _run_execution

    await _run_execution(
        execution_id, workflow, session_factory=async_session, overrides=overrides
    )
