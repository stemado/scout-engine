"""Artifact API -- retrieve screenshots and downloads from executions."""

import asyncio
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
                    "url": f"/api/executions/{execution_id}/artifacts/{artifact_type}/{f}",
                })

    return artifacts


@router.get("/api/executions/{execution_id}/artifacts")
async def list_artifacts(execution_id: UUID):
    """List all artifacts (screenshots + downloads) for an execution."""
    artifacts = await asyncio.to_thread(_collect_artifacts, str(execution_id))
    return {"execution_id": str(execution_id), "artifacts": artifacts}


@router.get("/api/executions/{execution_id}/artifacts/{artifact_type}/{filename:path}")
async def download_artifact(execution_id: UUID, artifact_type: str, filename: str):
    """Download a single artifact file."""
    if artifact_type not in ("screenshot", "download"):
        raise HTTPException(status_code=400, detail="Invalid artifact type. Must be 'screenshot' or 'download'.")

    base_dir = settings.screenshot_dir if artifact_type == "screenshot" else settings.download_dir
    filepath = os.path.join(base_dir, str(execution_id), filename)

    # Path traversal guard: resolved path must stay within the execution's directory
    real_filepath = os.path.realpath(filepath)
    allowed_prefix = os.path.realpath(os.path.join(base_dir, str(execution_id)))
    if not real_filepath.startswith(allowed_prefix):
        raise HTTPException(status_code=400, detail="Invalid filename.")

    if not await asyncio.to_thread(os.path.isfile, filepath):
        raise HTTPException(status_code=404, detail="Artifact not found.")

    return FileResponse(filepath)
