"""Inventory tracking models."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


class StockLevel(enum.Enum):
    """Categorization of stock levels for alerting."""

    OUT_OF_STOCK = "out_of_stock"
    CRITICAL = "critical"
    LOW = "low"
    NORMAL = "normal"
    OVERSTOCKED = "overstocked"


@dataclass
class InventoryItem:
    """Tracks the quantity of a specific product in a specific zone."""

    product_id: UUID
    zone_id: UUID
    quantity: int
    id: UUID = field(default_factory=uuid4)
    lot_number: Optional[str] = None
    expiry_date: Optional[datetime] = None
    last_counted_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.utcnow)

    def adjust_quantity(self, delta: int) -> None:
        """Adjust quantity by delta (positive for additions, negative for removals)."""
        new_qty = self.quantity + delta
        if new_qty < 0:
            raise ValueError(
                f"Adjustment would result in negative quantity: {self.quantity} + {delta}"
            )
        self.quantity = new_qty

    def classify_stock_level(self, min_level: int, max_level: int) -> StockLevel:
        """Classify current stock level relative to thresholds."""
        if self.quantity == 0:
            return StockLevel.OUT_OF_STOCK
        if self.quantity < min_level * 0.5:
            return StockLevel.CRITICAL
        if self.quantity < min_level:
            return StockLevel.LOW
        if self.quantity > max_level:
            return StockLevel.OVERSTOCKED
        return StockLevel.NORMAL

    def is_expired(self) -> bool:
        """Check if the item has passed its expiry date."""
        if self.expiry_date is None:
            return False
        return datetime.utcnow() > self.expiry_date

    def record_count(self, actual_quantity: int) -> int:
        """Record a physical count and return the discrepancy."""
        discrepancy = actual_quantity - self.quantity
        self.quantity = actual_quantity
        self.last_counted_at = datetime.utcnow()
        return discrepancy


@dataclass
class InventoryMovement:
    """Records a transfer of inventory between zones or in/out of warehouse."""

    product_id: UUID
    quantity: int
    source_zone_id: Optional[UUID]
    destination_zone_id: Optional[UUID]
    id: UUID = field(default_factory=uuid4)
    reason: str = ""
    performed_by: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self):
        if self.source_zone_id is None and self.destination_zone_id is None:
            raise ValueError("At least one of source or destination must be specified")
        if self.quantity <= 0:
            raise ValueError("Movement quantity must be positive")

    @property
    def is_inbound(self) -> bool:
        return self.source_zone_id is None

    @property
    def is_outbound(self) -> bool:
        return self.destination_zone_id is None

    @property
    def is_transfer(self) -> bool:
        return self.source_zone_id is not None and self.destination_zone_id is not None
