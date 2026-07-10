"""Tests for workflow fragment storage and load-time resolution."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.main import app
from app.models import Base, WorkflowFragment
from app.services.fragments import resolve_fragments


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def client():
    """Async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def db_session():
    """Yield a raw async session for service-level tests.

    Re-uses the in-memory SQLite that conftest.test_db already created via
    the autouse fixture.  We build a fresh engine+tables here because
    service tests need direct session access (not via HTTP).
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        isolation_level="AUTOCOMMIT",
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

BC_LOGIN_FRAGMENT = {
    "fragment_id": "bc_login",
    "name": "bC Login",
    "description": "Login to BenefitsConnect portal",
    "variables": {
        "BC_USERNAME": {"type": "credential", "default": "", "description": "bC username"},
        "BC_PASSWORD": {"type": "credential", "default": "", "description": "bC password"},
    },
    "steps": [
        {"order": 1, "name": "Navigate to bC", "action": "navigate", "value": "https://bc.example.com"},
        {"order": 2, "name": "Enter username", "action": "type", "selector": "#username", "value": "${BC_USERNAME}"},
        {"order": 3, "name": "Enter password", "action": "type", "selector": "#password", "value": "${BC_PASSWORD}"},
        {"order": 4, "name": "Click login", "action": "click", "selector": "#login-btn"},
    ],
}

DOWNLOAD_FRAGMENT = {
    "fragment_id": "bc_download",
    "name": "bC Download Report",
    "description": "Download a report from bC",
    "variables": {
        "REPORT_NAME": {"type": "string", "default": "census", "description": "Report to download"},
    },
    "steps": [
        {"order": 1, "name": "Click reports", "action": "click", "selector": "#reports"},
        {"order": 2, "name": "Select report", "action": "click", "selector": "[data-report='${REPORT_NAME}']"},
        {"order": 3, "name": "Download", "action": "click", "selector": "#download-btn"},
    ],
}


# ---------------------------------------------------------------------------
# Fragment resolution service tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_no_includes(db_session):
    """Workflow without includes passes through unchanged."""
    workflow_json = {
        "name": "plain-workflow",
        "steps": [
            {"order": 1, "name": "Step 1", "action": "navigate", "value": "https://example.com"},
            {"order": 2, "name": "Step 2", "action": "click", "selector": "#btn"},
        ],
    }
    result = await resolve_fragments(workflow_json, db_session)
    assert len(result["steps"]) == 2
    assert result["steps"][0]["name"] == "Step 1"
    assert result["steps"][1]["name"] == "Step 2"


@pytest.mark.asyncio
async def test_resolve_single_include(db_session):
    """Include is replaced with fragment steps, order re-numbered."""
    # Seed fragment
    fragment = WorkflowFragment(
        fragment_id="bc_login",
        name="bC Login",
        steps=BC_LOGIN_FRAGMENT["steps"],
    )
    db_session.add(fragment)
    await db_session.commit()

    workflow_json = {
        "name": "with-include",
        "steps": [
            {"include": "bc_login"},
            {"order": 99, "name": "Do work", "action": "click", "selector": "#work"},
        ],
    }
    result = await resolve_fragments(workflow_json, db_session)

    # 4 fragment steps + 1 regular step
    assert len(result["steps"]) == 5
    # Re-numbered sequentially
    assert [s["order"] for s in result["steps"]] == [1, 2, 3, 4, 5]
    # First 4 are from fragment
    assert result["steps"][0]["name"] == "Navigate to bC"
    assert result["steps"][3]["name"] == "Click login"
    # Last is the original step
    assert result["steps"][4]["name"] == "Do work"


@pytest.mark.asyncio
async def test_resolve_multiple_includes(db_session):
    """Multiple includes in sequence are both resolved."""
    frag1 = WorkflowFragment(
        fragment_id="bc_login",
        name="bC Login",
        steps=BC_LOGIN_FRAGMENT["steps"],
    )
    frag2 = WorkflowFragment(
        fragment_id="bc_download",
        name="bC Download",
        steps=DOWNLOAD_FRAGMENT["steps"],
    )
    db_session.add_all([frag1, frag2])
    await db_session.commit()

    workflow_json = {
        "name": "multi-include",
        "steps": [
            {"include": "bc_login"},
            {"include": "bc_download"},
            {"order": 99, "name": "Cleanup", "action": "click", "selector": "#logout"},
        ],
    }
    result = await resolve_fragments(workflow_json, db_session)

    # 4 login + 3 download + 1 regular = 8
    assert len(result["steps"]) == 8
    assert [s["order"] for s in result["steps"]] == list(range(1, 9))


@pytest.mark.asyncio
async def test_resolve_with_variable_overrides(db_session):
    """Include variables substitute into fragment step fields."""
    fragment = WorkflowFragment(
        fragment_id="bc_login",
        name="bC Login",
        variables=BC_LOGIN_FRAGMENT["variables"],
        steps=BC_LOGIN_FRAGMENT["steps"],
    )
    db_session.add(fragment)
    await db_session.commit()

    workflow_json = {
        "name": "with-vars",
        "steps": [
            {
                "include": "bc_login",
                "variables": {
                    "BC_USERNAME": "admin@ccm.com",
                    "BC_PASSWORD": "secret123",
                },
            },
        ],
    }
    result = await resolve_fragments(workflow_json, db_session)

    assert len(result["steps"]) == 4
    # Variables should be substituted in the fragment steps
    assert result["steps"][1]["value"] == "admin@ccm.com"  # was ${BC_USERNAME}
    assert result["steps"][2]["value"] == "secret123"       # was ${BC_PASSWORD}
    # Non-variable fields unchanged
    assert result["steps"][0]["value"] == "https://bc.example.com"


@pytest.mark.asyncio
async def test_resolve_missing_fragment_raises(db_session):
    """Clear error raised for unknown fragment_id."""
    workflow_json = {
        "name": "bad-ref",
        "steps": [
            {"include": "nonexistent_fragment"},
        ],
    }
    with pytest.raises(ValueError, match="Fragment not found: nonexistent_fragment"):
        await resolve_fragments(workflow_json, db_session)


@pytest.mark.asyncio
async def test_resolve_preserves_regular_steps(db_session):
    """Non-include steps pass through unchanged."""
    fragment = WorkflowFragment(
        fragment_id="bc_login",
        name="bC Login",
        steps=[{"order": 1, "name": "Nav", "action": "navigate", "value": "https://bc.example.com"}],
    )
    db_session.add(fragment)
    await db_session.commit()

    workflow_json = {
        "name": "mixed",
        "steps": [
            {"order": 1, "name": "Before", "action": "navigate", "value": "https://start.com"},
            {"include": "bc_login"},
            {"order": 3, "name": "After", "action": "click", "selector": "#done"},
        ],
    }
    result = await resolve_fragments(workflow_json, db_session)

    assert len(result["steps"]) == 3
    assert result["steps"][0]["name"] == "Before"
    assert result["steps"][0]["value"] == "https://start.com"
    assert result["steps"][1]["name"] == "Nav"
    assert result["steps"][2]["name"] == "After"
    assert result["steps"][2]["selector"] == "#done"


@pytest.mark.asyncio
async def test_fragment_variables_merged_to_workflow(db_session):
    """Fragment variable declarations are added to workflow variables dict."""
    fragment = WorkflowFragment(
        fragment_id="bc_login",
        name="bC Login",
        variables={
            "BC_USERNAME": {"type": "credential", "default": "", "description": "bC user"},
            "BC_PASSWORD": {"type": "credential", "default": "", "description": "bC pass"},
        },
        steps=[{"order": 1, "name": "Nav", "action": "navigate", "value": "https://bc.example.com"}],
    )
    db_session.add(fragment)
    await db_session.commit()

    workflow_json = {
        "name": "merge-vars",
        "variables": {
            "MY_VAR": {"type": "string", "default": "hello", "description": "existing var"},
        },
        "steps": [
            {"include": "bc_login"},
        ],
    }
    result = await resolve_fragments(workflow_json, db_session)

    # Original variable preserved
    assert "MY_VAR" in result["variables"]
    # Fragment variables merged in
    assert "BC_USERNAME" in result["variables"]
    assert "BC_PASSWORD" in result["variables"]
    assert result["variables"]["BC_USERNAME"]["type"] == "credential"


@pytest.mark.asyncio
async def test_fragment_variables_dont_override_existing(db_session):
    """Existing workflow variable declarations take precedence over fragment ones."""
    fragment = WorkflowFragment(
        fragment_id="bc_login",
        name="bC Login",
        variables={
            "SHARED_VAR": {"type": "string", "default": "from-fragment", "description": "fragment default"},
        },
        steps=[{"order": 1, "name": "Nav", "action": "navigate", "value": "https://bc.example.com"}],
    )
    db_session.add(fragment)
    await db_session.commit()

    workflow_json = {
        "name": "no-override",
        "variables": {
            "SHARED_VAR": {"type": "string", "default": "from-workflow", "description": "workflow wins"},
        },
        "steps": [
            {"include": "bc_login"},
        ],
    }
    result = await resolve_fragments(workflow_json, db_session)

    # Workflow declaration wins
    assert result["variables"]["SHARED_VAR"]["default"] == "from-workflow"


# ---------------------------------------------------------------------------
# Fragment CRUD API tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_fragment(client):
    """POST creates a fragment."""
    resp = await client.post("/api/fragments", json=BC_LOGIN_FRAGMENT)
    assert resp.status_code == 201
    data = resp.json()
    assert data["fragment_id"] == "bc_login"
    assert data["name"] == "bC Login"
    assert data["version"] == 1
    assert len(data["steps"]) == 4


@pytest.mark.asyncio
async def test_update_fragment_increments_version(client):
    """POST same fragment_id increments version."""
    await client.post("/api/fragments", json=BC_LOGIN_FRAGMENT)

    updated = {**BC_LOGIN_FRAGMENT, "name": "bC Login v2"}
    resp = await client.post("/api/fragments", json=updated)
    assert resp.status_code == 201
    data = resp.json()
    assert data["version"] == 2
    assert data["name"] == "bC Login v2"


@pytest.mark.asyncio
async def test_list_fragments(client):
    """GET returns all fragments."""
    await client.post("/api/fragments", json=BC_LOGIN_FRAGMENT)
    await client.post("/api/fragments", json=DOWNLOAD_FRAGMENT)

    resp = await client.get("/api/fragments")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2
    # Check that step_count is returned
    fragment_ids = {f["fragment_id"] for f in data}
    assert "bc_login" in fragment_ids
    assert "bc_download" in fragment_ids


@pytest.mark.asyncio
async def test_get_fragment_by_id(client):
    """GET by fragment_id returns full fragment."""
    await client.post("/api/fragments", json=BC_LOGIN_FRAGMENT)

    resp = await client.get("/api/fragments/bc_login")
    assert resp.status_code == 200
    data = resp.json()
    assert data["fragment_id"] == "bc_login"
    assert len(data["steps"]) == 4
    assert data["variables"] is not None


@pytest.mark.asyncio
async def test_get_fragment_not_found(client):
    """GET unknown fragment_id returns 404."""
    resp = await client.get("/api/fragments/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_fragment(client):
    """DELETE removes fragment."""
    await client.post("/api/fragments", json=BC_LOGIN_FRAGMENT)

    resp = await client.delete("/api/fragments/bc_login")
    assert resp.status_code == 204

    # Verify it's gone
    resp = await client.get("/api/fragments/bc_login")
    assert resp.status_code == 404
