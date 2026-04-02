"""Tests for inventory models."""

from uuid import uuid4

import pytest

from models.inventory import InventoryItem, InventoryMovement, StockLevel


class TestInventoryItem:
    """Unit tests for InventoryItem."""

    def test_adjust_quantity_positive(self):
        item = InventoryItem(
            product_id=uuid4(), zone_id=uuid4(), quantity=100
        )
        item.adjust_quantity(50)
        assert item.quantity == 150

    def test_adjust_quantity_negative(self):
        item = InventoryItem(
            product_id=uuid4(), zone_id=uuid4(), quantity=100
        )
        item.adjust_quantity(-30)
        assert item.quantity == 70

    def test_adjust_quantity_below_zero_raises(self):
        item = InventoryItem(
            product_id=uuid4(), zone_id=uuid4(), quantity=10
        )
        with pytest.raises(ValueError, match="negative quantity"):
            item.adjust_quantity(-20)

    def test_classify_stock_level_normal(self):
        item = InventoryItem(
            product_id=uuid4(), zone_id=uuid4(), quantity=50
        )
        assert item.classify_stock_level(10, 100) == StockLevel.NORMAL

    def test_classify_stock_level_out_of_stock(self):
        item = InventoryItem(
            product_id=uuid4(), zone_id=uuid4(), quantity=0
        )
        assert item.classify_stock_level(10, 100) == StockLevel.OUT_OF_STOCK

    def test_classify_stock_level_low(self):
        item = InventoryItem(
            product_id=uuid4(), zone_id=uuid4(), quantity=8
        )
        assert item.classify_stock_level(10, 100) == StockLevel.LOW

    def test_classify_stock_level_overstocked(self):
        item = InventoryItem(
            product_id=uuid4(), zone_id=uuid4(), quantity=200
        )
        assert item.classify_stock_level(10, 100) == StockLevel.OVERSTOCKED

    def test_record_count_returns_discrepancy(self):
        item = InventoryItem(
            product_id=uuid4(), zone_id=uuid4(), quantity=100
        )
        discrepancy = item.record_count(95)
        assert discrepancy == -5
        assert item.quantity == 95
        assert item.last_counted_at is not None


class TestInventoryMovement:
    """Unit tests for InventoryMovement."""

    def test_inbound_movement(self):
        movement = InventoryMovement(
            product_id=uuid4(),
            quantity=50,
            source_zone_id=None,
            destination_zone_id=uuid4(),
        )
        assert movement.is_inbound is True
        assert movement.is_outbound is False

    def test_outbound_movement(self):
        movement = InventoryMovement(
            product_id=uuid4(),
            quantity=25,
            source_zone_id=uuid4(),
            destination_zone_id=None,
        )
        assert movement.is_outbound is True

    def test_transfer_movement(self):
        movement = InventoryMovement(
            product_id=uuid4(),
            quantity=10,
            source_zone_id=uuid4(),
            destination_zone_id=uuid4(),
        )
        assert movement.is_transfer is True

    def test_invalid_movement_no_zones(self):
        with pytest.raises(ValueError, match="source or destination"):
            InventoryMovement(
                product_id=uuid4(),
                quantity=10,
                source_zone_id=None,
                destination_zone_id=None,
            )

    def test_invalid_movement_zero_quantity(self):
        with pytest.raises(ValueError, match="positive"):
            InventoryMovement(
                product_id=uuid4(),
                quantity=0,
                source_zone_id=uuid4(),
                destination_zone_id=uuid4(),
            )
