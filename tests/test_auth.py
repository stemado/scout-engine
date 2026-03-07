"""Tests for API key authentication."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import ApiKey
from app.services.keys import generate_api_key, hash_key


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _create_api_key(client, label="test", is_admin=False):
    """Helper: insert an API key into the test DB and return the raw key."""
    raw_key = generate_api_key()
    # Get a session from the test DB override
    async for session in app.dependency_overrides[get_db]():
        key_record = ApiKey(
            key_hash=hash_key(raw_key),
            key_prefix=raw_key[:8],
            label=label,
            is_admin=is_admin,
        )
        session.add(key_record)
        await session.commit()
    return raw_key


async def test_health_requires_no_auth(client):
    """Health endpoint should always be accessible."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200


async def test_protected_endpoint_rejects_no_key(client, monkeypatch):
    """When API_KEY is set, requests without a key get 401."""
    monkeypatch.setattr(settings, "api_key", "test-secret-key")
    resp = await client.get("/api/workflows")
    assert resp.status_code == 401
    assert "Missing" in resp.json()["detail"]


async def test_protected_endpoint_rejects_wrong_key(client, monkeypatch):
    """Wrong key gets 403."""
    monkeypatch.setattr(settings, "api_key", "test-secret-key")
    resp = await client.get(
        "/api/workflows", headers={"Authorization": "Bearer wrong-key"}
    )
    assert resp.status_code == 403
    assert "Invalid" in resp.json()["detail"]


async def test_protected_endpoint_accepts_correct_key(client, monkeypatch):
    """Correct key passes through."""
    monkeypatch.setattr(settings, "api_key", "test-secret-key")
    resp = await client.get(
        "/api/workflows", headers={"Authorization": "Bearer test-secret-key"}
    )
    assert resp.status_code == 200


async def test_auth_disabled_when_no_key_configured(client, monkeypatch):
    """When API_KEY is empty, all requests pass (local dev mode)."""
    monkeypatch.setattr(settings, "api_key", "")
    resp = await client.get("/api/workflows")
    assert resp.status_code == 200


async def test_db_key_accepted(client, monkeypatch):
    """A key stored in the database should grant access."""
    monkeypatch.setattr(settings, "api_key", "")  # disable legacy key
    raw_key = await _create_api_key(client)
    resp = await client.get(
        "/api/workflows", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert resp.status_code == 200


async def test_revoked_key_rejected(client, monkeypatch):
    """A revoked key should be rejected."""
    monkeypatch.setattr(settings, "api_key", "")
    raw_key = await _create_api_key(client)
    # Revoke it
    async for session in app.dependency_overrides[get_db]():
        await session.execute(
            update(ApiKey).where(ApiKey.key_hash == hash_key(raw_key)).values(revoked=True)
        )
        await session.commit()
    resp = await client.get(
        "/api/workflows", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert resp.status_code == 403


async def test_legacy_key_still_works(client, monkeypatch):
    """settings.api_key should still work as a fallback."""
    monkeypatch.setattr(settings, "api_key", "legacy-secret")
    resp = await client.get(
        "/api/workflows", headers={"Authorization": "Bearer legacy-secret"}
    )
    assert resp.status_code == 200


@pytest.mark.xfail(reason="Route /api/auth/invites not yet implemented (Task 5)")
async def test_legacy_key_has_admin_access(client, monkeypatch):
    """Legacy key should be treated as admin (can create invites during migration)."""
    monkeypatch.setattr(settings, "api_key", "legacy-secret")
    resp = await client.post(
        "/api/auth/invites",
        json={"label": "Alice"},
        headers={"Authorization": "Bearer legacy-secret"},
    )
    assert resp.status_code == 201
