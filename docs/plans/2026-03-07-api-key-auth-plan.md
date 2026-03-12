# API Key Auth & Invite Flow Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the single static API key with per-user API keys, managed via an invite flow so admins can onboard teammates without SSH.

**Architecture:** Add `ApiKey` table storing hashed keys tied to a label (no full user/account model — YAGNI for a small team). Admin creates invite tokens via CLI; teammates exchange invites for real API keys via a REST endpoint. The existing `ApiKeyMiddleware` is updated to validate against the database instead of a single env var.

**Tech Stack:** FastAPI, SQLAlchemy async, `secrets.token_urlsafe` for key generation, `hashlib.sha256` for key hashing (not bcrypt — API keys are high-entropy random tokens, not passwords). Alembic for migration.

---

## Overview

### Data model

- `api_keys` table: `id (UUID)`, `key_hash (String)`, `key_prefix (String(8))`, `label (String)`, `is_admin (Bool)`, `created_at`, `last_used_at`, `revoked (Bool)`
- `invite_tokens` table: `id (UUID)`, `token_hash (String)`, `label (String)`, `created_by (UUID FK→api_keys)`, `expires_at`, `used_at`, `created_at`

### Key format

Keys use prefix `sk_` + 32 bytes urlsafe base64 = `sk_<43 chars>`. The prefix `sk_` makes keys recognizable in logs/configs. Only the SHA-256 hash is stored; the raw key is shown exactly once at creation.

Invite tokens use prefix `sk_inv_` + 16 bytes urlsafe base64. They are single-use and expire in 24 hours.

### Auth flow changes

1. Legacy check: if `settings.api_key` matches, treat as admin and allow (smooth migration)
2. DB check: look up `key_hash = sha256(bearer_token)` in `api_keys` table
3. If found and not revoked → allow request, fire-and-forget `last_used_at` update
4. If not found → 401/403 as before
5. Fail-closed: if the DB is unreachable during key lookup, return 503 (never silently skip auth)

### Testability

The middleware uses `import app.database` (module reference) rather than `from app.database import async_session` (value copy). This ensures that when `conftest.py` patches `app.database.async_session`, the middleware sees the test session factory. The conftest is updated to patch this module-level attribute.

### Endpoints

- `POST /api/auth/invites` — admin creates an invite (requires `is_admin` key)
- `POST /api/auth/register` — exchange invite token for API key (public)
- `GET /api/auth/keys` — admin lists all keys (redacted)
- `DELETE /api/auth/keys/{id}` — admin revokes a key

### CLI command

- `scout-engine create-admin-key --label "Sean"` — bootstraps the first admin key (writes to stdout, no invite needed)

---

## Task 1: Add `ApiKey` and `InviteToken` models

**Files:**
- Modify: `app/models.py`
- Test: `tests/test_models.py` (create new)

**Step 1: Write the failing test**

Create `tests/test_models.py`:

```python
"""Tests for ORM models."""

from app.models import ApiKey, InviteToken


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
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'ApiKey'`

**Step 3: Write minimal implementation**

Add to the end of `app/models.py` (before any EOF):

```python
class ApiKey(Base):
    """A bearer token for API access."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    key_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    invites_created: Mapped[list["InviteToken"]] = relationship(back_populates="created_by_key")


class InviteToken(Base):
    """A single-use invite token that can be exchanged for an API key."""

    __tablename__ = "invite_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    created_by_key: Mapped["ApiKey"] = relationship(back_populates="invites_created")
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/models.py tests/test_models.py
git commit -m "feat(auth): add ApiKey and InviteToken ORM models"
```

---

## Task 2: Generate Alembic migration

**Files:**
- Create: `app/migrations/versions/<auto>_add_api_keys_and_invite_tokens.py`

**Step 1: Generate the migration**

Run: `uv run alembic revision --autogenerate -m "add api_keys and invite_tokens"`

**Step 2: Review the generated migration**

Open the file and verify it creates `api_keys` and `invite_tokens` tables with the correct columns, indexes, and foreign keys. Ensure the `JsonVariant` pattern is not applied here (no JSON columns in these tables).

**Step 3: Commit**

```bash
git add app/migrations/versions/
git commit -m "feat(auth): add migration for api_keys and invite_tokens"
```

---

## Task 3: Add key generation utility

**Files:**
- Create: `app/services/keys.py`
- Test: `tests/test_keys.py` (create new)

**Step 1: Write the failing test**

Create `tests/test_keys.py`:

