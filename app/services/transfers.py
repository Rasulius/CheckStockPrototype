from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..errors import invalid_state, total_shortage, validation_error, warehouse_shortage
from ..models import Transfer, TransferLine, Warehouse
from .stock import (
    available_alternatives,
    free_qty,
    lock_balances,
    record_movement,
    total_available,
)
from .validation import normalize_lines
from .warehouses import get_active_warehouse, get_warehouse


def _load_transfer(session: Session, transfer_id: int) -> Transfer:
    transfer = session.scalar(
        select(Transfer)
        .options(
            selectinload(Transfer.lines).selectinload(TransferLine.product),
            selectinload(Transfer.from_warehouse),
            selectinload(Transfer.to_warehouse),
        )
        .where(Transfer.id == transfer_id)
    )
    if transfer is None:
        raise validation_error(f"Перемещение с id={transfer_id} не найдено.")
    return transfer


def get_transfer_by_key(session: Session, key: UUID) -> Transfer | None:
    return session.scalar(
        select(Transfer)
        .options(
            selectinload(Transfer.lines).selectinload(TransferLine.product),
            selectinload(Transfer.from_warehouse),
            selectinload(Transfer.to_warehouse),
        )
        .where(Transfer.idempotency_key == key)
    )


def _raise_shortage(
    session: Session,
    warehouse: Warehouse,
    product_id: int,
    requested: int,
    available: int,
) -> None:
    total = total_available(session, product_id)
    alternatives = available_alternatives(session, product_id, exclude_warehouse_id=warehouse.id)
    line = {
        "product_id": product_id,
        "requested": requested,
        "available": available,
        "total_available": total,
    }
    if total >= requested:
        raise warehouse_shortage(
            warehouse.name, requested, available, total, alternatives, lines=[line]
        )
    raise total_shortage(
        f"Не хватает суммарно: запрошено {requested}, доступно {total}.",
        requested=requested,
        available=total,
        lines=[line],
    )


def create_transfer(
    session: Session,
    *,
    from_warehouse_id: int,
    to_warehouse_id: int,
    lines: list[dict[str, Any]],
    idempotency_key: UUID,
) -> Transfer:
    existing = get_transfer_by_key(session, idempotency_key)
    if existing is not None:
        return existing

    if from_warehouse_id == to_warehouse_id:
        raise validation_error("ФФ-отправитель и получатель должны различаться.")

    source = get_warehouse(session, from_warehouse_id)
    # С архивного ФФ отправлять можно, получателем — только активный.
    destination = get_active_warehouse(session, to_warehouse_id)
    normalized = normalize_lines(session, lines)

    pairs = [(product.id, source.id) for product, _ in normalized]
    pairs += [(product.id, destination.id) for product, _ in normalized]
    rows = lock_balances(session, pairs)

    for product, qty in normalized:
        available = free_qty(rows[(product.id, source.id)])
        if qty > available:
            _raise_shortage(session, source, product.id, qty, available)

    try:
        transfer = Transfer(
            from_warehouse_id=source.id,
            to_warehouse_id=destination.id,
            status="in_transit",
            idempotency_key=idempotency_key,
        )
        session.add(transfer)
        session.flush()
        for product, qty in normalized:
            session.add(TransferLine(transfer_id=transfer.id, product_id=product.id, qty=qty))
            rows[(product.id, source.id)].on_hand -= qty
            record_movement(
                session,
                op_type="transfer_out",
                product_id=product.id,
                qty=qty,
                from_place="free",
                from_warehouse_id=source.id,
                to_place="transfer_transit",
                to_warehouse_id=destination.id,
                doc_type="transfer",
                doc_id=transfer.id,
            )

        session.commit()
    except IntegrityError:
        session.rollback()
        existing = get_transfer_by_key(session, idempotency_key)
        if existing is not None:
            return existing
        raise
    return _load_transfer(session, transfer.id)


def receive_transfer(session: Session, transfer_id: int) -> Transfer:
    transfer = _load_transfer(session, transfer_id)

    result = session.execute(
        update(Transfer)
        .where(Transfer.id == transfer.id, Transfer.status == "in_transit")
        .values(status="received", received_at=func.now())
        .returning(Transfer.id)
    )
    changed = result.scalar_one_or_none()
    if changed is None:
        session.expire(transfer)
        transfer = _load_transfer(session, transfer_id)
        if transfer.status == "received":
            return transfer
        raise invalid_state(
            f"Перемещение #{transfer.id} нельзя принять в статусе «{transfer.status}».",
            transfer_id=transfer.id,
            status=transfer.status,
        )

    normalized = [(line.product_id, line.qty) for line in transfer.lines]
    rows = lock_balances(
        session, [(product_id, transfer.to_warehouse_id) for product_id, _ in normalized]
    )
    for product_id, qty in normalized:
        rows[(product_id, transfer.to_warehouse_id)].on_hand += qty
        record_movement(
            session,
            op_type="transfer_in",
            product_id=product_id,
            qty=qty,
            from_place="transfer_transit",
            from_warehouse_id=transfer.from_warehouse_id,
            to_place="free",
            to_warehouse_id=transfer.to_warehouse_id,
            doc_type="transfer",
            doc_id=transfer.id,
        )

    session.commit()
    return _load_transfer(session, transfer_id)
