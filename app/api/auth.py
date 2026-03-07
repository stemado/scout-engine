"""Auth API routes: invite management and key registration."""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
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


# -- Request / response models -----------------------------------------------


class CreateInviteRequest(BaseModel):
    label: str = Field(min_length=1, max_length=255)


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


# -- Endpoints ----------------------------------------------------------------


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

    # Compare using naive UTC to handle both SQLite (naive) and PostgreSQL (aware)
    now_utc = datetime.now(timezone.utc)
    expires = invite.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now_utc:
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
    request: Request,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(_require_admin),
):
    """Revoke an API key (admin only)."""
    try:
        key_uuid = _uuid.UUID(key_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="API key not found")

    # Prevent self-revocation
    caller_key_id = getattr(request.state, "api_key_id", None)
    if caller_key_id and caller_key_id == key_uuid:
        raise HTTPException(status_code=400, detail="Cannot revoke your own key")

    result = await db.execute(select(ApiKey).where(ApiKey.id == key_uuid))
    key = result.scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    key.revoked = True
    await db.commit()
