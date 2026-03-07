"""Tests for CLI commands."""

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
