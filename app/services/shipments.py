from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..errors import invalid_state, total_shortage, validation_error, warehouse_shortage
from ..models import Shipment, ShipmentLine, Warehouse
from .stock import (
    available_alternatives,
    free_qty,
    lock_balances,
    record_movement,
    total_available,
)
from .validation import normalize_external_ref, normalize_lines
from .warehouses import get_active_warehouse


def _load_shipment(session: Session, shipment_id: int) -> Shipment:
    shipment = session.scalar(
        select(Shipment)
        .options(
            selectinload(Shipment.lines).selectinload(ShipmentLine.product),
            selectinload(Shipment.warehouse),
        )
        .where(Shipment.id == shipment_id)
    )
    if shipment is None:
        raise validation_error(f"Отправка с id={shipment_id} не найдена.")
    return shipment


def get_shipment_by_key(session: Session, key: UUID) -> Shipment | None:
    return session.scalar(
        select(Shipment)
        .options(
            selectinload(Shipment.lines).selectinload(ShipmentLine.product),
            selectinload(Shipment.warehouse),
        )
        .where(Shipment.idempotency_key == key)
    )


def _raise_line_shortage(
    session: Session,
    warehouse: Warehouse,
    product_id: int,
    requested: int,
    available: int,
    all_shortages: list[dict[str, Any]],
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
            warehouse.name,
            requested,
            available,
            total,
            alternatives,
            lines=[line, *all_shortages],
        )
    raise total_shortage(
        f"Не хватает суммарно: запрошено {requested}, доступно {total}.",
        requested=requested,
        available=total,
        lines=[line, *all_shortages],
    )


def _validate_available(
    session: Session,
    warehouse: Warehouse,
    rows: dict[tuple[int, int], Any],
    normalized: list[tuple[Any, int]],
) -> None:
    shortages: list[dict[str, Any]] = []
    for product, qty in normalized:
        available = free_qty(rows[(product.id, warehouse.id)])
        if qty > available:
            shortages.append(
                {
                    "product_id": product.id,
                    "sku": product.sku,
                    "requested": qty,
                    "available": available,
                    "total_available": total_available(session, product.id),
                }
            )
    if shortages:
        first = shortages[0]
        _raise_line_shortage(
            session,
            warehouse,
            first["product_id"],
            first["requested"],
            first["available"],
            shortages[1:],
        )


def _apply_reserve(
    session: Session,
    *,
    shipment: Shipment,
    warehouse: Warehouse,
    normalized: list[tuple[Any, int]],
    rows: dict[tuple[int, int], Any],
) -> None:
    for product, qty in normalized:
        rows[(product.id, warehouse.id)].reserved += qty
        record_movement(
            session,
            op_type="reserve",
            product_id=product.id,
            qty=qty,
            from_place="free",
            from_warehouse_id=warehouse.id,
            to_place="reserved",
            to_warehouse_id=warehouse.id,
            doc_type="shipment",
            doc_id=shipment.id,
        )


def create_fbs(
    session: Session,
    *,
    warehouse_id: int,
    lines: list[dict[str, Any]],
    idempotency_key: UUID,
    external_ref: str | None = None,
) -> Shipment:
    existing = get_shipment_by_key(session, idempotency_key)
    if existing is not None:
        return existing

    warehouse = get_active_warehouse(session, warehouse_id)
    normalized = normalize_lines(session, lines)
    rows = lock_balances(session, [(product.id, warehouse.id) for product, _ in normalized])
    _validate_available(session, warehouse, rows, normalized)

    try:
        shipment = Shipment(
            shop_id=_default_shop_id(session),
            kind="FBS",
            warehouse_id=warehouse.id,
            status="reserved",
            external_ref=normalize_external_ref(external_ref),
            idempotency_key=idempotency_key,
        )
        session.add(shipment)
        session.flush()
        for product, qty in normalized:
            session.add(ShipmentLine(shipment_id=shipment.id, product_id=product.id, qty=qty))
        _apply_reserve(
            session,
            shipment=shipment,
            warehouse=warehouse,
            normalized=normalized,
            rows=rows,
        )

        session.commit()
    except IntegrityError:
        session.rollback()
        existing = get_shipment_by_key(session, idempotency_key)
        if existing is not None:
            return existing
        raise
    return _load_shipment(session, shipment.id)


def _default_shop_id(session: Session) -> int:
    shop_id = session.scalar(select(Shipment.shop_id).limit(1))
    if shop_id is not None:
        return int(shop_id)
    from ..models import Shop

    shop = session.scalar(select(Shop).order_by(Shop.id))
    if shop is None:
        shop = Shop(name="Магазин")
        session.add(shop)
        session.flush()
    return shop.id


