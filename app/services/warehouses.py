from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import IS_SQLITE
from ..errors import name_taken, validation_error
from ..models import Product, Shipment, StockBalance, Transfer, Warehouse
from .stock import utcnow


def _trim_expr(column):
    return func.trim(column) if IS_SQLITE else func.btrim(column)


def normalize_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name:
        raise validation_error("Название ФФ обязательно.")
    if len(name) > 120:
        raise validation_error("Название ФФ не должно превышать 120 символов.")
    return name


def active_name_exists(session: Session, name: str, exclude_id: int | None = None) -> bool:
    if IS_SQLITE:
        stmt = select(Warehouse.id, Warehouse.name).where(Warehouse.is_archived.is_(False))
        if exclude_id is not None:
            stmt = stmt.where(Warehouse.id != exclude_id)
        target = name.strip().casefold()
        for wid, wname in session.execute(stmt).all():
            if (wname or "").strip().casefold() == target:
                return True
        return False
    stmt = select(Warehouse.id).where(
        Warehouse.is_archived.is_(False),
        func.lower(_trim_expr(Warehouse.name)) == name.lower(),
    )
    if exclude_id is not None:
        stmt = stmt.where(Warehouse.id != exclude_id)
    return session.scalar(stmt) is not None


def get_warehouse(session: Session, warehouse_id: int) -> Warehouse:
    warehouse = session.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise validation_error(f"ФФ с id={warehouse_id} не найден.")
    return warehouse


def get_active_warehouse(session: Session, warehouse_id: int) -> Warehouse:
    warehouse = get_warehouse(session, warehouse_id)
    if warehouse.is_archived:
        from ..errors import warehouse_archived

        raise warehouse_archived(warehouse.name)
    return warehouse


def list_warehouses(session: Session, include_archived: bool = True) -> list[Warehouse]:
    stmt = select(Warehouse).order_by(Warehouse.is_archived, Warehouse.name)
    if not include_archived:
        stmt = stmt.where(Warehouse.is_archived.is_(False))
    return list(session.execute(stmt).scalars())


def create_warehouse(session: Session, name: str | None) -> Warehouse:
    normalized = normalize_name(name)
    if active_name_exists(session, normalized):
        raise name_taken(normalized)
    warehouse = Warehouse(name=normalized)
    session.add(warehouse)
    session.commit()
    session.refresh(warehouse)
    return warehouse


def rename_warehouse(session: Session, warehouse_id: int, name: str | None) -> Warehouse:
    warehouse = get_warehouse(session, warehouse_id)
    normalized = normalize_name(name)
    if active_name_exists(session, normalized, exclude_id=warehouse_id):
        raise name_taken(normalized)
    warehouse.name = normalized
    session.commit()
    session.refresh(warehouse)
    return warehouse


def warehouse_stats(session: Session, warehouse_id: int) -> dict[str, int]:
    on_hand = session.scalar(
        select(func.coalesce(func.sum(StockBalance.on_hand), 0)).where(
            StockBalance.warehouse_id == warehouse_id
        )
    )
    reserved = session.scalar(
        select(func.coalesce(func.sum(StockBalance.reserved), 0)).where(
            StockBalance.warehouse_id == warehouse_id
        )
    )
    open_transfers = session.scalar(
        select(func.count(Transfer.id)).where(
            Transfer.status == "in_transit",
            or_(
                Transfer.from_warehouse_id == warehouse_id,
                Transfer.to_warehouse_id == warehouse_id,
            ),
        )
    )
    open_shipments = session.scalar(
        select(func.count(Shipment.id)).where(
            Shipment.warehouse_id == warehouse_id,
            or_(
                Shipment.status == "reserved",
                Shipment.status == "shipped",
            ),
        )
    )
    return {
        "on_hand": int(on_hand or 0),
        "reserved": int(reserved or 0),
        "open_documents": int(open_transfers or 0) + int(open_shipments or 0),
    }


def warehouse_stock_lines(session: Session, warehouse_id: int) -> list[dict[str, object]]:
    """Остатки ФФ по артикулам — для предупреждения перед удалением склада."""
    rows = session.execute(
        select(StockBalance, Product)
        .join(Product, Product.id == StockBalance.product_id)
        .where(StockBalance.warehouse_id == warehouse_id)
        .order_by(Product.sku)
    ).all()
    return [
        {
            "product": product,
            "on_hand": balance.on_hand,
            "reserved": balance.reserved,
            "available": balance.on_hand - balance.reserved,
        }
        for balance, product in rows
        if balance.on_hand > 0 or balance.reserved > 0
    ]


def archive_warehouse(session: Session, warehouse_id: int) -> tuple[Warehouse, dict[str, int]]:
    warehouse = get_warehouse(session, warehouse_id)
    stats = warehouse_stats(session, warehouse_id)
    if not warehouse.is_archived:
        warehouse.is_archived = True
        warehouse.archived_at = utcnow()
        session.commit()
        session.refresh(warehouse)
    return warehouse, stats


def archived_warehouse_ids_with_stock(session: Session) -> set[int]:
    rows = session.execute(
        select(StockBalance.warehouse_id).where(StockBalance.on_hand > 0).distinct()
    ).scalars()
    return set(rows)
