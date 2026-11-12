from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..models import Receipt, ReceiptLine
from .stock import lock_balances, record_movement
from .validation import normalize_lines
from .warehouses import get_active_warehouse


def get_receipt(session: Session, receipt_id: int) -> Receipt:
    receipt = session.scalar(
        select(Receipt).options(selectinload(Receipt.lines)).where(Receipt.id == receipt_id)
    )
    if receipt is None:
        from ..errors import validation_error

        raise validation_error(f"Приход с id={receipt_id} не найден.")
    return receipt


def get_receipt_by_key(session: Session, key: UUID) -> Receipt | None:
    return session.scalar(
        select(Receipt).options(selectinload(Receipt.lines)).where(Receipt.idempotency_key == key)
    )


def create_receipt(
    session: Session,
    *,
    warehouse_id: int,
    lines: list[dict[str, Any]],
    idempotency_key: UUID,
    comment: str | None = None,
) -> Receipt:
    existing = get_receipt_by_key(session, idempotency_key)
    if existing is not None:
        return existing

    warehouse = get_active_warehouse(session, warehouse_id)
    normalized = normalize_lines(session, lines)

    try:
        receipt = Receipt(
            warehouse_id=warehouse.id,
            idempotency_key=idempotency_key,
            comment=(comment or "").strip() or None,
        )
        session.add(receipt)
        session.flush()
        for product, qty in normalized:
            session.add(ReceiptLine(receipt_id=receipt.id, product_id=product.id, qty=qty))

        rows = lock_balances(session, [(product.id, warehouse.id) for product, _ in normalized])
        for product, qty in normalized:
            rows[(product.id, warehouse.id)].on_hand += qty
            record_movement(
                session,
                op_type="receipt",
                product_id=product.id,
                qty=qty,
                from_place="supplier",
                to_place="free",
                to_warehouse_id=warehouse.id,
                doc_type="receipt",
                doc_id=receipt.id,
            )

        session.commit()
    except IntegrityError:
        session.rollback()
        existing = get_receipt_by_key(session, idempotency_key)
        if existing is not None:
            return existing
        raise
    session.refresh(receipt)
    return get_receipt(session, receipt.id)