```python
"""Tests for API key generation utilities."""

from app.services.keys import generate_api_key, generate_invite_token, hash_key


def test_generate_api_key_format():
    """API key should start with sk_ and be 46 chars total."""
    key = generate_api_key()
    assert key.startswith("sk_")
    assert len(key) == 46  # "sk_" + 43 chars of base64


def test_generate_invite_token_format():
    """Invite token should start with sk_inv_ and be 29 chars total."""
    token = generate_invite_token()
    assert token.startswith("sk_inv_")
    assert len(token) == 29  # "sk_inv_" + 22 chars of base64


def test_hash_key_deterministic():
    """Same input should produce the same hash."""
    assert hash_key("sk_abc123") == hash_key("sk_abc123")


def test_hash_key_different_inputs():
    """Different inputs should produce different hashes."""
    assert hash_key("sk_abc123") != hash_key("sk_def456")


def test_generate_api_key_unique():
    """Two generated keys should not collide."""
    assert generate_api_key() != generate_api_key()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_keys.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

Create `app/services/keys.py`:

```python
"""API key and invite token generation utilities."""

import hashlib
import secrets


def generate_api_key() -> str:
    """Generate a new API key: sk_ + 32 random bytes (base64)."""
    return "sk_" + secrets.token_urlsafe(32)


def generate_invite_token() -> str:
    """Generate a single-use invite token: sk_inv_ + 16 random bytes (base64)."""
    return "sk_inv_" + secrets.token_urlsafe(16)


def hash_key(raw_key: str) -> str:
    """SHA-256 hash of a raw key. Returns hex digest (64 chars)."""
    return hashlib.sha256(raw_key.encode()).hexdigest()
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_keys.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/keys.py tests/test_keys.py
git commit -m "feat(auth): add key generation and hashing utilities"
```

---

## Task 4: Update auth middleware to validate against database

**Files:**
- Modify: `app/auth.py`
- Modify: `tests/test_auth.py`

**Step 1: Write the failing tests**

Add new tests to `tests/test_auth.py`:

```python
from app.models import ApiKey
from app.services.keys import generate_api_key, hash_key
from app.database import get_db


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
        from sqlalchemy import update
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


async def test_legacy_key_has_admin_access(client, monkeypatch):
    """Legacy key should be treated as admin (can create invites during migration)."""
    monkeypatch.setattr(settings, "api_key", "legacy-secret")
    resp = await client.post(
        "/api/auth/invites",
        json={"label": "Alice"},
        headers={"Authorization": "Bearer legacy-secret"},
    )
    assert resp.status_code == 201
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_auth.py -v`
Expected: FAIL — db lookup not implemented yet

**Step 3: Rewrite `app/auth.py`**

> **Design decisions (from adversarial review):**
> - Uses `import app.database` (module ref) not `from app.database import async_session` (value copy) so tests can patch `app.database.async_session` and the middleware sees it. (Issue #1)
> - Legacy key sets `request.state.is_admin = True` so the legacy key holder can create invites during migration. (Issue #2)
> - DB errors in `_has_any_db_keys` return 503 (fail closed), not silently skip auth. (Issue #4)
> - `_touch_last_used` uses `asyncio.create_task` so it doesn't block the response. (Issue #5)

Replace the contents of `app/auth.py` with:

```python
"""API key authentication middleware."""

import asyncio
import hmac
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import app.database
from app.config import settings
from app.models import ApiKey
from app.services.keys import hash_key

logger = logging.getLogger(__name__)

