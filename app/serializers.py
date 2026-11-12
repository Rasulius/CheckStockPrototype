from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from .models import (
    Product,
    Receipt,
    ReceiptLine,
    Shipment,
    ShipmentLine,
    StockBalance,
    StockMovement,
    Transfer,
    TransferLine,
    Warehouse,
)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def warehouse_dict(warehouse: Warehouse) -> dict[str, Any]:
    return {
        "id": warehouse.id,
        "name": warehouse.name,
        "is_archived": warehouse.is_archived,
        "archived_at": iso(warehouse.archived_at),
    }


def product_dict(product: Product) -> dict[str, Any]:
    return {"id": product.id, "sku": product.sku, "name": product.name}


def line_dict(line: ReceiptLine | TransferLine | ShipmentLine) -> dict[str, Any]:
    return {
        "product_id": line.product_id,
        "sku": line.product.sku if line.product else None,
        "name": line.product.name if line.product else None,
        "qty": line.qty,
    }


def receipt_dict(receipt: Receipt) -> dict[str, Any]:
    return {
        "id": receipt.id,
        "warehouse_id": receipt.warehouse_id,
        "warehouse": receipt.warehouse.name if receipt.warehouse else None,
        "comment": receipt.comment,
        "created_at": iso(receipt.created_at),
        "idempotency_key": str(receipt.idempotency_key),
        "lines": [line_dict(line) for line in receipt.lines],
    }


def transfer_dict(transfer: Transfer) -> dict[str, Any]:
    return {
        "id": transfer.id,
        "from_warehouse_id": transfer.from_warehouse_id,
        "from_warehouse": transfer.from_warehouse.name if transfer.from_warehouse else None,
        "to_warehouse_id": transfer.to_warehouse_id,
        "to_warehouse": transfer.to_warehouse.name if transfer.to_warehouse else None,
        "status": transfer.status,
        "created_at": iso(transfer.created_at),
        "received_at": iso(transfer.received_at),
        "idempotency_key": str(transfer.idempotency_key),
        "lines": [line_dict(line) for line in transfer.lines],
    }


def shipment_dict(shipment: Shipment) -> dict[str, Any]:
    return {
        "id": shipment.id,
        "kind": shipment.kind,
        "warehouse_id": shipment.warehouse_id,
        "warehouse": shipment.warehouse.name if shipment.warehouse else None,
        "status": shipment.status,
        "external_ref": shipment.external_ref,
        "created_at": iso(shipment.created_at),
        "shipped_at": iso(shipment.shipped_at),
        "closed_at": iso(shipment.closed_at),
        "idempotency_key": str(shipment.idempotency_key),
        "lines": [line_dict(line) for line in shipment.lines],
    }


def movement_dict(movement: StockMovement) -> dict[str, Any]:
    return {
        "id": movement.id,
        "created_at": iso(movement.created_at),
        "op_type": movement.op_type,
        "product_id": movement.product_id,
        "sku": movement.product.sku if movement.product else None,
        "product": movement.product.name if movement.product else None,
        "qty": movement.qty,
        "from_place": movement.from_place,
        "from_warehouse_id": movement.from_warehouse_id,
        "from_warehouse": movement.from_warehouse.name if movement.from_warehouse else None,
        "to_place": movement.to_place,
        "to_warehouse_id": movement.to_warehouse_id,
        "to_warehouse": movement.to_warehouse.name if movement.to_warehouse else None,
        "doc_type": movement.doc_type,
        "doc_id": movement.doc_id,
    }


def open_warehouse_ids(session: Session) -> set[int]:
    ids: set[int] = set(
        session.execute(
            select(Transfer.from_warehouse_id).where(Transfer.status == "in_transit")
        ).scalars()
    )
    ids.update(
        session.execute(
            select(Transfer.to_warehouse_id).where(Transfer.status == "in_transit")
        ).scalars()
    )
    ids.update(
        session.execute(
            select(Shipment.warehouse_id).where(Shipment.status.in_(["reserved", "shipped"]))
        ).scalars()
    )
    return ids


