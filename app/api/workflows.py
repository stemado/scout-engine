"""Workflow CRUD API."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import WorkflowRecord
from app.schemas import Workflow

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_workflow(workflow: Workflow, db: AsyncSession = Depends(get_db)):
    """Upload a new workflow."""
    record = WorkflowRecord(
        name=workflow.name,
        description=workflow.description,
        schema_version=workflow.schema_version,
        workflow_json=workflow.model_dump(),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return {
        "id": str(record.id),
        "name": record.name,
        "description": record.description,
        "created_at": record.created_at.isoformat(),
    }


@router.get("")
async def list_workflows(db: AsyncSession = Depends(get_db)):
    """List all workflows."""
    result = await db.execute(select(WorkflowRecord).order_by(WorkflowRecord.created_at.desc()))
    workflows = result.scalars().all()
    return [
        {
            "id": str(wf.id),
            "name": wf.name,
            "description": wf.description,
            "schema_version": wf.schema_version,
            "created_at": wf.created_at.isoformat(),
        }
        for wf in workflows
    ]


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: UUID, db: AsyncSession = Depends(get_db)):
    """Get a workflow by ID."""
    result = await db.execute(select(WorkflowRecord).where(WorkflowRecord.id == workflow_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {
        "id": str(record.id),
        "name": record.name,
        "description": record.description,
        "schema_version": record.schema_version,
        "workflow_json": record.workflow_json,
        "created_at": record.created_at.isoformat(),
    }


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(workflow_id: UUID, db: AsyncSession = Depends(get_db)):
    """Delete a workflow."""
    result = await db.execute(select(WorkflowRecord).where(WorkflowRecord.id == workflow_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Workflow not found")
    await db.delete(record)
    await db.commit()
