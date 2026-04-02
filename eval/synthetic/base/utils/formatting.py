"""String formatting utilities used across the service."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional


def format_sku(raw_sku: str) -> str:
    """Normalize a SKU string to uppercase with dashes.

    Examples:
        >>> format_sku("abc 123 def")
        'ABC-123-DEF'
        >>> format_sku("  widget--42  ")
        'WIDGET-42'
    """
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", raw_sku.strip())
    cleaned = cleaned.strip("-")
    # Collapse multiple dashes
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned.upper()


def format_quantity(value: int, unit: str = "units") -> str:
    """Format a quantity with thousands separators.

    Examples:
        >>> format_quantity(1500)
        '1,500 units'
        >>> format_quantity(42, "kg")
        '42 kg'
    """
    formatted = f"{value:,}"
    return f"{formatted} {unit}"


def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug.

    Examples:
        >>> slugify("Hello World!")
        'hello-world'
        >>> slugify("Über Cool Product")
        'uber-cool-product'
    """
    # Normalize unicode characters
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return re.sub(r"-{2,}", "-", text)


def truncate(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """Truncate text to max_length, appending suffix if truncated."""
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


def format_warehouse_code(name: str, location: str) -> str:
    """Generate a warehouse code from name and location.

    Examples:
        >>> format_warehouse_code("Main Hub", "New York")
        'MH-NY'
    """
    name_parts = name.upper().split()
    loc_parts = location.upper().split()
    name_initials = "".join(p[0] for p in name_parts if p)
    loc_initials = "".join(p[0] for p in loc_parts if p)
    return f"{name_initials}-{loc_initials}"


def build_log_prefix(module: str, operation: str, entity_id: Optional[str] = None) -> str:
    """Build a structured log prefix for consistent logging.

    Examples:
        >>> build_log_prefix("inventory", "adjust", "abc-123")
        '[inventory:adjust:abc-123]'
    """
    parts = [module, operation]
    if entity_id:
        parts.append(entity_id)
    return f"[{':'.join(parts)}]"
