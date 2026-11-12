from __future__ import annotations

from datetime import UTC
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..errors import AppError, validation_error
from ..models import Product
from ..serializers import (
    history,
    in_transit,
    movement_dict,
    pending_fbs,
    product_locations,
    stock_matrix,
)
from ..services import receipts, shipments, transfers, warehouses
from ..templating import templates

router = APIRouter()

OP_TYPES = [
    ("receipt", "Приход"),
    ("reserve", "Резерв FBS"),
    ("unreserve", "Отмена резерва"),
    ("fbs_ship", "Отправка FBS"),
    ("transfer_out", "Перемещение: отправка"),
    ("transfer_in", "Перемещение: приёмка"),
    ("fbo_ship", "Отправка FBO"),
    ("fbo_accept", "Приёмка FBO"),
]

PLACE_LABELS = {
    "supplier": "Поставщик",
    "free": "Свободно на ФФ",
    "reserved": "Резерв FBS",
    "transfer_transit": "В пути между ФФ",
    "fbs_transit": "К клиенту (FBS)",
    "fbo_transit": "На склад МП (FBO)",
    "mp_warehouse": "Принято на складе МП",
}

OP_LABELS = dict(OP_TYPES)

OP_STATUS = {
    "receipt": "Проведено",
    "reserve": "В резерве",
    "unreserve": "Резерв отменён",
    "fbs_ship": "В пути к клиенту",
    "transfer_out": "В пути",
    "transfer_in": "Принято",
    "fbo_ship": "В пути на склад МП",
    "fbo_accept": "Принято",
}


def _movement_items(movements) -> list[dict[str, Any]]:
    items = []
    for movement in movements:
        item = movement_dict(movement)
        item["op_label"] = OP_LABELS.get(item["op_type"], item["op_type"])
        item["status"] = OP_STATUS.get(item["op_type"], "—")
        items.append(item)
    return items


def _redirect(path: str, message: str | None = None) -> RedirectResponse:
    if message:
        separator = "&" if "?" in path else "?"
        path = f"{path}{separator}ok={quote(message)}"
    return RedirectResponse(path, status_code=303)


def _parse_int(value: Any, field: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        raise validation_error(f"Поле «{field}» должно быть целым числом.") from None


def _safe_return_to(value: Any, default: str) -> str:
    path = str(value or "").strip()
    if path.startswith("/") and not path.startswith("//"):
        return path
    return default


def _parse_key(value: Any) -> UUID:
    if not value:
        return uuid4()
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        raise validation_error("Некорректный idempotency_key.") from None


def _form_lines(form: Any) -> list[dict[str, int]]:
    product_ids = form.getlist("product_id")
    qtys = form.getlist("qty")
    if len(product_ids) != len(qtys):
        raise validation_error("Некорректные строки документа.")
    lines: list[dict[str, int]] = []
    for product_id, qty in zip(product_ids, qtys, strict=False):
        qty_text = (qty or "").strip() if isinstance(qty, str) else str(qty or "").strip()
        if not qty_text:
            continue
        if not product_id:
            raise validation_error("Выберите артикул в строке документа.")
        lines.append(
            {
                "product_id": _parse_int(product_id, "артикул"),
                "qty": _parse_int(qty_text, "количество"),
            }
        )
    if not lines:
        raise validation_error("Добавьте хотя бы одну строку с количеством.")
    return lines


def _active_warehouses(session: Session):
    return warehouses.list_warehouses(session, include_archived=False)


def _all_products(session: Session):
    return list(session.execute(select(Product).order_by(Product.sku)).scalars())


def _error_response(
    request: Request,
    session: Session,
    exc: AppError,
    *,
    action: str,
    lines: list[dict[str, int]] | None = None,
    warehouse_id: int | None = None,
    from_warehouse_id: int | None = None,
    to_warehouse_id: int | None = None,
    external_ref: str | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "error": exc,
            "action": action,
            "lines": lines or [],
            "warehouse_id": warehouse_id,
            "from_warehouse_id": from_warehouse_id,
            "to_warehouse_id": to_warehouse_id,
            "external_ref": external_ref,
            "idempotency_key": uuid4(),
            "active_warehouses": _active_warehouses(session),
            "products": _all_products(session),
            "place_labels": PLACE_LABELS,
        },
        status_code=exc.status_code,
    )


