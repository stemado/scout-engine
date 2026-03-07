"""Tests for auth API endpoints (invites, key management)."""

import pytest
from httpx import ASGITransport, AsyncClient

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


async def _insert_key(label="admin", is_admin=True):
    """Insert an API key directly and return the raw key."""
    raw_key = generate_api_key()
    async for session in app.dependency_overrides[get_db]():
        session.add(ApiKey(
            key_hash=hash_key(raw_key),
            key_prefix=raw_key[:8],
            label=label,
            is_admin=is_admin,
        ))
        await session.commit()
    return raw_key


async def test_create_invite(client):
    """Admin can create an invite token."""
    admin_key = await _insert_key()
    resp = await client.post(
        "/api/auth/invites",
        json={"label": "Alice"},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["invite_token"].startswith("sk_inv_")
    assert data["label"] == "Alice"
    assert "expires_at" in data


async def test_non_admin_cannot_create_invite(client):
    """Non-admin key should be rejected for invite creation."""
    user_key = await _insert_key(label="user", is_admin=False)
    resp = await client.post(
        "/api/auth/invites",
        json={"label": "Bob"},
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert resp.status_code == 403


async def test_register_with_invite(client):
    """Exchange a valid invite token for an API key."""
    admin_key = await _insert_key()
    # Create invite
    invite_resp = await client.post(
        "/api/auth/invites",
        json={"label": "Alice"},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    invite_token = invite_resp.json()["invite_token"]

    # Register (public endpoint)
    resp = await client.post(
        "/api/auth/register",
        json={"invite_token": invite_token},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["api_key"].startswith("sk_")
    assert data["label"] == "Alice"


async def test_invite_single_use(client):
    """An invite token can only be used once."""
    admin_key = await _insert_key()
    invite_resp = await client.post(
        "/api/auth/invites",
        json={"label": "Alice"},
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    invite_token = invite_resp.json()["invite_token"]

    # First use succeeds
    resp1 = await client.post("/api/auth/register", json={"invite_token": invite_token})
    assert resp1.status_code == 201

    # Second use fails
    resp2 = await client.post("/api/auth/register", json={"invite_token": invite_token})
    assert resp2.status_code == 400


async def test_list_keys(client):
    """Admin can list all API keys (without hashes)."""
    admin_key = await _insert_key()
    resp = await client.get(
        "/api/auth/keys",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert resp.status_code == 200
    keys = resp.json()
    assert len(keys) >= 1
    # Should NOT expose key_hash
    assert "key_hash" not in keys[0]
    assert "key_prefix" in keys[0]


async def test_revoke_key(client):
    """Admin can revoke an API key."""
    admin_key = await _insert_key()
    user_key = await _insert_key(label="doomed", is_admin=False)

    # List to get the user key's ID
    list_resp = await client.get(
        "/api/auth/keys",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    user_record = [k for k in list_resp.json() if k["label"] == "doomed"][0]

    # Revoke
    resp = await client.delete(
        f"/api/auth/keys/{user_record['id']}",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    assert resp.status_code == 204

    # Verify the revoked key no longer works
    resp = await client.get(
        "/api/workflows",
        headers={"Authorization": f"Bearer {user_key}"},
    )
    assert resp.status_code == 403
