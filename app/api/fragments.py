"""Fragment CRUD API -- reusable step sequences for workflows."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import WorkflowFragment

router = APIRouter(prefix="/api/fragments", tags=["fragments"])


class CreateFragmentRequest(BaseModel):
    """Request body for creating/updating a fragment."""

    fragment_id: str
    name: str
    description: str | None = None
    variables: dict | None = None
    steps: list


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_or_update_fragment(
    body: CreateFragmentRequest, db: AsyncSession = Depends(get_db)
):
    """Create a new fragment or update an existing one (upsert by fragment_id)."""
    result = await db.execute(
        select(WorkflowFragment).where(
            WorkflowFragment.fragment_id == body.fragment_id
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.name = body.name
        existing.description = body.description
        existing.variables = body.variables
        existing.steps = body.steps
        existing.version = existing.version + 1
        await db.commit()
        await db.refresh(existing)
        fragment = existing
    else:
        fragment = WorkflowFragment(
            fragment_id=body.fragment_id,
            name=body.name,
            description=body.description,
            variables=body.variables,
            steps=body.steps,
        )
        db.add(fragment)
        await db.commit()
        await db.refresh(fragment)

    return {
        "id": str(fragment.id),
        "fragment_id": fragment.fragment_id,
        "name": fragment.name,
        "description": fragment.description,
        "variables": fragment.variables,
        "steps": fragment.steps,
        "version": fragment.version,
        "created_at": fragment.created_at.isoformat(),
        "updated_at": fragment.updated_at.isoformat(),
    }


@router.get("")
async def list_fragments(db: AsyncSession = Depends(get_db)):
    """List all fragments."""
    result = await db.execute(
        select(WorkflowFragment).order_by(WorkflowFragment.created_at.desc())
    )
    fragments = result.scalars().all()
    return [
        {
            "id": str(f.id),
            "fragment_id": f.fragment_id,
            "name": f.name,
            "description": f.description,
            "version": f.version,
            "step_count": len(f.steps) if f.steps else 0,
        }
        for f in fragments
    ]


@router.get("/{fragment_id}")
async def get_fragment(fragment_id: str, db: AsyncSession = Depends(get_db)):
    """Get a fragment by its fragment_id."""
    result = await db.execute(
        select(WorkflowFragment).where(
            WorkflowFragment.fragment_id == fragment_id
        )
    )
    fragment = result.scalar_one_or_none()
    if not fragment:
        raise HTTPException(status_code=404, detail="Fragment not found")
    return {
        "id": str(fragment.id),
        "fragment_id": fragment.fragment_id,
        "name": fragment.name,
        "description": fragment.description,
        "variables": fragment.variables,
        "steps": fragment.steps,
        "version": fragment.version,
        "created_at": fragment.created_at.isoformat(),
        "updated_at": fragment.updated_at.isoformat(),
    }


@router.delete("/{fragment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fragment(fragment_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a fragment by its fragment_id."""
    result = await db.execute(
        select(WorkflowFragment).where(
            WorkflowFragment.fragment_id == fragment_id
        )
    )
    fragment = result.scalar_one_or_none()
    if not fragment:
        raise HTTPException(status_code=404, detail="Fragment not found")
    await db.delete(fragment)
    await db.commit()
