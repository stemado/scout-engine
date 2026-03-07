"""Tests for database models."""

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import ApiKey, Base, Execution, ExecutionStep, InviteToken, Schedule, WorkflowRecord


@pytest.fixture
def db():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_create_workflow_record(db):
    """Should store a workflow with JSON payload."""
    wf = WorkflowRecord(
        name="test-workflow",
        description="A test",
        schema_version="1.0",
        workflow_json={"name": "test", "steps": []},
    )
    db.add(wf)
    db.commit()
    db.refresh(wf)

    assert wf.id is not None
    assert wf.name == "test-workflow"
    assert wf.workflow_json["name"] == "test"


def test_create_execution(db):
    """Should track an execution linked to a workflow."""
    wf = WorkflowRecord(name="test", schema_version="1.0", workflow_json={})
    db.add(wf)
    db.commit()

    ex = Execution(workflow_id=wf.id, status="pending", total_steps=3)
    db.add(ex)
    db.commit()
    db.refresh(ex)

    assert ex.id is not None
    assert ex.workflow_id == wf.id
    assert ex.status == "pending"


def test_create_execution_step(db):
    """Should track individual step results."""
    wf = WorkflowRecord(name="test", schema_version="1.0", workflow_json={})
    db.add(wf)
    db.commit()

    ex = Execution(workflow_id=wf.id, status="running", total_steps=1)
    db.add(ex)
    db.commit()

    step = ExecutionStep(
        execution_id=ex.id,
        step_order=1,
        step_name="Navigate",
        action="navigate",
        status="passed",
        elapsed_ms=1234,
    )
    db.add(step)
    db.commit()
    db.refresh(step)

    assert step.execution_id == ex.id
    assert step.elapsed_ms == 1234


def test_create_schedule(db):
    """Should store a cron schedule linked to a workflow."""
    wf = WorkflowRecord(name="test", schema_version="1.0", workflow_json={})
    db.add(wf)
    db.commit()

    sched = Schedule(
        workflow_id=wf.id,
        name="Daily 6am",
        cron_expression="0 6 * * *",
        timezone="US/Eastern",
        enabled=True,
    )
    db.add(sched)
    db.commit()
    db.refresh(sched)

    assert sched.id is not None
    assert sched.cron_expression == "0 6 * * *"
    assert sched.enabled is True


def test_api_key_model_exists():
    """ApiKey model should be importable and have expected columns."""
    assert ApiKey.__tablename__ == "api_keys"
    cols = {c.name for c in ApiKey.__table__.columns}
    assert "key_hash" in cols
    assert "key_prefix" in cols
    assert "label" in cols
    assert "is_admin" in cols
    assert "revoked" in cols


def test_invite_token_model_exists():
    """InviteToken model should be importable and have expected columns."""
    assert InviteToken.__tablename__ == "invite_tokens"
    cols = {c.name for c in InviteToken.__table__.columns}
    assert "token_hash" in cols
    assert "label" in cols
    assert "expires_at" in cols
    assert "used_at" in cols
