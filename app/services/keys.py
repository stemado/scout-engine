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