# Paths that never require authentication
PUBLIC_PATHS = {"/api/health", "/docs", "/openapi.json", "/redoc", "/api/auth/register"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid Bearer token.

    Checks (in order):
    1. Legacy static key from settings.api_key (treated as admin)
    2. Hashed key lookup in api_keys table
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip auth for public paths (check FIRST, before any DB calls)
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # Skip auth when no key is configured AND no db keys exist (local dev)
        if not settings.api_key:
            has_db_keys = await self._has_any_db_keys()
            if has_db_keys is None:
                # DB unreachable — fail closed
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Auth service unavailable"},
                )
            if not has_db_keys:
                return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing Authorization header"},
            )

        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0] != "Bearer":
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing Bearer token"},
            )

        token = parts[1]

        # 1. Check legacy static key — treat as admin for migration
        if settings.api_key and hmac.compare_digest(token, settings.api_key):
            request.state.is_admin = True
            return await call_next(request)

        # 2. Check database keys
        api_key_record = await self._lookup_key(token)
        if api_key_record is None:
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid API key"},
            )
        if api_key_record.revoked:
            return JSONResponse(
                status_code=403,
                content={"detail": "API key has been revoked"},
            )

        # Stamp last_used_at in background (don't block the response)
        asyncio.create_task(self._touch_last_used(api_key_record.id))

        # Attach key info to request state for downstream use
        request.state.api_key_id = api_key_record.id
        request.state.is_admin = api_key_record.is_admin

        return await call_next(request)

    async def _lookup_key(self, raw_token: str) -> ApiKey | None:
        key_hash = hash_key(raw_token)
        async with app.database.async_session() as session:
            result = await session.execute(
                select(ApiKey).where(ApiKey.key_hash == key_hash)
            )
            return result.scalar_one_or_none()

    async def _touch_last_used(self, key_id) -> None:
        try:
            async with app.database.async_session() as session:
                await session.execute(
                    update(ApiKey)
                    .where(ApiKey.id == key_id)
                    .values(last_used_at=datetime.now(timezone.utc))
                )
                await session.commit()
        except Exception:
            logger.warning("Failed to update last_used_at for key %s", key_id)

    async def _has_any_db_keys(self) -> bool | None:
        """Check if any API keys exist. Returns None if DB is unreachable."""
        try:
            async with app.database.async_session() as session:
                result = await session.execute(select(ApiKey.id).limit(1))
                return result.scalar_one_or_none() is not None
        except Exception:
            logger.warning("Cannot reach database for auth check", exc_info=True)
            return None  # signals "unknown" — caller must fail closed
```

**Step 4: Run all auth tests**

Run: `uv run pytest tests/test_auth.py -v`
Expected: ALL PASS

**Step 5: Run full test suite to check for regressions**

Run: `uv run pytest -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add app/auth.py tests/test_auth.py
git commit -m "feat(auth): validate API keys against database with legacy fallback"
```

---

## Task 5: Add auth API routes (invites + key management)

**Files:**
- Create: `app/api/auth.py`
- Modify: `app/main.py` (register router)
- Test: `tests/test_auth_api.py` (create new)

**Step 1: Write the failing tests**

Create `tests/test_auth_api.py`:

```python
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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_auth_api.py -v`
Expected: FAIL — routes don't exist yet

**Step 3: Create `app/api/auth.py`**

```python
"""Auth API routes: invite management and key registration."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ApiKey, InviteToken
from app.services.keys import generate_api_key, generate_invite_token, hash_key

router = APIRouter(prefix="/api/auth", tags=["auth"])

INVITE_TTL_HOURS = 24


def _require_admin(request: Request):
    """Dependency: require the caller to be an admin."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


# ── Request / response models ──────────────────────────────

class CreateInviteRequest(BaseModel):
    label: str


class CreateInviteResponse(BaseModel):
    invite_token: str
    label: str
    expires_at: datetime


class RegisterRequest(BaseModel):
    invite_token: str


class RegisterResponse(BaseModel):
    api_key: str
    label: str


class KeyInfo(BaseModel):
    id: str
    key_prefix: str
    label: str
    is_admin: bool
    revoked: bool
    created_at: datetime
    last_used_at: datetime | None


# ── Endpoints ───────────────────────────────────────────────

@router.post("/invites", status_code=201, response_model=CreateInviteResponse)
async def create_invite(
    body: CreateInviteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(_require_admin),
):
    """Create a single-use invite token (admin only)."""
    raw_token = generate_invite_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=INVITE_TTL_HOURS)

    # Legacy keys have no api_key_id (no DB record); created_by_id is nullable
    created_by = getattr(request.state, "api_key_id", None)

    invite = InviteToken(
        token_hash=hash_key(raw_token),
        label=body.label,
        created_by_id=created_by,
        expires_at=expires_at,
    )
    db.add(invite)
    await db.commit()

    return CreateInviteResponse(
        invite_token=raw_token,
        label=body.label,
        expires_at=expires_at,
    )


