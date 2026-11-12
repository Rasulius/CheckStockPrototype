from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from ..config import IS_SQLITE

if IS_SQLITE:
    from sqlalchemy.dialects.sqlite import insert as db_insert
else:
    from sqlalchemy.dialects.postgresql import insert as db_insert

from ..errors import validation_error
from ..models import (
    Product,
    ReceiptLine,
    Shipment,
    ShipmentLine,
    StockBalance,
    StockMovement,
    Transfer,
    TransferLine,
    Warehouse,
)

BalanceKey = tuple[int, int]


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_balance_rows(session: Session, pairs: Iterable[BalanceKey]) -> None:
    """Создаёт нулевые строки остатков, чтобы их можно было заблокировать."""
    unique = sorted(set(pairs))
    if not unique:
        return
    stmt = db_insert(StockBalance).values(
        [
            {"product_id": product_id, "warehouse_id": warehouse_id}
            for product_id, warehouse_id in unique
        ]
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["product_id", "warehouse_id"])
    session.execute(stmt)


def lock_balances(session: Session, pairs: Iterable[BalanceKey]) -> dict[BalanceKey, StockBalance]:
    """Блокирует строки остатков в детерминированном порядке (product_id, warehouse_id).

    Единый порядок исключает взаимные блокировки при встречных перемещениях.
    """
    unique = sorted(set(pairs))
    ensure_balance_rows(session, unique)
    if not unique:
        return {}
    query = (
        select(StockBalance)
        .where(tuple_(StockBalance.product_id, StockBalance.warehouse_id).in_(unique))
        .order_by(StockBalance.product_id, StockBalance.warehouse_id)
    )
    if not IS_SQLITE:
        query = query.with_for_update()
    rows = session.execute(query).scalars().all()
    return {(row.product_id, row.warehouse_id): row for row in rows}


def free_qty(row: StockBalance | None) -> int:
    return 0 if row is None else row.on_hand - row.reserved


def record_movement(
    session: Session,
    *,
    op_type: str,
    product_id: int,
    qty: int,
    from_place: str,
    to_place: str,
    doc_type: str,
    doc_id: int,
    from_warehouse_id: int | None = None,
    to_warehouse_id: int | None = None,
) -> None:
    session.add(
        StockMovement(
            op_type=op_type,
            product_id=product_id,
            qty=qty,
            from_place=from_place,
            from_warehouse_id=from_warehouse_id,
            to_place=to_place,
            to_warehouse_id=to_warehouse_id,
            doc_type=doc_type,
            doc_id=doc_id,
        )
    )


def total_available(session: Session, product_id: int) -> int:
    value = session.scalar(
        select(func.coalesce(func.sum(StockBalance.on_hand - StockBalance.reserved), 0))
        .join(Warehouse, Warehouse.id == StockBalance.warehouse_id)
        .where(StockBalance.product_id == product_id, Warehouse.is_archived.is_(False))
    )
    return int(value or 0)


def available_alternatives(
    session: Session, product_id: int, exclude_warehouse_id: int | None = None
) -> list[dict[str, int | str]]:
    stmt = (
        select(
            Warehouse.id,
            Warehouse.name,
            (StockBalance.on_hand - StockBalance.reserved).label("available"),
        )
        .join(StockBalance, StockBalance.warehouse_id == Warehouse.id)
        .where(
            StockBalance.product_id == product_id,
            Warehouse.is_archived.is_(False),
            StockBalance.on_hand > StockBalance.reserved,
        )
        .order_by((StockBalance.on_hand - StockBalance.reserved).desc(), Warehouse.name)
    )
    if exclude_warehouse_id is not None:
        stmt = stmt.where(Warehouse.id != exclude_warehouse_id)
    return [
        {"warehouse_id": row.id, "name": row.name, "available": int(row.available)}
        for row in session.execute(stmt)
    ]


