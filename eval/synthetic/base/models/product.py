"""Product domain models."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4


class ProductCategory(enum.Enum):
    """Classification of products by storage requirements."""

    GENERAL = "general"
    FRAGILE = "fragile"
    HAZARDOUS = "hazardous"
    PERISHABLE = "perishable"
    OVERSIZED = "oversized"


@dataclass
class ProductDimensions:
    """Physical dimensions in centimeters and weight in kilograms."""

    length: float
    width: float
    height: float
    weight: float

    @property
    def volume(self) -> float:
        return self.length * self.width * self.height

    def fits_in(self, max_length: float, max_width: float, max_height: float) -> bool:
        """Check if the product fits within given constraints."""
        dims = sorted([self.length, self.width, self.height])
        limits = sorted([max_length, max_width, max_height])
        return all(d <= l for d, l in zip(dims, limits))


@dataclass
class Product:
    """A product that can be stored in the warehouse."""

    sku: str
    name: str
    category: ProductCategory
    dimensions: ProductDimensions
    id: UUID = field(default_factory=uuid4)
    barcode: Optional[str] = None
    min_stock_level: int = 0
    max_stock_level: int = 10000
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
    is_active: bool = True

    def __post_init__(self):
        if self.min_stock_level < 0:
            raise ValueError("min_stock_level cannot be negative")
        if self.max_stock_level < self.min_stock_level:
            raise ValueError("max_stock_level must be >= min_stock_level")

    def deactivate(self) -> None:
        """Mark product as inactive."""
        self.is_active = False
        self.updated_at = datetime.utcnow()

    def update_stock_limits(self, min_level: int, max_level: int) -> None:
        """Update the min/max stock thresholds for reorder alerts."""
        if min_level < 0 or max_level < min_level:
            raise ValueError("Invalid stock level range")
        self.min_stock_level = min_level
        self.max_stock_level = max_level
        self.updated_at = datetime.utcnow()

    def requires_special_handling(self) -> bool:
        """Check if product needs special storage or handling."""
        return self.category in (
            ProductCategory.FRAGILE,
            ProductCategory.HAZARDOUS,
            ProductCategory.PERISHABLE,
        )

    def validate_barcode(self) -> bool:
        """Basic barcode format validation."""
        if self.barcode is None:
            return True
        # EAN-13 check
        if len(self.barcode) == 13 and self.barcode.isdigit():
            return True
        # UPC-A check
        if len(self.barcode) == 12 and self.barcode.isdigit():
            return True
        return False
