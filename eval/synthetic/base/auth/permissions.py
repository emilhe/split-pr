"""Role-based permission checks."""

from __future__ import annotations

import enum
from typing import Optional


class Permission(enum.Enum):
    """Permissions for inventory operations."""

    READ_PRODUCTS = "read:products"
    WRITE_PRODUCTS = "write:products"
    READ_INVENTORY = "read:inventory"
    WRITE_INVENTORY = "write:inventory"
    MANAGE_WAREHOUSE = "manage:warehouse"
    ADMIN = "admin"


# Role to permissions mapping
ROLE_PERMISSIONS: dict[str, set[Permission]] = {
    "viewer": {
        Permission.READ_PRODUCTS,
        Permission.READ_INVENTORY,
    },
    "operator": {
        Permission.READ_PRODUCTS,
        Permission.READ_INVENTORY,
        Permission.WRITE_INVENTORY,
    },
    "manager": {
        Permission.READ_PRODUCTS,
        Permission.WRITE_PRODUCTS,
        Permission.READ_INVENTORY,
        Permission.WRITE_INVENTORY,
        Permission.MANAGE_WAREHOUSE,
    },
    "admin": {
        Permission.READ_PRODUCTS,
        Permission.WRITE_PRODUCTS,
        Permission.READ_INVENTORY,
        Permission.WRITE_INVENTORY,
        Permission.MANAGE_WAREHOUSE,
        Permission.ADMIN,
    },
}


def get_permissions_for_roles(roles: list[str]) -> set[Permission]:
    """Compute the union of permissions for a list of roles."""
    permissions: set[Permission] = set()
    for role in roles:
        role_perms = ROLE_PERMISSIONS.get(role, set())
        permissions |= role_perms
    return permissions


def check_permission(roles: list[str], required: Permission) -> bool:
    """Check if any of the given roles grant the required permission.

    Args:
        roles: List of role identifiers from the JWT token.
        required: The permission to check.

    Returns:
        True if the permission is granted.
    """
    permissions = get_permissions_for_roles(roles)
    return required in permissions


def require_permission(roles: list[str], required: Permission) -> None:
    """Assert that the required permission is present.

    Raises:
        PermissionError: If the permission is not granted.
    """
    if not check_permission(roles, required):
        raise PermissionError(
            f"Permission denied: {required.value} not granted by roles {roles}"
        )


def list_roles_with_permission(permission: Permission) -> list[str]:
    """Find all roles that grant a specific permission."""
    return [
        role
        for role, perms in ROLE_PERMISSIONS.items()
        if permission in perms
    ]
