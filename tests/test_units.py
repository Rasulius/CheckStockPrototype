from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal
from tests.helpers import (
    NORTH,
    SOUTH,
    balance,
    fbo,
    fbs,
    history,
    movement_count,
    new_key,
    products,
    receipt,
    stock,
    transfer,
    warehouses,
)


def test_receipt_validation(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north = wh[NORTH]

    for qty in (0, -1, 1.5):
        response = receipt(client, north, [(p["SKU-101"], qty)])
        assert response.status_code == 422
        assert response.json()["error"] == "VALIDATION_ERROR"

    response = receipt(client, north, [(999999, 1)])
    assert response.status_code == 422
    assert response.json()["error"] == "VALIDATION_ERROR"

    response = client.post(
        "/api/v1/receipts",
        json={
            "warehouse_id": north,
            "idempotency_key": new_key(),
            "lines": [
                {"product_id": p["SKU-101"], "qty": 1},
                {"product_id": p["SKU-101"], "qty": 2},
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"] == "VALIDATION_ERROR"

    response = client.post(
        "/api/v1/receipts",
        json={"warehouse_id": north, "idempotency_key": new_key(), "lines": []},
    )
    assert response.status_code == 422

    response = client.post("/api/v1/warehouses", json={"name": "   "})
    assert response.status_code == 422
    assert response.json()["error"] == "VALIDATION_ERROR"


def test_receipt_multi_line_and_rollback(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north = wh[NORTH]
    before_receipts = movement_count(client, op_type="receipt")

    response = receipt(
        client,
        north,
        [(p["SKU-101"], 2), (p["SKU-102"], 3), (p["SKU-103"], 1)],
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["lines"]) == 3
    assert balance(client, "SKU-103", north)["on_hand"] == 1
    assert movement_count(client, op_type="receipt") == before_receipts + 3
    after_success = stock(client)

    # Вторая строка не проходит — первая не должна примениться.
    response = receipt(client, north, [(p["SKU-101"], 5), (999999, 1)])
    assert response.status_code == 422
    assert (
        stock(client)["SKU-101"]["totals"]["on_hand"]
        == after_success["SKU-101"]["totals"]["on_hand"]
    )
    assert movement_count(client, op_type="receipt") == before_receipts + 3


def test_reserve_exact_available_and_cancel(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north, south = wh[NORTH], wh[SOUTH]

    response = fbs(client, north, [(p["SKU-101"], 10)])
    assert response.status_code == 200, response.text
    cell = balance(client, "SKU-101", north)
    assert (cell["on_hand"], cell["reserved"], cell["available"]) == (10, 10, 0)

    response = fbs(client, north, [(p["SKU-101"], 1)])
    assert response.status_code == 409
    assert response.json()["error"] == "WAREHOUSE_SHORTAGE"

    # Отмена освобождает резерв, повторная отмена ничего не меняет.
    shipment = fbs(client, south, [(p["SKU-103"], 2)]).json()
    before = movement_count(client)
    response = client.post(f"/api/v1/fbs/{shipment['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert balance(client, "SKU-103", south)["reserved"] == 0
    after = movement_count(client)
    response = client.post(f"/api/v1/fbs/{shipment['id']}/cancel")
    assert response.status_code == 200
    assert movement_count(client) == after

    # Отправка отменённого заказа запрещена.
    response = client.post(f"/api/v1/fbs/{shipment['id']}/ship")
    assert response.status_code == 409
    assert response.json()["error"] == "INVALID_STATE"

    # Отмена после отправки запрещена.
    shipped = fbs(client, north, [(p["SKU-102"], 1)]).json()
    client.post(f"/api/v1/fbs/{shipped['id']}/ship")
    response = client.post(f"/api/v1/fbs/{shipped['id']}/cancel")
    assert response.status_code == 409
    assert response.json()["error"] == "INVALID_STATE"
    assert before > 0


def test_transfer_validation_and_archived_destination(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north, south = wh[NORTH], wh[SOUTH]

    response = transfer(client, north, north, [(p["SKU-101"], 1)])
    assert response.status_code == 422
    assert response.json()["error"] == "VALIDATION_ERROR"

    response = transfer(client, north, south, [(p["SKU-101"], 11)])
    assert response.status_code == 409
    assert response.json()["error"] == "WAREHOUSE_SHORTAGE"

    # Нельзя отправить зарезервированное.
    fbs(client, north, [(p["SKU-101"], 8)])
    response = transfer(client, north, south, [(p["SKU-101"], 3)])
    assert response.status_code == 409

    # Приёмка на ФФ, архивированный уже после отправки, проходит.
    response = transfer(client, south, north, [(p["SKU-101"], 2)])
    assert response.status_code == 200
    move = response.json()
    client.post(f"/api/v1/warehouses/{north}/archive")
    response = client.post(f"/api/v1/transfers/{move['id']}/receive")
    assert response.status_code == 200, response.text
    assert balance(client, "SKU-101", north)["on_hand"] == 12


def test_fbo_validation_and_double_accept(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north = wh[NORTH]

    response = fbo(client, north, [(p["SKU-102"], 6)])
    assert response.status_code == 409

    response = fbo(client, north, [(p["SKU-102"], 2)])
    assert response.status_code == 200
    shipment = response.json()
    assert balance(client, "SKU-102", north)["on_hand"] == 3

    before = movement_count(client)
    assert client.post(f"/api/v1/fbo/{shipment['id']}/accept").status_code == 200
    after_first = movement_count(client)
    assert after_first == before + 1
    assert client.post(f"/api/v1/fbo/{shipment['id']}/accept").status_code == 200
    assert movement_count(client) == after_first


def test_split_shortage(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north, south = wh[NORTH], wh[SOUTH]

    response = fbs(client, north, [(p["SKU-103"], 3)])
    assert response.status_code == 409

    response = client.post(
        "/api/v1/fbs/split",
        json={
            "idempotency_key": new_key(),
            "lines": [{"product_id": p["SKU-103"], "qty": 3}],
        },
    )
    assert response.status_code == 200, response.text
    shipments = response.json()
    assert len(shipments) == 1
    assert shipments[0]["warehouse_id"] == south
    assert shipments[0]["lines"][0]["qty"] == 3
    assert shipments[0]["external_ref"].startswith("SPLIT-")
    assert balance(client, "SKU-103", south)["reserved"] == 3

    # Раскладка по двум ФФ: сначала туда, где больше доступно.
    response = client.post(
        "/api/v1/fbs/split",
        json={
            "idempotency_key": new_key(),
            "lines": [{"product_id": p["SKU-101"], "qty": 12}],
        },
    )
    assert response.status_code == 200, response.text
    shipments = response.json()
    assert sum(item["lines"][0]["qty"] for item in shipments) == 12
    assert {item["warehouse_id"]: item["lines"][0]["qty"] for item in shipments} == {
        north: 10,
        south: 2,
    }

    # При суммарной нехватке не создаётся ничего.
    before = stock(client)
    response = client.post(
        "/api/v1/fbs/split",
        json={
            "idempotency_key": new_key(),
            "lines": [{"product_id": p["SKU-103"], "qty": 5}],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"] == "TOTAL_SHORTAGE"
    assert stock(client) == before


def test_idempotency_same_key_five_times(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north = wh[NORTH]
    key = new_key()
    before = movement_count(client)

    documents = [receipt(client, north, [(p["SKU-101"], 1)], key=key) for _ in range(5)]
    assert all(response.status_code == 200 for response in documents)
    assert len({response.json()["id"] for response in documents}) == 1
    assert movement_count(client) == before + 1

    with SessionLocal() as session:
        count = session.execute(
            text("SELECT count(*) FROM receipts WHERE idempotency_key = :key"), {"key": key}
        ).scalar_one()
        assert count == 1


def test_warehouse_name_uniqueness(client: TestClient) -> None:
    wh = warehouses(client)
    response = client.post("/api/v1/warehouses", json={"name": " север "})
    assert response.status_code == 409
    assert response.json()["error"] == "NAME_TAKEN"

    response = client.post(f"/api/v1/warehouses/{wh[SOUTH]}/rename", json={"name": "СЕВЕР"})
    assert response.status_code == 409

    client.post(f"/api/v1/warehouses/{wh[NORTH]}/archive")
    response = client.post("/api/v1/warehouses", json={"name": "Север"})
    assert response.status_code == 200


def test_journal_is_append_only(client: TestClient) -> None:
    with SessionLocal() as session:
        with pytest.raises(DBAPIError):
            session.execute(text("UPDATE stock_movements SET qty = qty + 1"))
            session.commit()
        session.rollback()

    with SessionLocal() as session:
        with pytest.raises(DBAPIError):
            session.execute(text("DELETE FROM stock_movements"))
            session.commit()
        session.rollback()

    assert movement_count(client) == 5


def test_history_filters(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north = wh[NORTH]

    fbs(client, north, [(p["SKU-101"], 2)])
    transfer(client, north, wh[SOUTH], [(p["SKU-102"], 1)])

    by_sku = history(client, sku="SKU-101")
    assert by_sku["count"] == 4
    assert {item["sku"] for item in by_sku["items"]} == {"SKU-101"}

    by_type = history(client, op_type="reserve")
    assert by_type["count"] == 1
    assert by_type["items"][0]["op_type"] == "reserve"

    by_warehouse = history(client, warehouse_id=north)
    assert by_warehouse["count"] >= 1
    assert all(
        item["from_warehouse_id"] == north or item["to_warehouse_id"] == north
        for item in by_warehouse["items"]
    )
