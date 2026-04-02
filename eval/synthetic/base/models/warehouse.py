"""Warehouse and storage zone models."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from models.product import ProductCategory


class ZoneType(enum.Enum):
    """Storage zone classifications."""

    STANDARD = "standard"
    COLD_STORAGE = "cold_storage"
    HAZMAT = "hazmat"
    HIGH_VALUE = "high_value"
    BULK = "bulk"


CATEGORY_ZONE_MAPPING: dict[ProductCategory, list[ZoneType]] = {
    ProductCategory.GENERAL: [ZoneType.STANDARD, ZoneType.BULK],
    ProductCategory.FRAGILE: [ZoneType.HIGH_VALUE],
    ProductCategory.HAZARDOUS: [ZoneType.HAZMAT],
    ProductCategory.PERISHABLE: [ZoneType.COLD_STORAGE],
    ProductCategory.OVERSIZED: [ZoneType.BULK],
}


@dataclass
class StorageZone:
    """A designated area within a warehouse for specific product types."""

    name: str
    zone_type: ZoneType
    capacity: int
    warehouse_id: UUID
    id: UUID = field(default_factory=uuid4)
    current_occupancy: int = 0
    is_active: bool = True

    @property
    def available_capacity(self) -> int:
        return self.capacity - self.current_occupancy

    @property
    def utilization_pct(self) -> float:
        if self.capacity == 0:
            return 0.0
        return (self.current_occupancy / self.capacity) * 100

    def can_accept(self, category: ProductCategory) -> bool:
        """Check if this zone can store products of the given category."""
        allowed_zones = CATEGORY_ZONE_MAPPING.get(category, [])
        return self.zone_type in allowed_zones and self.available_capacity > 0

    def allocate(self, quantity: int) -> None:
        """Reserve space in this zone."""
        if quantity > self.available_capacity:
            raise ValueError(
                f"Insufficient capacity: need {quantity}, have {self.available_capacity}"
            )
        self.current_occupancy += quantity

    def release(self, quantity: int) -> None:
        """Free space in this zone."""
        if quantity > self.current_occupancy:
            raise ValueError("Cannot release more than current occupancy")
        self.current_occupancy -= quantity


@dataclass
class Warehouse:
    """A physical warehouse facility."""

    name: str
    location: str
    id: UUID = field(default_factory=uuid4)
    zones: list[StorageZone] = field(default_factory=list)
    is_operational: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    manager_email: Optional[str] = None

    @property
    def total_capacity(self) -> int:
        return sum(z.capacity for z in self.zones)

    @property
    def total_occupancy(self) -> int:
        return sum(z.current_occupancy for z in self.zones)

    def find_zone_for_product(self, category: ProductCategory) -> Optional[StorageZone]:
        """Find the best available zone for a product category."""
        candidates = [z for z in self.zones if z.can_accept(category)]
        if not candidates:
            return None
        # Prefer zone with most available capacity
        return max(candidates, key=lambda z: z.available_capacity)

    def get_zone_by_type(self, zone_type: ZoneType) -> list[StorageZone]:
        """Get all zones of a specific type."""
        return [z for z in self.zones if z.zone_type == zone_type]

    def shutdown(self) -> None:
        """Mark warehouse as non-operational."""
        self.is_operational = False
        for zone in self.zones:
            zone.is_active = False
