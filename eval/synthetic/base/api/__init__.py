"""API layer — FastAPI route definitions."""

from api.products import router as products_router
from api.inventory import router as inventory_router

__all__ = ["products_router", "inventory_router"]
