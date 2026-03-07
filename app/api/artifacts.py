"""Artifact API -- retrieve screenshots and downloads from executions."""

import os
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import settings

router = APIRouter(tags=["artifacts"])


def _collect_artifacts(execution_id: str) -> list[dict]:
    """Scan screenshot and download dirs for this execution's files."""
    artifacts = []

    for artifact_type, base_dir in (
        ("screenshot", settings.screenshot_dir),
        ("download", settings.download_dir),
    ):
        exec_dir = os.path.join(base_dir, execution_id)
        if not os.path.isdir(exec_dir):
            continue
        for f in sorted(os.listdir(exec_dir)):
            filepath = os.path.join(exec_dir, f)
            if os.path.isfile(filepath):
                artifacts.append({
                    "filename": f,
                    "type": artifact_type,
                    "size_bytes": os.path.getsize(filepath),
                })

    return artifacts


@router.get("/api/executions/{execution_id}/artifacts")
async def list_artifacts(execution_id: UUID):
    """List all artifacts (screenshots + downloads) for an execution."""
    artifacts = _collect_artifacts(str(execution_id))
    return {"execution_id": str(execution_id), "artifacts": artifacts}