def stock_matrix(session: Session) -> tuple[list[Warehouse], list[dict[str, Any]]]:
    warehouses = list(
        session.execute(select(Warehouse).order_by(Warehouse.is_archived, Warehouse.name)).scalars()
    )
    products = list(session.execute(select(Product).order_by(Product.sku)).scalars())
    balances = {
        (row.product_id, row.warehouse_id): row
        for row in session.execute(select(StockBalance)).scalars()
    }
    with_stock = {
        warehouse_id
        for (_, warehouse_id), row in balances.items()
        if row.on_hand > 0 or row.reserved > 0
    }
    visible_ids = with_stock | open_warehouse_ids(session)
    visible = [wh for wh in warehouses if not wh.is_archived or wh.id in visible_ids]

    rows: list[dict[str, Any]] = []
    for product in products:
        cells: dict[int, dict[str, int]] = {}
        total_on_hand = total_reserved = 0
        for warehouse in visible:
            row = balances.get((product.id, warehouse.id))
            on_hand = row.on_hand if row else 0
            reserved = row.reserved if row else 0
            cells[warehouse.id] = {
                "on_hand": on_hand,
                "reserved": reserved,
                "available": on_hand - reserved,
            }
            total_on_hand += on_hand
            total_reserved += reserved
        rows.append(
            {
                "product": product,
                "cells": cells,
                "totals": {
                    "on_hand": total_on_hand,
                    "reserved": total_reserved,
                    "available": total_on_hand - total_reserved,
                },
            }
        )
    return visible, rows


def in_transit(session: Session) -> dict[str, list[Any]]:
    transfers = list(
        session.execute(
            select(Transfer)
            .options(
                selectinload(Transfer.lines).selectinload(TransferLine.product),
                selectinload(Transfer.from_warehouse),
                selectinload(Transfer.to_warehouse),
            )
            .where(Transfer.status == "in_transit")
            .order_by(Transfer.id)
        ).scalars()
    )
    fbo = list(
        session.execute(
            select(Shipment)
            .options(
                selectinload(Shipment.lines).selectinload(ShipmentLine.product),
                selectinload(Shipment.warehouse),
            )
            .where(Shipment.kind == "FBO", Shipment.status == "shipped")
            .order_by(Shipment.id)
        ).scalars()
    )
    fbs = list(
        session.execute(
            select(Shipment)
            .options(
                selectinload(Shipment.lines).selectinload(ShipmentLine.product),
                selectinload(Shipment.warehouse),
            )
            .where(Shipment.kind == "FBS", Shipment.status == "shipped")
            .order_by(Shipment.id)
        ).scalars()
    )
    return {"transfers": transfers, "fbo": fbo, "fbs": fbs}


def pending_fbs(session: Session) -> list[Shipment]:
    """Зарезервированные FBS, которые ещё не отправлены клиенту."""
    return list(
        session.execute(
            select(Shipment)
            .options(
                selectinload(Shipment.lines).selectinload(ShipmentLine.product),
                selectinload(Shipment.warehouse),
            )
            .where(Shipment.kind == "FBS", Shipment.status == "reserved")
            .order_by(Shipment.id)
        ).scalars()
    )


def history(
    session: Session,
    *,
    sku: str | None = None,
    warehouse_id: int | None = None,
    op_type: str | None = None,
    limit: int = 200,
) -> list[StockMovement]:
    stmt = (
        select(StockMovement)
        .options(
            selectinload(StockMovement.product),
            selectinload(StockMovement.from_warehouse),
            selectinload(StockMovement.to_warehouse),
        )
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .limit(limit)
    )
    if sku:
        stmt = stmt.join(Product, Product.id == StockMovement.product_id).where(
            Product.sku == sku.strip()
        )
    if warehouse_id is not None:
        stmt = stmt.where(
            or_(
                StockMovement.from_warehouse_id == warehouse_id,
                StockMovement.to_warehouse_id == warehouse_id,
            )
        )
    if op_type:
        stmt = stmt.where(StockMovement.op_type == op_type)
    return list(session.execute(stmt).scalars())


def product_locations(session: Session, product_id: int) -> list[dict[str, Any]]:
    from sqlalchemy import text

    rows = session.execute(
        text(
            "SELECT place, warehouse_id, qty, doc_type, doc_id "
            "FROM v_product_locations WHERE product_id = :product_id "
            "ORDER BY place, warehouse_id"
        ),
        {"product_id": product_id},
    )
    warehouses = {
        warehouse.id: warehouse.name for warehouse in session.execute(select(Warehouse)).scalars()
    }
    return [
        {
            "place": row.place,
            "warehouse_id": row.warehouse_id,
            "warehouse": warehouses.get(row.warehouse_id),
            "qty": row.qty,
            "doc_type": row.doc_type,
            "doc_id": row.doc_id,
        }
        for row in rows
    ]
