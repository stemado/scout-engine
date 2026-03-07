# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

scout-engine is a FastAPI service that executes Scout browser automation workflows. It receives workflow JSON (Scout schema v1.0), stores it in PostgreSQL, and runs browser actions via `botasaurus-driver`. Execution results (pass/fail, screenshots, timing) are tracked per-step. Cron scheduling via APScheduler and webhook notifications via httpx are planned.

## Commands

```bash
# Install dependencies (uses uv, not pip)
uv sync --group dev

# Run the server
uv run scout-engine                # CLI entry point (app.main:cli)
uv run uvicorn app.main:app       # direct uvicorn

# Run all tests
uv run pytest

# Run a single test file or test
uv run pytest tests/test_schemas.py
uv run pytest tests/test_schemas.py::test_minimal_workflow -v

# Database migrations (requires running PostgreSQL)
uv run alembic revision --autogenerate -m "description"
uv run alembic upgrade head

# Create the first admin API key (run on server)
uv run scout-engine-create-key --label "Your Name"
```

## Architecture

**Async-first stack**: FastAPI + SQLAlchemy async sessions (`asyncpg` for PostgreSQL) + Alembic for migrations. Tests use SQLite in-memory via `aiosqlite`.

**Key modules**:
- `app/main.py` — FastAPI app with lifespan context manager, health endpoint, CLI entry
- `app/config.py` — `pydantic-settings` singleton (`settings`) reading from `.env`
- `app/schemas.py` — Vendored Pydantic models from the Scout project (workflow schema v1.0). These are passive validation models, not ORM models
- `app/models.py` — SQLAlchemy ORM: `WorkflowRecord`, `Execution`, `ExecutionStep`, `Schedule`, `ApiKey`, `InviteToken`
- `app/database.py` — Async engine + session factory, `get_db()` FastAPI dependency
- `app/services/keys.py` — API key generation and SHA-256 hashing utilities
- `app/api/auth.py` — Auth endpoints (invites, registration, key management)
- `app/migrations/` — Alembic (async-aware env.py, no migrations generated yet)

**Data flow**: Workflow JSON → validate with Pydantic schemas → store as `WorkflowRecord` (JSONB) → create `Execution` → run steps via botasaurus-driver → record `ExecutionStep` results

## API Endpoints

- `POST /api/workflows` -- Upload workflow JSON
- `GET /api/workflows` -- List workflows
- `POST /api/workflows/{id}/run` -- Execute immediately
- `GET /api/executions` -- List recent executions
- `GET /api/executions/{id}` -- Execution status + step results
- `POST /api/executions/{id}/stop` -- Cancel a running execution
- `GET /api/executions/{id}/browser` -- CDP connection info for live browser attachment
- `POST /api/executions/{id}/pause` -- Request pause (takes effect after current step)
- `POST /api/executions/{id}/resume` -- Resume a paused execution (retry/continue/abort/jump)
- `POST /api/schedules` -- Create cron schedule
- `GET /api/schedules` -- List schedules
- `PUT /api/schedules/{id}` -- Update a schedule
- `DELETE /api/schedules/{id}` -- Delete a schedule
- `POST /api/auth/invites` -- Create invite token (admin only)
- `POST /api/auth/register` -- Exchange invite for API key (public)
- `GET /api/auth/keys` -- List all API keys (admin only)
- `DELETE /api/auth/keys/{id}` -- Revoke an API key (admin only)
- `GET /api/health` -- Health check

## Conventions

- **JsonVariant pattern**: `JSONB().with_variant(JSON, "sqlite")` allows JSONB in production and plain JSON in SQLite tests. Use this for any new JSON columns.
- **Pydantic strict mode**: All vendored schemas use `extra="forbid"`. Tests assert unknown fields are rejected.
- **UUID primary keys**: All ORM models use `uuid.uuid4` defaults.
- **Settings singleton**: Import `from app.config import settings` — never instantiate a second `Settings()`.
- **Vendored code**: `app/schemas.py` is copied from Scout to avoid a package dependency. Preserve the source header comment when updating.
- **Test marker**: `@pytest.mark.integration` for tests needing a real browser or database. pytest-asyncio mode is `"auto"` (no need for `@pytest.mark.asyncio`).
- **Status enums are strings**: Execution status is `pending/running/completed/failed/cancelled`; step status is `pending/running/passed/failed/skipped`. These are stored as `String(20)`, not Python enums.
- **API key hashing**: Keys are SHA-256 hashed before storage. Raw keys are shown once at creation and never stored. Use `app.services.keys.hash_key()` for all hashing.
- **Admin guard**: Use `Depends(_require_admin)` from `app/api/auth.py` for admin-only endpoints. The middleware sets `request.state.is_admin` and `request.state.api_key_id`.
- **Middleware session access**: Auth middleware uses `import app.database as app_database` (module reference) so tests can patch `app_database.async_session`. Never use `from app.database import async_session` in middleware.