@router.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    visible, rows = stock_matrix(session)
    transit = in_transit(session)
    transit_product_ids = set()
    for t in transit.get("transfers", []):
        for line in t.lines:
            transit_product_ids.add(line.product_id)
    for s in transit.get("fbo", []) + transit.get("fbs", []):
        for line in s.lines:
            transit_product_ids.add(line.product_id)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "warehouses": visible,
            "rows": rows,
            "transit": transit,
            "transit_product_ids": sorted(transit_product_ids),
            "idempotency_key": uuid4(),
        },
    )


@router.get("/products/{sku}", response_class=HTMLResponse)
def product_card(
    request: Request, sku: str, session: Session = Depends(get_session)
) -> HTMLResponse:
    product = session.scalar(select(Product).where(Product.sku == sku))
    if product is None:
        raise validation_error(f"Артикул {sku} не найден.")
    movements = history(session, sku=sku, limit=100)
    return templates.TemplateResponse(
        request,
        "product.html",
        {
            "product": product,
            "locations": product_locations(session, product.id),
            "movements": _movement_items(movements),
            "place_labels": PLACE_LABELS,
        },
    )


@router.get("/warehouses", response_class=HTMLResponse)
def warehouses_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    items = []
    for warehouse in warehouses.list_warehouses(session):
        items.append(
            {
                "warehouse": warehouse,
                "stats": warehouses.warehouse_stats(session, warehouse.id),
            }
        )
    return templates.TemplateResponse(
        request,
        "warehouses.html",
        {"items": items, "idempotency_key": uuid4()},
    )


@router.get("/warehouses/{warehouse_id}/delete", response_class=HTMLResponse)
def warehouse_delete_page(
    request: Request, warehouse_id: int, session: Session = Depends(get_session)
) -> HTMLResponse:
    warehouse = warehouses.get_warehouse(session, warehouse_id)
    stats = warehouses.warehouse_stats(session, warehouse_id)
    stock_lines = warehouses.warehouse_stock_lines(session, warehouse_id)
    return templates.TemplateResponse(
        request,
        "warehouse_delete.html",
        {
            "warehouse": warehouse,
            "stats": stats,
            "stock_lines": stock_lines,
            "has_stock": stats["on_hand"] > 0 or stats["reserved"] > 0,
            "active_warehouses": [
                item for item in _active_warehouses(session) if item.id != warehouse_id
            ],
            "idempotency_key": uuid4(),
        },
    )


def _stock_context(session: Session) -> dict[str, Any]:
    stock_warehouses, stock_rows = stock_matrix(session)
    availability = {
        str(warehouse.id): {
            str(row["product"].id): row["cells"][warehouse.id]["available"] for row in stock_rows
        }
        for warehouse in stock_warehouses
    }
    transit = in_transit(session)
    transit_product_ids = set()
    for t in transit.get("transfers", []):
        for line in t.lines:
            transit_product_ids.add(line.product_id)
    for s in transit.get("fbo", []) + transit.get("fbs", []):
        for line in s.lines:
            transit_product_ids.add(line.product_id)
    return {
        "stock_warehouses": stock_warehouses,
        "stock_rows": stock_rows,
        "availability": availability,
        "transit": transit,
        "transit_product_ids": sorted(transit_product_ids),
    }


@router.get("/operations", response_class=HTMLResponse)
def operations_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    active = _active_warehouses(session)
    return templates.TemplateResponse(
        request,
        "operations.html",
        {
            "active_warehouses": active,
            "all_warehouses": warehouses.list_warehouses(session),
            "products": _all_products(session),
            **_stock_context(session),
            "keys": {
                "receipt": uuid4(),
                "transfer": uuid4(),
            },
            "prefill": {
                "tab": request.query_params.get("tab", "receipt"),
                "warehouse_id": request.query_params.get("warehouse_id"),
                "from_warehouse_id": request.query_params.get("from_warehouse_id"),
                "to_warehouse_id": request.query_params.get("to_warehouse_id"),
                "product_id": request.query_params.get("product_id"),
                "qty": request.query_params.get("qty"),
            },
        },
    )