def ship_fbs(session: Session, shipment_id: int) -> Shipment:
    shipment = _load_shipment(session, shipment_id)
    if shipment.kind != "FBS":
        raise validation_error("Операция ship применима только к FBS.")

    result = session.execute(
        update(Shipment)
        .where(Shipment.id == shipment.id, Shipment.status == "reserved")
        .values(status="shipped", shipped_at=func.now())
        .returning(Shipment.id)
    )
    if result.scalar_one_or_none() is None:
        session.expire(shipment)
        shipment = _load_shipment(session, shipment_id)
        if shipment.status == "shipped":
            return shipment
        raise invalid_state(
            f"Отправка #{shipment.id} в статусе «{shipment.status}» не может быть отправлена.",
            shipment_id=shipment.id,
            status=shipment.status,
        )

    normalized = [(line.product_id, line.qty) for line in shipment.lines]
    rows = lock_balances(
        session, [(product_id, shipment.warehouse_id) for product_id, _ in normalized]
    )
    for product_id, qty in normalized:
        row = rows[(product_id, shipment.warehouse_id)]
        if row.on_hand < qty or row.reserved < qty:
            raise invalid_state(
                "Остатки не позволяют отправить заказ: резерв рассинхронизирован.",
                shipment_id=shipment.id,
                product_id=product_id,
            )
        row.on_hand -= qty
        row.reserved -= qty
        record_movement(
            session,
            op_type="fbs_ship",
            product_id=product_id,
            qty=qty,
            from_place="reserved",
            from_warehouse_id=shipment.warehouse_id,
            to_place="fbs_transit",
            doc_type="shipment",
            doc_id=shipment.id,
        )

    session.commit()
    return _load_shipment(session, shipment_id)


def cancel_fbs(session: Session, shipment_id: int) -> Shipment:
    shipment = _load_shipment(session, shipment_id)
    if shipment.kind != "FBS":
        raise validation_error("Операция cancel применима только к FBS.")

    result = session.execute(
        update(Shipment)
        .where(Shipment.id == shipment.id, Shipment.status == "reserved")
        .values(status="cancelled", closed_at=func.now())
        .returning(Shipment.id)
    )
    if result.scalar_one_or_none() is None:
        session.expire(shipment)
        shipment = _load_shipment(session, shipment_id)
        if shipment.status == "cancelled":
            return shipment
        raise invalid_state(
            f"Отменить отправку #{shipment.id} в статусе «{shipment.status}» нельзя.",
            shipment_id=shipment.id,
            status=shipment.status,
        )

    normalized = [(line.product_id, line.qty) for line in shipment.lines]
    rows = lock_balances(
        session, [(product_id, shipment.warehouse_id) for product_id, _ in normalized]
    )
    for product_id, qty in normalized:
        rows[(product_id, shipment.warehouse_id)].reserved -= qty
        record_movement(
            session,
            op_type="unreserve",
            product_id=product_id,
            qty=qty,
            from_place="reserved",
            from_warehouse_id=shipment.warehouse_id,
            to_place="free",
            to_warehouse_id=shipment.warehouse_id,
            doc_type="shipment",
            doc_id=shipment.id,
        )

    session.commit()
    return _load_shipment(session, shipment_id)


def create_fbo(
    session: Session,
    *,
    warehouse_id: int,
    lines: list[dict[str, Any]],
    idempotency_key: UUID,
    external_ref: str | None = None,
) -> Shipment:
    existing = get_shipment_by_key(session, idempotency_key)
    if existing is not None:
        return existing

    warehouse = get_active_warehouse(session, warehouse_id)
    normalized = normalize_lines(session, lines)
    rows = lock_balances(session, [(product.id, warehouse.id) for product, _ in normalized])
    _validate_available(session, warehouse, rows, normalized)

    try:
        shipment = Shipment(
            shop_id=_default_shop_id(session),
            kind="FBO",
            warehouse_id=warehouse.id,
            status="shipped",
            external_ref=normalize_external_ref(external_ref),
            idempotency_key=idempotency_key,
            shipped_at=func.now(),
        )
        session.add(shipment)
        session.flush()
        for product, qty in normalized:
            session.add(ShipmentLine(shipment_id=shipment.id, product_id=product.id, qty=qty))
            rows[(product.id, warehouse.id)].on_hand -= qty
            record_movement(
                session,
                op_type="fbo_ship",
                product_id=product.id,
                qty=qty,
                from_place="free",
                from_warehouse_id=warehouse.id,
                to_place="fbo_transit",
                doc_type="shipment",
                doc_id=shipment.id,
            )

        session.commit()
    except IntegrityError:
        session.rollback()
        existing = get_shipment_by_key(session, idempotency_key)
        if existing is not None:
            return existing
        raise
    return _load_shipment(session, shipment.id)