def assert_consistent(session: Session) -> None:
    """Проверяет главный инвариант учёта. Вызывается в тестах после каждого сценария."""
    balances = session.execute(select(StockBalance)).scalars().all()
    for row in balances:
        if row.on_hand < 0 or row.reserved < 0 or row.reserved > row.on_hand:
            raise AssertionError(
                f"Некорректный остаток product={row.product_id} warehouse={row.warehouse_id}: "
                f"on_hand={row.on_hand}, reserved={row.reserved}"
            )

    # 2. Σ приходов = on_hand + в пути между ФФ + FBO в пути + FBO принято + FBS отправлено
    received: dict[int, int] = {
        product_id: int(qty)
        for product_id, qty in session.execute(
            select(ReceiptLine.product_id, func.sum(ReceiptLine.qty)).group_by(
                ReceiptLine.product_id
            )
        )
    }
    on_hand: dict[int, int] = defaultdict(int)
    for row in balances:
        on_hand[row.product_id] += row.on_hand

    transfer_transit: dict[int, int] = {
        product_id: int(qty)
        for product_id, qty in session.execute(
            select(TransferLine.product_id, func.sum(TransferLine.qty))
            .join(Transfer, Transfer.id == TransferLine.transfer_id)
            .where(Transfer.status == "in_transit")
            .group_by(TransferLine.product_id)
        )
    }
    fbo_transit: dict[int, int] = {}
    fbo_accepted: dict[int, int] = {}
    fbs_transit: dict[int, int] = {}
    for kind, status, target in (
        ("FBO", "shipped", fbo_transit),
        ("FBO", "accepted", fbo_accepted),
        ("FBS", "shipped", fbs_transit),
    ):
        target.update(
            {
                product_id: int(qty)
                for product_id, qty in session.execute(
                    select(ShipmentLine.product_id, func.sum(ShipmentLine.qty))
                    .join(Shipment, Shipment.id == ShipmentLine.shipment_id)
                    .where(Shipment.kind == kind, Shipment.status == status)
                    .group_by(ShipmentLine.product_id)
                )
            }
        )

    products = session.execute(select(Product.id, Product.sku)).all()
    for product_id, sku in products:
        left = received.get(product_id, 0)
        right = (
            on_hand.get(product_id, 0)
            + transfer_transit.get(product_id, 0)
            + fbo_transit.get(product_id, 0)
            + fbo_accepted.get(product_id, 0)
            + fbs_transit.get(product_id, 0)
        )
        if left != right:
            raise AssertionError(
                f"Нарушен баланс по {sku}: приходы={left}, "
                f"on_hand={on_hand.get(product_id, 0)}, "
                f"transfer={transfer_transit.get(product_id, 0)}, "
                f"fbo_transit={fbo_transit.get(product_id, 0)}, "
                f"fbo_accepted={fbo_accepted.get(product_id, 0)}, "
                f"fbs_transit={fbs_transit.get(product_id, 0)}"
            )

    # 3. Пересчёт остатков из журнала должен совпасть со stock_balances.
    recalculated: dict[BalanceKey, list[int]] = defaultdict(lambda: [0, 0])
    for movement in session.execute(select(StockMovement)).scalars():
        key_from = (movement.product_id, movement.from_warehouse_id)
        key_to = (movement.product_id, movement.to_warehouse_id)
        if movement.op_type == "receipt":
            recalculated[key_to][0] += movement.qty
        elif movement.op_type == "reserve":
            recalculated[key_from][1] += movement.qty
        elif movement.op_type == "unreserve":
            recalculated[key_from][1] -= movement.qty
        elif movement.op_type == "fbs_ship":
            recalculated[key_from][0] -= movement.qty
            recalculated[key_from][1] -= movement.qty
        elif movement.op_type == "transfer_out":
            recalculated[key_from][0] -= movement.qty
        elif movement.op_type == "transfer_in":
            recalculated[key_to][0] += movement.qty
        elif movement.op_type == "fbo_ship":
            recalculated[key_from][0] -= movement.qty
        elif movement.op_type == "fbo_accept":
            continue

    actual = {(row.product_id, row.warehouse_id): [row.on_hand, row.reserved] for row in balances}
    for key, values in recalculated.items():
        if values == [0, 0] and key not in actual:
            continue
        expected = actual.get(key, [0, 0])
        if values != expected:
            raise AssertionError(
                f"Остатки не сходятся с журналом для product={key[0]} warehouse={key[1]}: "
                f"журнал={values}, balances={expected}"
            )
    for key, values in actual.items():
        if values != recalculated.get(key, [0, 0]):
            raise AssertionError(
                f"Остатки не сходятся с журналом для product={key[0]} warehouse={key[1]}: "
                f"balances={values}, журнал={recalculated.get(key, [0, 0])}"
            )


def require_products(session: Session, product_ids: Iterable[int]) -> dict[int, Product]:
    ids = list(product_ids)
    products = {
        product.id: product
        for product in session.execute(select(Product).where(Product.id.in_(ids))).scalars()
    }
    missing = [str(product_id) for product_id in ids if product_id not in products]
    if missing:
        raise validation_error(
            "Неизвестный артикул: " + ", ".join(missing),
            unknown_product_ids=[int(item) for item in missing],
        )
    return products
