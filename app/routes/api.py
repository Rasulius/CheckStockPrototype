from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..db import get_session
from ..errors import validation_error
from ..models import Product
from ..schemas import (
    LineIn,
    ReceiptIn,
    RenameIn,
    ShipmentIn,
    SplitIn,
    TransferIn,
    TransitionIn,
    WarehouseIn,
)
from ..serializers import (
    history,
    in_transit,
    movement_dict,
    product_dict,
    product_locations,
    receipt_dict,
    shipment_dict,
    stock_matrix,
    transfer_dict,
    warehouse_dict,
)
from ..services import receipts, shipments, transfers, warehouses

router = APIRouter(prefix="/api/v1", tags=["api"])


def _lines(lines: list[LineIn]) -> list[dict[str, Any]]:
    return [{"product_id": line.product_id, "qty": line.qty} for line in lines]


@router.get("/stock")
def get_stock(session: Session = Depends(get_session)) -> dict[str, Any]:
    visible, rows = stock_matrix(session)
    transit = in_transit(session)
    return {
        "warehouses": [warehouse_dict(warehouse) for warehouse in visible],
        "products": [
            {
                **product_dict(row["product"]),
                "balances": [
                    {"warehouse_id": warehouse.id, **row["cells"][warehouse.id]}
                    for warehouse in visible
                ],
                "totals": row["totals"],
            }
            for row in rows
        ],
        "in_transit": {
            "transfers": [transfer_dict(item) for item in transit["transfers"]],
            "fbo": [shipment_dict(item) for item in transit["fbo"]],
            "fbs": [shipment_dict(item) for item in transit["fbs"]],
        },
    }


@router.get("/history")
def get_history(
    sku: str | None = None,
    warehouse_id: int | None = None,
    op_type: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    items = history(session, sku=sku, warehouse_id=warehouse_id, op_type=op_type, limit=limit)
    return {"count": len(items), "items": [movement_dict(item) for item in items]}


@router.get("/products")
def list_products(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    products = session.execute(select(Product).order_by(Product.sku)).scalars()
    return [product_dict(product) for product in products]


@router.get("/products/{sku}")
def get_product(sku: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    product = session.scalar(select(Product).where(Product.sku == sku))
    if product is None:
        raise validation_error(f"Артикул {sku} не найден.")
    return {
        "product": product_dict(product),
        "locations": product_locations(session, product.id),
        "history": [movement_dict(item) for item in history(session, sku=sku, limit=50)],
    }


@router.get("/warehouses")
def get_warehouses(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [warehouse_dict(item) for item in warehouses.list_warehouses(session)]


@router.post("/warehouses")
def add_warehouse(payload: WarehouseIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    return warehouse_dict(warehouses.create_warehouse(session, payload.name))


@router.post("/warehouses/{warehouse_id}/rename")
def rename_warehouse(
    warehouse_id: int, payload: RenameIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    return warehouse_dict(warehouses.rename_warehouse(session, warehouse_id, payload.name))


@router.post("/warehouses/{warehouse_id}/archive")
def archive_warehouse(warehouse_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    warehouse, stats = warehouses.archive_warehouse(session, warehouse_id)
    return {**warehouse_dict(warehouse), "stats": stats}


@router.post("/receipts")
def add_receipt(payload: ReceiptIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    receipt = receipts.create_receipt(
        session,
        warehouse_id=payload.warehouse_id,
        lines=_lines(payload.lines),
        idempotency_key=payload.idempotency_key,
        comment=payload.comment,
    )
    return receipt_dict(receipt)


@router.post("/transfers")
def add_transfer(payload: TransferIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    transfer = transfers.create_transfer(
        session,
        from_warehouse_id=payload.from_warehouse_id,
        to_warehouse_id=payload.to_warehouse_id,
        lines=_lines(payload.lines),
        idempotency_key=payload.idempotency_key,
    )
    return transfer_dict(transfer)


@router.post("/transfers/{transfer_id}/receive")
def receive_transfer(
    transfer_id: int,
    payload: TransitionIn | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return transfer_dict(transfers.receive_transfer(session, transfer_id))


@router.post("/fbs")
def add_fbs(payload: ShipmentIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    shipment = shipments.create_fbs(
        session,
        warehouse_id=payload.warehouse_id,
        lines=_lines(payload.lines),
        idempotency_key=payload.idempotency_key,
        external_ref=payload.external_ref,
    )
    return shipment_dict(shipment)


@router.post("/fbs/split")
def split_fbs(payload: SplitIn, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    result = shipments.split_fbs(
        session,
        lines=_lines(payload.lines),
        idempotency_key=payload.idempotency_key,
    )
    return [shipment_dict(item) for item in result]


@router.post("/fbs/{shipment_id}/ship")
def ship_fbs(
    shipment_id: int,
    payload: TransitionIn | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return shipment_dict(shipments.ship_fbs(session, shipment_id))


@router.post("/fbs/{shipment_id}/cancel")
def cancel_fbs(
    shipment_id: int,
    payload: TransitionIn | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return shipment_dict(shipments.cancel_fbs(session, shipment_id))


@router.post("/fbo")
def add_fbo(payload: ShipmentIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    shipment = shipments.create_fbo(
        session,
        warehouse_id=payload.warehouse_id,
        lines=_lines(payload.lines),
        idempotency_key=payload.idempotency_key,
        external_ref=payload.external_ref,
    )
    return shipment_dict(shipment)


@router.post("/fbo/{shipment_id}/accept")
def accept_fbo(
    shipment_id: int,
    payload: TransitionIn | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return shipment_dict(shipments.accept_fbo(session, shipment_id))


@router.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}