@router.post("/register", status_code=201, response_model=RegisterResponse)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a valid invite token for a new API key (public)."""
    token_hash = hash_key(body.invite_token)
    # SELECT FOR UPDATE prevents race condition where two concurrent requests
    # both read the same unused invite and both create keys. On SQLite (tests)
    # this is a no-op since SQLite serializes writes anyway.
    result = await db.execute(
        select(InviteToken)
        .where(InviteToken.token_hash == token_hash)
        .with_for_update()
    )
    invite = result.scalar_one_or_none()

    if invite is None:
        raise HTTPException(status_code=400, detail="Invalid invite token")
    if invite.used_at is not None:
        raise HTTPException(status_code=400, detail="Invite token already used")
    if invite.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invite token has expired")

    # Mark invite as used
    invite.used_at = datetime.now(timezone.utc)

    # Generate new API key
    raw_key = generate_api_key()
    api_key = ApiKey(
        key_hash=hash_key(raw_key),
        key_prefix=raw_key[:8],
        label=invite.label,
        is_admin=False,
    )
    db.add(api_key)
    await db.commit()

    return RegisterResponse(api_key=raw_key, label=invite.label)


@router.get("/keys", response_model=list[KeyInfo])
async def list_keys(
    db: AsyncSession = Depends(get_db),
    _admin=Depends(_require_admin),
):
    """List all API keys (admin only). Key hashes are never exposed."""
    result = await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    keys = result.scalars().all()
    return [
        KeyInfo(
            id=str(k.id),
            key_prefix=k.key_prefix,
            label=k.label,
            is_admin=k.is_admin,
            revoked=k.revoked,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
        )
        for k in keys
    ]


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(
    key_id: str,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(_require_admin),
):
    """Revoke an API key (admin only)."""
    result = await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    key.revoked = True
    await db.commit()
```

**Step 4: Register the router in `app/main.py`**

Add this import near the top with the other router imports:

```python
from .api.auth import router as auth_router
```

Add this line after the other `include_router` calls:

```python
app.include_router(auth_router)
```

**Step 5: Run the tests**

Run: `uv run pytest tests/test_auth_api.py -v`
Expected: ALL PASS

**Step 6: Run full test suite**

Run: `uv run pytest -v`
Expected: ALL PASS

**Step 7: Commit**

```bash
git add app/api/auth.py app/main.py tests/test_auth_api.py
git commit -m "feat(auth): add invite and key management API endpoints"
```

---

## Task 6: Add `create-admin-key` CLI command

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_cli.py` (create new)

**Step 1: Write the failing test**

Create `tests/test_cli.py`:

```python
"""Tests for CLI commands."""

from unittest.mock import AsyncMock, patch

from app.models import ApiKey


async def test_create_admin_key(test_db, capsys):
    """create-admin-key should insert an admin key and print it."""
    from app.main import _create_admin_key_impl

    # Run inside the test event loop with the test DB
    raw_key = await _create_admin_key_impl(label="Sean")

    assert raw_key.startswith("sk_")

    # Verify it's in the database
    from app.database import get_db
    from app.main import app
    from sqlalchemy import select

    async for session in app.dependency_overrides[get_db]():
        result = await session.execute(select(ApiKey).where(ApiKey.label == "Sean"))
        record = result.scalar_one()
        assert record.is_admin is True
        assert record.revoked is False
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `_create_admin_key_impl` doesn't exist

**Step 3: Add the CLI command to `app/main.py`**

Add these imports at the top of `app/main.py`:

```python
import asyncio
import sys
```

Add these functions before the `if __name__` block:

```python
async def _create_admin_key_impl(label: str) -> str:
    """Create an admin API key and return the raw key."""
    from app.database import async_session
    from app.models import ApiKey
    from app.services.keys import generate_api_key, hash_key

    raw_key = generate_api_key()
    async with async_session() as session:
        key = ApiKey(
            key_hash=hash_key(raw_key),
            key_prefix=raw_key[:8],
            label=label,
            is_admin=True,
        )
        session.add(key)
        await session.commit()
    return raw_key


def create_admin_key():
    """CLI entry point: create an admin API key."""
    import argparse

    parser = argparse.ArgumentParser(description="Create an admin API key")
    parser.add_argument("--label", required=True, help="Label for the key (e.g. your name)")
    args = parser.parse_args(sys.argv[1:])

    raw_key = asyncio.run(_create_admin_key_impl(args.label))
    print(f"\nAdmin API key created for '{args.label}':")
    print(f"\n  {raw_key}\n")
    print("Save this key — it cannot be retrieved again.")
```

**Step 4: Register the CLI entry point in `pyproject.toml`**

Add under `[project.scripts]`:

```toml
scout-engine-create-key = "app.main:create_admin_key"
```

**Step 5: Run test**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add app/main.py pyproject.toml tests/test_cli.py
git commit -m "feat(auth): add create-admin-key CLI command"
```

---

## Task 7: Update conftest to patch middleware session and run full regression

**Files:**
- Modify: `tests/conftest.py`

> **Why this is needed (Issue #1):** The auth middleware uses `app.database.async_session` directly
> (it can't use FastAPI `Depends`). The existing conftest overrides `get_db` and
> `get_session_factory` but not the module-level `async_session`. Without this patch,
> the middleware would try to connect to real PostgreSQL during tests.

**Step 1: Update `tests/conftest.py`**

Add `app.database.async_session` patching to the `test_db` fixture. The full fixture becomes:

```python
import app.database  # module import for patching


@pytest.fixture(autouse=True)
async def test_db():
    """Override database with async SQLite in-memory for tests.

    Overrides:
    - ``get_db`` for request-scoped sessions (FastAPI dependency)
    - ``get_session_factory`` for background tasks
    - ``app.database.async_session`` for auth middleware (which can't use Depends)
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    def override_get_session_factory():
        return session_factory

    # Patch all three access paths to the session factory
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = override_get_session_factory
    original_async_session = app.database.async_session
    app.database.async_session = session_factory  # middleware reads this at call time

    yield

    app.dependency_overrides.clear()
    app.database.async_session = original_async_session
    await engine.dispose()
```

**Step 2: Run full test suite**

Run: `uv run pytest -v`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test(auth): patch async_session for middleware in conftest"
```

---

## Task 8: Update CLAUDE.md and .env.example

**Files:**
- Modify: `CLAUDE.md`
- Modify: `.env.example` (if it exists)

**Step 1: Update CLAUDE.md**

Add to the **API Endpoints** section:

```markdown
- `POST /api/auth/invites` -- Create invite token (admin only)
- `POST /api/auth/register` -- Exchange invite for API key (public)
- `GET /api/auth/keys` -- List all API keys (admin only)
- `DELETE /api/auth/keys/{id}` -- Revoke an API key (admin only)
```

Add to the **Commands** section:

```markdown
# Create the first admin API key (run on server)
uv run scout-engine-create-key --label "Your Name"
```

Add to the **Conventions** section:

```markdown
- **API key hashing**: Keys are SHA-256 hashed before storage. Raw keys are shown once at creation and never stored. Use `app.services.keys.hash_key()` for all hashing.
- **Admin guard**: Use `Depends(_require_admin)` from `app/api/auth.py` for admin-only endpoints. The middleware sets `request.state.is_admin` and `request.state.api_key_id`.
```

**Step 2: Commit**

```bash
git add CLAUDE.md .env.example
git commit -m "docs: update CLAUDE.md with auth endpoints and conventions"
```

---

## Summary of changes

| File | Action | Purpose |
|------|--------|---------|
| `app/models.py` | Modify | Add `ApiKey` and `InviteToken` models |
| `app/services/keys.py` | Create | Key generation and hashing utilities |
| `app/auth.py` | Rewrite | DB-backed key validation with legacy fallback |
| `app/api/auth.py` | Create | Invite + key management endpoints |
| `app/main.py` | Modify | Register auth router, add CLI command |
| `pyproject.toml` | Modify | Add `scout-engine-create-key` entry point |
| `app/migrations/versions/...` | Create | Alembic migration for new tables |
| `tests/test_models.py` | Create | Model structure tests |
| `tests/test_keys.py` | Create | Key utility tests |
| `tests/test_auth.py` | Modify | Add DB-backed auth tests |
| `tests/test_auth_api.py` | Create | Auth endpoint tests |
| `tests/test_cli.py` | Create | CLI command test |
| `CLAUDE.md` | Modify | Document new endpoints and conventions |

## Deployment sequence

After implementation, on the production server:

```bash
# 1. Pull and install
git pull && uv sync

# 2. Run migration
uv run alembic upgrade head

# 3. Create the first admin key
uv run scout-engine-create-key --label "Sean"
# → prints sk_... — save this

# 4. Remove the old static API_KEY from .env (optional, legacy fallback works)

# 5. Restart the service
systemctl restart scout-engine

# 6. Update your local connection
# /connect https://178.104.0.194 with the new sk_... key

# 7. Invite a teammate
curl -X POST https://178.104.0.194/api/auth/invites \
  -H "Authorization: Bearer sk_..." \
  -d '{"label": "Alice"}'
# → gives them an sk_inv_... token

# 8. Teammate registers
# /connect https://178.104.0.194 --invite sk_inv_...
```