def accept_fbo(session: Session, shipment_id: int) -> Shipment:
    shipment = _load_shipment(session, shipment_id)
    if shipment.kind != "FBO":
        raise validation_error("Операция accept применима только к FBO.")

    result = session.execute(
        update(Shipment)
        .where(Shipment.id == shipment.id, Shipment.status == "shipped")
        .values(status="accepted", closed_at=func.now())
        .returning(Shipment.id)
    )
    if result.scalar_one_or_none() is None:
        session.expire(shipment)
        shipment = _load_shipment(session, shipment_id)
        if shipment.status == "accepted":
            return shipment
        raise invalid_state(
            f"Принять FBO #{shipment.id} в статусе «{shipment.status}» нельзя.",
            shipment_id=shipment.id,
            status=shipment.status,
        )

    for line in shipment.lines:
        record_movement(
            session,
            op_type="fbo_accept",
            product_id=line.product_id,
            qty=line.qty,
            from_place="fbo_transit",
            from_warehouse_id=shipment.warehouse_id,
            to_place="mp_warehouse",
            doc_type="shipment",
            doc_id=shipment.id,
        )

    session.commit()
    return _load_shipment(session, shipment_id)


def split_fbs(
    session: Session,
    *,
    lines: list[dict[str, Any]],
    idempotency_key: UUID,
) -> list[Shipment]:
    """Раскладывает резерв по активным ФФ: сначала туда, где больше доступно."""
    order_ref = f"SPLIT-{str(idempotency_key)[:8].upper()}"
    existing = list(
        session.execute(
            select(Shipment)
            .options(selectinload(Shipment.lines))
            .where(Shipment.kind == "FBS", Shipment.external_ref == order_ref)
            .order_by(Shipment.id)
        ).scalars()
    )
    if existing:
        return existing

    normalized = normalize_lines(session, lines)
    product_ids = [product.id for product, _ in normalized]
    active_warehouses = list(
        session.execute(
            select(Warehouse).where(Warehouse.is_archived.is_(False)).order_by(Warehouse.name)
        ).scalars()
    )
    pairs = [
        (product_id, warehouse.id) for product_id in product_ids for warehouse in active_warehouses
    ]
    rows = lock_balances(session, pairs)

    allocation: dict[int, list[tuple[Any, int]]] = defaultdict(list)
    for product, requested in normalized:
        remaining = requested
        candidates = sorted(
            active_warehouses,
            key=lambda wh: (-free_qty(rows[(product.id, wh.id)]), wh.name),
        )
        for warehouse in candidates:
            if remaining <= 0:
                break
            available = free_qty(rows[(product.id, warehouse.id)])
            if available <= 0:
                continue
            take = min(remaining, available)
            allocation[warehouse.id].append((product, take))
            remaining -= take
        if remaining > 0:
            total = total_available(session, product.id)
            raise total_shortage(
                f"Не хватает суммарно: запрошено {requested}, доступно {total}.",
                requested=requested,
                available=total,
                lines=[
                    {
                        "product_id": product.id,
                        "sku": product.sku,
                        "requested": requested,
                        "available": total,
                    }
                ],
            )

    if not allocation:
        raise validation_error("Не удалось распределить ни одной строки.")

    shipments: list[Shipment] = []
    try:
        for warehouse_id, wh_lines in allocation.items():
            warehouse = get_active_warehouse(session, warehouse_id)
            shipment = Shipment(
                shop_id=_default_shop_id(session),
                kind="FBS",
                warehouse_id=warehouse.id,
                status="reserved",
                external_ref=order_ref,
                idempotency_key=uuid5(idempotency_key, f"warehouse-{warehouse.id}"),
            )
            session.add(shipment)
            session.flush()
            for product, qty in wh_lines:
                session.add(ShipmentLine(shipment_id=shipment.id, product_id=product.id, qty=qty))
            _apply_reserve(
                session,
                shipment=shipment,
                warehouse=warehouse,
                normalized=wh_lines,
                rows=rows,
            )
            shipments.append(shipment)

        session.commit()
    except IntegrityError:
        session.rollback()
        existing = list(
            session.execute(
                select(Shipment)
                .options(selectinload(Shipment.lines))
                .where(Shipment.kind == "FBS", Shipment.external_ref == order_ref)
                .order_by(Shipment.id)
            ).scalars()
        )
        if existing:
            return existing
        raise

    return [_load_shipment(session, shipment.id) for shipment in shipments]
