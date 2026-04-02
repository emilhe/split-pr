"""Inventory management API endpoints."""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth.permissions import Permission, require_permission
from database.connection import session_scope
from database.queries import InventoryRepository
from utils.formatting import format_quantity, build_log_prefix

router = APIRouter(prefix="/inventory", tags=["inventory"])


class StockResponse(BaseModel):
    """Stock level for a product in a zone."""

    product_id: str
    zone_id: str
    zone_name: str
    quantity: int
    formatted_quantity: str


class MovementRequest(BaseModel):
    """Request to move inventory between zones."""

    product_id: str
    quantity: int = Field(..., gt=0)
    source_zone_id: Optional[str] = None
    destination_zone_id: Optional[str] = None
    reason: str = ""


class MovementResponse(BaseModel):
    """Response after recording a movement."""

    product_id: str
    quantity: int
    reason: str
    status: str = "recorded"


@router.get("/stock/{product_id}", response_model=list[StockResponse])
async def get_stock(product_id: UUID):
    """Get stock levels for a product across all zones."""
    prefix = build_log_prefix("api", "get_stock", str(product_id))

    with session_scope() as session:
        repo = InventoryRepository(session)
        entries = repo.get_stock_for_product(product_id)

    if not entries:
        raise HTTPException(status_code=404, detail="No stock found for product")

    return [
        StockResponse(
            product_id=e["product_id"],
            zone_id=e["zone_id"],
            zone_name=e["zone_name"],
            quantity=e["quantity"],
            formatted_quantity=format_quantity(e["quantity"]),
        )
        for e in entries
    ]


@router.get("/low-stock", response_model=list[dict])
async def get_low_stock(threshold: int = Query(default=10, ge=1)):
    """Find items with stock below threshold."""
    with session_scope() as session:
        repo = InventoryRepository(session)
        items = repo.get_low_stock_items(threshold=threshold)
    return items


@router.post("/movement", response_model=MovementResponse)
async def record_movement(body: MovementRequest):
    """Record an inventory movement (inbound, outbound, or transfer)."""
    prefix = build_log_prefix("api", "movement")

    if body.source_zone_id is None and body.destination_zone_id is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of source or destination zone must be specified",
        )

    with session_scope() as session:
        repo = InventoryRepository(session)
        result = repo.record_movement(
            product_id=UUID(body.product_id),
            quantity=body.quantity,
            source_zone_id=UUID(body.source_zone_id) if body.source_zone_id else None,
            dest_zone_id=UUID(body.destination_zone_id) if body.destination_zone_id else None,
            reason=body.reason,
        )

    return MovementResponse(**result)
