"""Тестовые данные CheckStock.

Начальные остатки создаются настоящими приходами, а не прямой вставкой в
stock_balances: история и остатки сходятся с первой секунды. Повторный запуск
не дублирует документы — у каждого прихода фиксированный idempotency_key.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import IS_SQLITE

from app.db import SessionLocal
from app.models import Product, Receipt, Shop, Warehouse
from app.services import receipts
from app.services.stock import assert_consistent

SEED_NAMESPACE = NAMESPACE_URL

SHOP_NAME = "Магазин WB"
PRODUCTS = [
    ("SKU-101", "Органайзер"),
    ("SKU-102", "Контейнер"),
    ("SKU-103", "Корзина"),
]
WAREHOUSES = ["Север", "Юг"]

# (номер прихода, ФФ, SKU, количество). Пять приходов — как в сценарии E1.
RECEIPTS = [
    (1, "Север", "SKU-101", 6),
    (2, "Север", "SKU-101", 4),
    (3, "Юг", "SKU-101", 6),
    (4, "Север", "SKU-102", 5),
    (5, "Юг", "SKU-103", 4),
]


def _get_or_create_shop(session: Session) -> Shop:
    shop = session.scalar(select(Shop).where(Shop.name == SHOP_NAME))
    if shop is None:
        shop = Shop(name=SHOP_NAME)
        session.add(shop)
        session.commit()
        session.refresh(shop)
    return shop


def _get_or_create_warehouse(session: Session, name: str) -> Warehouse:
    target = name.strip().casefold()
    active = session.scalars(select(Warehouse).where(Warehouse.is_archived.is_(False))).all()
    for wh in active:
        if wh.name.strip().casefold() == target:
            return wh
    warehouse = Warehouse(name=name)
    session.add(warehouse)
    session.commit()
    session.refresh(warehouse)
    return warehouse


def _get_or_create_product(session: Session, sku: str, name: str) -> Product:
    product = session.scalar(select(Product).where(Product.sku == sku))
    if product is None:
        product = Product(sku=sku, name=name)
        session.add(product)
        session.commit()
        session.refresh(product)
    return product


def seed_database(session: Session) -> None:
    _get_or_create_shop(session)
    warehouses = {name: _get_or_create_warehouse(session, name) for name in WAREHOUSES}
    products = {sku: _get_or_create_product(session, sku, name) for sku, name in PRODUCTS}

    for number, warehouse_name, sku, qty in RECEIPTS:
        key = uuid5(SEED_NAMESPACE, f"checkstock/seed/receipt/{number}")
        if session.scalar(select(Receipt.id).where(Receipt.idempotency_key == key)) is not None:
            continue
        receipts.create_receipt(
            session,
            warehouse_id=warehouses[warehouse_name].id,
            lines=[{"product_id": products[sku].id, "qty": qty}],
            idempotency_key=key,
            comment=f"Тестовый приход #{number}",
        )


def main() -> None:
    with SessionLocal() as session:
        seed_database(session)
        assert_consistent(session)
    print("Seed готов: 2 ФФ, 3 артикула, 5 приходов.")


if __name__ == "__main__":
    main()
