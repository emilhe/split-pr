"""Tests for warehouse models."""

from uuid import uuid4

import pytest

from models.warehouse import Warehouse, StorageZone, ZoneType
from models.product import ProductCategory


class TestStorageZone:
    """Unit tests for StorageZone."""

    def test_available_capacity(self):
        zone = StorageZone(
            name="Zone A", zone_type=ZoneType.STANDARD,
            capacity=100, warehouse_id=uuid4(), current_occupancy=30,
        )
        assert zone.available_capacity == 70

    def test_utilization_percentage(self):
        zone = StorageZone(
            name="Zone B", zone_type=ZoneType.STANDARD,
            capacity=200, warehouse_id=uuid4(), current_occupancy=50,
        )
        assert zone.utilization_pct == 25.0

    def test_can_accept_matching_category(self):
        zone = StorageZone(
            name="Cold Zone", zone_type=ZoneType.COLD_STORAGE,
            capacity=100, warehouse_id=uuid4(),
        )
        assert zone.can_accept(ProductCategory.PERISHABLE) is True
        assert zone.can_accept(ProductCategory.GENERAL) is False

    def test_allocate_success(self):
        zone = StorageZone(
            name="Zone C", zone_type=ZoneType.STANDARD,
            capacity=100, warehouse_id=uuid4(),
        )
        zone.allocate(50)
        assert zone.current_occupancy == 50

    def test_allocate_overflow_raises(self):
        zone = StorageZone(
            name="Zone D", zone_type=ZoneType.STANDARD,
            capacity=10, warehouse_id=uuid4(),
        )
        with pytest.raises(ValueError, match="Insufficient capacity"):
            zone.allocate(20)

    def test_release(self):
        zone = StorageZone(
            name="Zone E", zone_type=ZoneType.STANDARD,
            capacity=100, warehouse_id=uuid4(), current_occupancy=50,
        )
        zone.release(20)
        assert zone.current_occupancy == 30


class TestWarehouse:
    """Unit tests for Warehouse."""

    def _make_warehouse(self) -> Warehouse:
        wh = Warehouse(name="Test Hub", location="Copenhagen")
        wh_id = wh.id
        wh.zones = [
            StorageZone(
                name="Standard A", zone_type=ZoneType.STANDARD,
                capacity=500, warehouse_id=wh_id,
            ),
            StorageZone(
                name="Cold Storage", zone_type=ZoneType.COLD_STORAGE,
                capacity=200, warehouse_id=wh_id,
            ),
            StorageZone(
                name="Hazmat", zone_type=ZoneType.HAZMAT,
                capacity=50, warehouse_id=wh_id,
            ),
        ]
        return wh

    def test_total_capacity(self):
        wh = self._make_warehouse()
        assert wh.total_capacity == 750

    def test_find_zone_for_product(self):
        wh = self._make_warehouse()
        zone = wh.find_zone_for_product(ProductCategory.PERISHABLE)
        assert zone is not None
        assert zone.zone_type == ZoneType.COLD_STORAGE

    def test_find_zone_none_available(self):
        wh = self._make_warehouse()
        # Fill all hazmat zones
        for z in wh.get_zone_by_type(ZoneType.HAZMAT):
            z.allocate(z.capacity)
        zone = wh.find_zone_for_product(ProductCategory.HAZARDOUS)
        assert zone is None

    def test_shutdown(self):
        wh = self._make_warehouse()
        wh.shutdown()
        assert wh.is_operational is False
        assert all(not z.is_active for z in wh.zones)
