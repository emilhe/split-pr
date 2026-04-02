"""Tests for authentication and authorization."""

import pytest

from auth.permissions import (
    Permission,
    check_permission,
    get_permissions_for_roles,
    require_permission,
    list_roles_with_permission,
)
from auth.tokens import create_access_token, verify_token, extract_roles


class TestTokens:
    """Tests for JWT token operations."""

    def test_create_and_verify_token(self):
        token = create_access_token(
            subject="user@example.com",
            roles=["operator"],
        )
        payload = verify_token(token)
        assert payload is not None
        assert payload["sub"] == "user@example.com"
        assert "operator" in payload["roles"]

    def test_verify_invalid_token(self):
        result = verify_token("invalid-token-string")
        assert result is None

    def test_extract_roles(self):
        token = create_access_token(
            subject="admin@example.com",
            roles=["admin", "manager"],
        )
        roles = extract_roles(token)
        assert "admin" in roles
        assert "manager" in roles

    def test_extract_roles_invalid_token(self):
        roles = extract_roles("bad-token")
        assert roles == []


class TestPermissions:
    """Tests for role-based permissions."""

    def test_viewer_can_read(self):
        assert check_permission(["viewer"], Permission.READ_PRODUCTS) is True
        assert check_permission(["viewer"], Permission.READ_INVENTORY) is True

    def test_viewer_cannot_write(self):
        assert check_permission(["viewer"], Permission.WRITE_PRODUCTS) is False
        assert check_permission(["viewer"], Permission.WRITE_INVENTORY) is False

    def test_operator_can_write_inventory(self):
        assert check_permission(["operator"], Permission.WRITE_INVENTORY) is True
        assert check_permission(["operator"], Permission.WRITE_PRODUCTS) is False

    def test_admin_has_all_permissions(self):
        for perm in Permission:
            assert check_permission(["admin"], perm) is True

    def test_multiple_roles_union(self):
        perms = get_permissions_for_roles(["viewer", "operator"])
        assert Permission.READ_PRODUCTS in perms
        assert Permission.WRITE_INVENTORY in perms

    def test_require_permission_raises(self):
        with pytest.raises(PermissionError):
            require_permission(["viewer"], Permission.ADMIN)

    def test_list_roles_with_permission(self):
        roles = list_roles_with_permission(Permission.ADMIN)
        assert "admin" in roles
        assert "viewer" not in roles
