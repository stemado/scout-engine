"""Tests for API key authentication."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


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
