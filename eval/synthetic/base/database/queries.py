"""Repository classes for database operations."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from utils.formatting import build_log_prefix


class ProductRepository:
    """Data access layer for products."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, product_id: UUID) -> Optional[dict]:
        """Fetch a product by its primary key."""
        result = self.session.execute(
            text("SELECT * FROM products WHERE id = :id"),
            {"id": str(product_id)},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None

    def get_by_sku(self, sku: str) -> Optional[dict]:
        """Fetch a product by its SKU."""
        result = self.session.execute(
            text("SELECT * FROM products WHERE sku = :sku"),
            {"sku": sku},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None

    def list_active(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """List all active products with pagination."""
        result = self.session.execute(
            text(
                "SELECT * FROM products WHERE is_active = true "
                "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset},
        )
        return [dict(row._mapping) for row in result]

    def search_by_name(self, query: str) -> list[dict]:
        """Search products by name using ILIKE."""
        result = self.session.execute(
            text("SELECT * FROM products WHERE name ILIKE :query ORDER BY name"),
            {"query": f"%{query}%"},
        )
        return [dict(row._mapping) for row in result]

    def count_active(self) -> int:
        """Count all active products."""
        result = self.session.execute(
            text("SELECT COUNT(*) FROM products WHERE is_active = true")
        )
        return result.scalar() or 0


class InventoryRepository:
    """Data access layer for inventory items."""

    def __init__(self, session: Session):
        self.session = session

    def get_stock_for_product(self, product_id: UUID) -> list[dict]:
        """Get all inventory entries for a product across zones."""
        prefix = build_log_prefix("inventory", "get_stock", str(product_id))
        result = self.session.execute(
            text(
                "SELECT i.*, z.name as zone_name "
                "FROM inventory_items i "
                "JOIN storage_zones z ON i.zone_id = z.id "
                "WHERE i.product_id = :pid"
            ),
            {"pid": str(product_id)},
        )
        return [dict(row._mapping) for row in result]

    def get_total_quantity(self, product_id: UUID) -> int:
        """Get total quantity of a product across all zones."""
        result = self.session.execute(
            text(
                "SELECT COALESCE(SUM(quantity), 0) "
                "FROM inventory_items WHERE product_id = :pid"
            ),
            {"pid": str(product_id)},
        )
        return result.scalar() or 0

    def get_low_stock_items(self, threshold: int = 10) -> list[dict]:
        """Find items with stock below threshold."""
        result = self.session.execute(
            text(
                "SELECT i.*, p.name as product_name, p.sku "
                "FROM inventory_items i "
                "JOIN products p ON i.product_id = p.id "
                "WHERE i.quantity < :threshold AND p.is_active = true "
                "ORDER BY i.quantity ASC"
            ),
            {"threshold": threshold},
        )
        return [dict(row._mapping) for row in result]

    def record_movement(
        self,
        product_id: UUID,
        quantity: int,
        source_zone_id: Optional[UUID],
        dest_zone_id: Optional[UUID],
        reason: str = "",
    ) -> dict:
        """Record an inventory movement and update quantities."""
        prefix = build_log_prefix("inventory", "movement")

        if source_zone_id:
            self.session.execute(
                text(
                    "UPDATE inventory_items SET quantity = quantity - :qty "
                    "WHERE product_id = :pid AND zone_id = :zid"
                ),
                {"qty": quantity, "pid": str(product_id), "zid": str(source_zone_id)},
            )

        if dest_zone_id:
            # Upsert: insert or update quantity
            self.session.execute(
                text(
                    "INSERT INTO inventory_items (product_id, zone_id, quantity) "
                    "VALUES (:pid, :zid, :qty) "
                    "ON CONFLICT (product_id, zone_id) "
                    "DO UPDATE SET quantity = inventory_items.quantity + :qty"
                ),
                {"pid": str(product_id), "zid": str(dest_zone_id), "qty": quantity},
            )

        # Record the movement
        self.session.execute(
            text(
                "INSERT INTO inventory_movements "
                "(product_id, quantity, source_zone_id, destination_zone_id, reason) "
                "VALUES (:pid, :qty, :src, :dst, :reason)"
            ),
            {
                "pid": str(product_id),
                "qty": quantity,
                "src": str(source_zone_id) if source_zone_id else None,
                "dst": str(dest_zone_id) if dest_zone_id else None,
                "reason": reason,
            },
        )

        return {"product_id": str(product_id), "quantity": quantity, "reason": reason}
