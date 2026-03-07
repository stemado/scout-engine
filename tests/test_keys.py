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