@router.get("/fbs-fbo", response_class=HTMLResponse)
def fbs_fbo_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    active = _active_warehouses(session)
    return templates.TemplateResponse(
        request,
        "fbs_fbo.html",
        {
            "active_warehouses": active,
            "products": _all_products(session),
            "reserved_fbs": pending_fbs(session),
            **_stock_context(session),
            "keys": {
                "fbs": uuid4(),
                "fbo": uuid4(),
            },
            "prefill": {
                "tab": request.query_params.get("tab", "fbs"),
                "warehouse_id": request.query_params.get("warehouse_id"),
                "product_id": request.query_params.get("product_id"),
                "qty": request.query_params.get("qty"),
            },
        },
    )


@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    from datetime import datetime, timedelta

    sku = request.query_params.get("sku") or None
    op_type = request.query_params.get("op_type") or None
    warehouse_raw = request.query_params.get("warehouse_id") or ""
    warehouse_id = int(warehouse_raw) if warehouse_raw.isdigit() else None
    date_from_raw = request.query_params.get("date_from") or ""
    date_to_raw = request.query_params.get("date_to") or ""
    date_from = date_to = None
    try:
        if date_from_raw:
            date_from = datetime.fromisoformat(date_from_raw).replace(tzinfo=UTC)
        if date_to_raw:
            date_to = datetime.fromisoformat(date_to_raw).replace(tzinfo=UTC) + timedelta(days=1)
    except ValueError:
        date_from = date_to = None
    movements = history(session, sku=sku, warehouse_id=warehouse_id, op_type=op_type, limit=300)
    if date_from or date_to:
        movements = [
            m
            for m in movements
            if (date_from is None or m.created_at >= date_from)
            and (date_to is None or m.created_at < date_to)
        ]
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "movements": _movement_items(movements),
            "warehouses": warehouses.list_warehouses(session),
            "op_types": OP_TYPES,
            "filters": {
                "sku": sku or "",
                "op_type": op_type or "",
                "warehouse_id": warehouse_id,
                "date_from": date_from_raw,
                "date_to": date_to_raw,
            },
            "place_labels": PLACE_LABELS,
        },
    )


