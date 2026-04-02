"""Input validation utilities."""

from __future__ import annotations

import re
from uuid import UUID


_EMAIL_PATTERN = re.compile(
    r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
)


def validate_email(email: str) -> bool:
    """Validate an email address format.

    Args:
        email: The email string to validate.

    Returns:
        True if the email format is valid.

    Examples:
        >>> validate_email("user@example.com")
        True
        >>> validate_email("not-an-email")
        False
    """
    if not email or len(email) > 254:
        return False
    return bool(_EMAIL_PATTERN.match(email))


def validate_uuid_string(value: str) -> bool:
    """Check if a string is a valid UUID.

    Args:
        value: The string to check.

    Returns:
        True if the string is a valid UUID format.
    """
    try:
        UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def validate_positive_int(value: int, field_name: str = "value") -> int:
    """Validate that an integer is positive.

    Raises:
        ValueError: If the value is not positive.
    """
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer, got {value}")
    return value


def validate_non_empty_string(value: str, field_name: str = "value") -> str:
    """Validate that a string is non-empty after stripping whitespace.

    Raises:
        ValueError: If the string is empty or only whitespace.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be empty")
    return stripped


def validate_sku_format(sku: str) -> bool:
    """Check if a SKU follows the expected format: uppercase alphanumeric with dashes.

    Examples:
        >>> validate_sku_format("ABC-123-DEF")
        True
        >>> validate_sku_format("abc 123")
        False
    """
    return bool(re.match(r"^[A-Z0-9]+(-[A-Z0-9]+)*$", sku))
