"""Tests for product models and API."""

from uuid import uuid4

import pytest

from models.product import Product, ProductCategory, ProductDimensions


class TestProductModel:
    """Unit tests for the Product dataclass."""

    def test_create_product(self):
        dims = ProductDimensions(length=10, width=5, height=3, weight=0.5)
        product = Product(
            sku="TEST-001",
            name="Test Widget",
            category=ProductCategory.GENERAL,
            dimensions=dims,
        )
        assert product.sku == "TEST-001"
        assert product.is_active is True
        assert product.min_stock_level == 0

    def test_product_deactivate(self):
        dims = ProductDimensions(length=10, width=5, height=3, weight=0.5)
        product = Product(
            sku="TEST-002",
            name="Another Widget",
            category=ProductCategory.GENERAL,
            dimensions=dims,
        )
        product.deactivate()
        assert product.is_active is False
        assert product.updated_at is not None

    def test_invalid_stock_levels(self):
        dims = ProductDimensions(length=10, width=5, height=3, weight=0.5)
        with pytest.raises(ValueError):
            Product(
                sku="TEST-003",
                name="Bad Widget",
                category=ProductCategory.GENERAL,
                dimensions=dims,
                min_stock_level=-1,
            )

    def test_requires_special_handling(self):
        dims = ProductDimensions(length=10, width=5, height=3, weight=0.5)
        fragile = Product(
            sku="FRAG-001", name="Glass Vase",
            category=ProductCategory.FRAGILE, dimensions=dims,
        )
        general = Product(
            sku="GEN-001", name="Rubber Ball",
            category=ProductCategory.GENERAL, dimensions=dims,
        )
        assert fragile.requires_special_handling() is True
        assert general.requires_special_handling() is False

    def test_dimensions_volume(self):
        dims = ProductDimensions(length=10, width=5, height=3, weight=0.5)
        assert dims.volume == 150.0

    def test_dimensions_fits_in(self):
        dims = ProductDimensions(length=10, width=5, height=3, weight=0.5)
        assert dims.fits_in(20, 20, 20) is True
        assert dims.fits_in(4, 4, 4) is False

    def test_barcode_validation(self):
        dims = ProductDimensions(length=10, width=5, height=3, weight=0.5)
        product = Product(
            sku="BC-001", name="Barcoded Item",
            category=ProductCategory.GENERAL,
            dimensions=dims, barcode="1234567890123",
        )
        assert product.validate_barcode() is True

        product.barcode = "short"
        assert product.validate_barcode() is False
