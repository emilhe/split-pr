"""Tests for utility functions."""

import pytest

from utils.formatting import format_sku, format_quantity, slugify, truncate
from utils.validation import (
    validate_email,
    validate_uuid_string,
    validate_positive_int,
    validate_sku_format,
)


class TestFormatting:
    """Tests for string formatting utilities."""

    def test_format_sku_basic(self):
        assert format_sku("abc 123 def") == "ABC-123-DEF"

    def test_format_sku_strips_whitespace(self):
        assert format_sku("  widget--42  ") == "WIDGET-42"

    def test_format_sku_special_chars(self):
        assert format_sku("hello.world/123") == "HELLO-WORLD-123"

    def test_format_quantity_default_unit(self):
        assert format_quantity(1500) == "1,500 units"

    def test_format_quantity_custom_unit(self):
        assert format_quantity(42, "kg") == "42 kg"

    def test_slugify_basic(self):
        assert slugify("Hello World!") == "hello-world"

    def test_slugify_unicode(self):
        assert slugify("Über Cool") == "uber-cool"

    def test_truncate_short_text(self):
        assert truncate("hello", max_length=10) == "hello"

    def test_truncate_long_text(self):
        result = truncate("a very long string", max_length=10)
        assert len(result) == 10
        assert result.endswith("...")


class TestValidation:
    """Tests for validation utilities."""

    def test_validate_email_valid(self):
        assert validate_email("user@example.com") is True
        assert validate_email("test.name+tag@domain.org") is True

    def test_validate_email_invalid(self):
        assert validate_email("not-an-email") is False
        assert validate_email("") is False
        assert validate_email("@missing-local.com") is False

    def test_validate_uuid_string_valid(self):
        assert validate_uuid_string("12345678-1234-5678-1234-567812345678") is True

    def test_validate_uuid_string_invalid(self):
        assert validate_uuid_string("not-a-uuid") is False
        assert validate_uuid_string("") is False

    def test_validate_positive_int(self):
        assert validate_positive_int(1) == 1
        assert validate_positive_int(999) == 999

    def test_validate_positive_int_invalid(self):
        with pytest.raises(ValueError):
            validate_positive_int(0)
        with pytest.raises(ValueError):
            validate_positive_int(-5)

    def test_validate_sku_format(self):
        assert validate_sku_format("ABC-123-DEF") is True
        assert validate_sku_format("WIDGET42") is True
        assert validate_sku_format("abc 123") is False
        assert validate_sku_format("") is False
