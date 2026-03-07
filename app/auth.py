"""API key authentication middleware."""

import asyncio
import hmac
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import app.database as app_database
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
        async with app_database.async_session() as session:
            result = await session.execute(
                select(ApiKey).where(ApiKey.key_hash == key_hash)
            )
            return result.scalar_one_or_none()

    async def _touch_last_used(self, key_id) -> None:
        try:
            async with app_database.async_session() as session:
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
            async with app_database.async_session() as session:
                result = await session.execute(select(ApiKey.id).limit(1))
                return result.scalar_one_or_none() is not None
        except Exception:
            logger.warning("Cannot reach database for auth check", exc_info=True)
            return None  # signals "unknown" — caller must fail closed
