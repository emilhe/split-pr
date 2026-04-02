"""JWT token creation and verification."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Optional

from jose import JWTError, jwt

from utils.validation import validate_email


SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-secret-key-change-in-production")
ALGORITHM = "HS256"
DEFAULT_EXPIRE_MINUTES = 60


def create_access_token(
    subject: str,
    roles: list[str] | None = None,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a signed JWT access token.

    Args:
        subject: The token subject (usually user email).
        roles: List of role identifiers.
        expires_delta: Custom expiration time. Defaults to 60 minutes.
        extra_claims: Additional claims to include in the token.

    Returns:
        Encoded JWT string.
    """
    now = datetime.utcnow()
    expire = now + (expires_delta or timedelta(minutes=DEFAULT_EXPIRE_MINUTES))

    claims = {
        "sub": subject,
        "iat": now,
        "exp": expire,
        "roles": roles or [],
    }
    if extra_claims:
        claims.update(extra_claims)

    return jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> Optional[dict[str, Any]]:
    """Verify and decode a JWT token.

    Returns:
        Decoded claims dict, or None if verification fails.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        subject: str = payload.get("sub", "")
        if not subject:
            return None
        return payload
    except JWTError:
        return None


def extract_roles(token: str) -> list[str]:
    """Extract roles from a verified token.

    Returns:
        List of role strings, or empty list if token is invalid.
    """
    payload = verify_token(token)
    if payload is None:
        return []
    return payload.get("roles", [])


def is_token_expired(token: str) -> bool:
    """Check if a token has expired without raising.

    Returns:
        True if expired or invalid, False if still valid.
    """
    try:
        jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return False
    except JWTError:
        return True
