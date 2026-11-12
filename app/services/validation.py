from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..errors import validation_error
from ..models import Product
from .stock import require_products


def normalize_lines(session: Session, raw_lines: list[dict[str, Any]]) -> list[tuple[Product, int]]:
    """Проверяет строки документа и возвращает (товар, количество)."""
    if not raw_lines:
        raise validation_error("Нужна хотя бы одна строка документа.")

    normalized: list[tuple[int, int]] = []
    seen: set[int] = set()
    for raw in raw_lines:
        try:
            product_id = int(raw.get("product_id"))
        except (TypeError, ValueError):
            raise validation_error("Не указан артикул в строке документа.") from None
        qty = raw.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int):
            raise validation_error("Количество должно быть целым числом.", product_id=product_id)
        if qty <= 0:
            raise validation_error(
                "Количество должно быть больше нуля.", product_id=product_id, qty=qty
            )
        if product_id in seen:
            raise validation_error(
                "Артикул повторяется в документе. Укажите его одной строкой.",
                product_id=product_id,
            )
        seen.add(product_id)
        normalized.append((product_id, qty))

    products = require_products(session, [product_id for product_id, _ in normalized])
    return [(products[product_id], qty) for product_id, qty in normalized]


def normalize_external_ref(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