@router.post("/warehouses")
async def add_warehouse(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    try:
        warehouse = warehouses.create_warehouse(session, form.get("name"))
    except AppError as exc:
        return _error_response(request, session, exc, action="/warehouses")
    return _redirect("/warehouses", f"ФФ «{warehouse.name}» добавлен.")


@router.post("/warehouses/{warehouse_id}/rename")
async def rename_warehouse(
    request: Request, warehouse_id: int, session: Session = Depends(get_session)
):
    form = await request.form()
    try:
        warehouse = warehouses.rename_warehouse(session, warehouse_id, form.get("name"))
    except AppError as exc:
        return _error_response(request, session, exc, action=f"/warehouses/{warehouse_id}/rename")
    return _redirect("/warehouses", f"ФФ переименован в «{warehouse.name}».")


@router.post("/warehouses/{warehouse_id}/archive")
async def archive_warehouse(
    request: Request, warehouse_id: int, session: Session = Depends(get_session)
):
    try:
        warehouse = warehouses.get_warehouse(session, warehouse_id)
        stats = warehouses.warehouse_stats(session, warehouse_id)
        if stats["on_hand"] > 0 or stats["reserved"] > 0:
            return _redirect(
                f"/warehouses/{warehouse_id}/delete",
                "Сначала переместите товар на другой ФФ — остатки ещё числятся на складе.",
            )
        warehouse, stats = warehouses.archive_warehouse(session, warehouse_id)
    except AppError as exc:
        return _error_response(request, session, exc, action=f"/warehouses/{warehouse_id}/archive")
    return _redirect(
        "/warehouses",
        f"ФФ «{warehouse.name}» в архиве. Сохранено остатков: {stats['on_hand']}.",
    )


@router.post("/receipts")
async def add_receipt(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    lines: list[dict[str, int]] = []
    warehouse_id = None
    try:
        warehouse_id = _parse_int(form.get("warehouse_id"), "склад")
        lines = _form_lines(form)
        receipts.create_receipt(
            session,
            warehouse_id=warehouse_id,
            lines=lines,
            idempotency_key=_parse_key(form.get("idempotency_key")),
            comment=form.get("comment"),
        )
    except AppError as exc:
        return _error_response(
            request, session, exc, action="/receipts", lines=lines, warehouse_id=warehouse_id
        )
    return _redirect("/operations?tab=receipt", "Приход успешно оформлен. Остатки обновлены.")


@router.post("/transfers")
async def add_transfer(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    lines: list[dict[str, int]] = []
    from_id = to_id = None
    try:
        from_id = _parse_int(form.get("from_warehouse_id"), "ФФ-отправитель")
        to_id = _parse_int(form.get("to_warehouse_id"), "ФФ-получатель")
        lines = _form_lines(form)
        transfers.create_transfer(
            session,
            from_warehouse_id=from_id,
            to_warehouse_id=to_id,
            lines=lines,
            idempotency_key=_parse_key(form.get("idempotency_key")),
        )
    except AppError as exc:
        return _error_response(
            request,
            session,
            exc,
            action="/transfers",
            lines=lines,
            from_warehouse_id=from_id,
            to_warehouse_id=to_id,
        )
    return _redirect(
        _safe_return_to(form.get("return_to"), "/operations?tab=transfer"),
        "Перемещение отправлено. Остатки обновлены.",
    )


@router.post("/transfers/{transfer_id}/receive")
def receive_transfer(request: Request, transfer_id: int, session: Session = Depends(get_session)):
    try:
        transfers.receive_transfer(session, transfer_id)
    except AppError as exc:
        return _error_response(request, session, exc, action=f"/transfers/{transfer_id}/receive")
    return _redirect("/", "Перемещение принято.")


@router.post("/fbs")
async def add_fbs(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    lines: list[dict[str, int]] = []
    warehouse_id = None
    external_ref = form.get("external_ref")
    try:
        warehouse_id = _parse_int(form.get("warehouse_id"), "склад")
        lines = _form_lines(form)
        shipments.create_fbs(
            session,
            warehouse_id=warehouse_id,
            lines=lines,
            idempotency_key=_parse_key(form.get("idempotency_key")),
            external_ref=external_ref,
        )
    except AppError as exc:
        return _error_response(
            request,
            session,
            exc,
            action="/fbs",
            lines=lines,
            warehouse_id=warehouse_id,
            external_ref=external_ref,
        )
    return _redirect("/fbs-fbo?tab=fbs", "Резерв FBS создан.")


@router.post("/fbs/split")
async def split_fbs(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    lines: list[dict[str, int]] = []
    try:
        lines = _form_lines(form)
        shipments.split_fbs(
            session,
            lines=lines,
            idempotency_key=_parse_key(form.get("idempotency_key")),
        )
    except AppError as exc:
        return _error_response(request, session, exc, action="/fbs/split", lines=lines)
    return _redirect("/fbs-fbo?tab=fbs", "Резерв разделён между ФФ.")


@router.post("/fbs/{shipment_id}/ship")
def ship_fbs(request: Request, shipment_id: int, session: Session = Depends(get_session)):
    try:
        shipments.ship_fbs(session, shipment_id)
    except AppError as exc:
        return _error_response(request, session, exc, action=f"/fbs/{shipment_id}/ship")
    return _redirect("/fbs-fbo", "FBS отправлен.")


@router.post("/fbs/{shipment_id}/cancel")
def cancel_fbs(request: Request, shipment_id: int, session: Session = Depends(get_session)):
    try:
        shipments.cancel_fbs(session, shipment_id)
    except AppError as exc:
        return _error_response(request, session, exc, action=f"/fbs/{shipment_id}/cancel")
    return _redirect("/fbs-fbo", "Резерв FBS отменён.")


@router.post("/fbo")
async def add_fbo(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    lines: list[dict[str, int]] = []
    warehouse_id = None
    external_ref = form.get("external_ref")
    try:
        warehouse_id = _parse_int(form.get("warehouse_id"), "склад")
        lines = _form_lines(form)
        shipments.create_fbo(
            session,
            warehouse_id=warehouse_id,
            lines=lines,
            idempotency_key=_parse_key(form.get("idempotency_key")),
            external_ref=external_ref,
        )
    except AppError as exc:
        return _error_response(
            request,
            session,
            exc,
            action="/fbo",
            lines=lines,
            warehouse_id=warehouse_id,
            external_ref=external_ref,
        )
    return _redirect("/fbs-fbo?tab=fbo", "FBO отправлен на склад МП.")


@router.post("/fbo/{shipment_id}/accept")
def accept_fbo(request: Request, shipment_id: int, session: Session = Depends(get_session)):
    try:
        shipments.accept_fbo(session, shipment_id)
    except AppError as exc:
        return _error_response(request, session, exc, action=f"/fbo/{shipment_id}/accept")
    return _redirect("/", "FBO принят на складе МП.")
