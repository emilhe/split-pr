"""Product management API endpoints."""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth.permissions import Permission, require_permission
from auth.tokens import verify_token
from database.connection import session_scope
from database.queries import ProductRepository
from utils.formatting import format_sku, build_log_prefix
from utils.validation import validate_sku_format

router = APIRouter(prefix="/products", tags=["products"])


class ProductCreate(BaseModel):
    """Request body for creating a product."""

    sku: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=200)
    category: str = Field(default="general")
    barcode: Optional[str] = None
    min_stock_level: int = Field(default=0, ge=0)
    max_stock_level: int = Field(default=10000, ge=0)


class ProductResponse(BaseModel):
    """Response body for product data."""

    id: str
    sku: str
    name: str
    category: str
    is_active: bool
    min_stock_level: int
    max_stock_level: int


class ProductListResponse(BaseModel):
    """Paginated product list response."""

    items: list[ProductResponse]
    total: int
    limit: int
    offset: int


@router.get("/", response_model=ProductListResponse)
async def list_products(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List all active products with pagination."""
    with session_scope() as session:
        repo = ProductRepository(session)
        items = repo.list_active(limit=limit, offset=offset)
        total = repo.count_active()

    return ProductListResponse(
        items=[ProductResponse(**item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product(product_id: UUID):
    """Get a single product by ID."""
    with session_scope() as session:
        repo = ProductRepository(session)
        product = repo.get_by_id(product_id)

    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return ProductResponse(**product)


@router.post("/", response_model=ProductResponse, status_code=201)
async def create_product(body: ProductCreate):
    """Create a new product."""
    prefix = build_log_prefix("api", "create_product")
    normalized_sku = format_sku(body.sku)

    if not validate_sku_format(normalized_sku):
        raise HTTPException(status_code=400, detail="Invalid SKU format")

    with session_scope() as session:
        repo = ProductRepository(session)
        existing = repo.get_by_sku(normalized_sku)
        if existing:
            raise HTTPException(status_code=409, detail="SKU already exists")

    return ProductResponse(
        id="generated-uuid",
        sku=normalized_sku,
        name=body.name,
        category=body.category,
        is_active=True,
        min_stock_level=body.min_stock_level,
        max_stock_level=body.max_stock_level,
    )


@router.get("/search/", response_model=list[ProductResponse])
async def search_products(q: str = Query(..., min_length=1)):
    """Search products by name."""
    with session_scope() as session:
        repo = ProductRepository(session)
        results = repo.search_by_name(q)
    return [ProductResponse(**r) for r in results]
