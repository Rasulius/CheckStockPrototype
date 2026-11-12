from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import SessionLocal
from seed import seed_database
from tests.helpers import (
    NORTH,
    SOUTH,
    balance,
    fbo,
    fbs,
    movement_count,
    products,
    receipt,
    stock,
    transfer,
    warehouses,
)


def test_e1_seed_is_idempotent(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    data = stock(client)

    assert balance(client, "SKU-101", wh[NORTH])["on_hand"] == 10
    assert balance(client, "SKU-101", wh[SOUTH])["on_hand"] == 6
    assert balance(client, "SKU-102", wh[NORTH])["on_hand"] == 5
    assert balance(client, "SKU-102", wh[SOUTH])["on_hand"] == 0
    assert balance(client, "SKU-103", wh[NORTH])["on_hand"] == 0
    assert balance(client, "SKU-103", wh[SOUTH])["on_hand"] == 4
    assert data["SKU-101"]["totals"]["on_hand"] == 16
    assert movement_count(client, op_type="receipt") == 5

    with SessionLocal() as session:
        seed_database(session)
        seed_database(session)

    assert movement_count(client, op_type="receipt") == 5
    assert stock(client)["SKU-101"]["totals"]["on_hand"] == 16
    assert p["SKU-101"] > 0


def test_e2_e11_full_scenario(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north, south = wh[NORTH], wh[SOUTH]

    # E2 — резерв FBS 101 × 3 на Севере
    response = fbs(client, north, [(p["SKU-101"], 3)], external_ref="WB-1001")
    assert response.status_code == 200, response.text
    shipment = response.json()
    assert shipment["status"] == "reserved"
    cell = balance(client, "SKU-101", north)
    assert (cell["on_hand"], cell["reserved"], cell["available"]) == (10, 3, 7)

    # E3 — отправка этого FBS
    response = client.post(f"/api/v1/fbs/{shipment['id']}/ship")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "shipped"
    cell = balance(client, "SKU-101", north)
    assert (cell["on_hand"], cell["reserved"]) == (7, 0)
    transit = client.get("/api/v1/stock").json()["in_transit"]["fbs"]
    assert sum(line["qty"] for item in transit for line in item["lines"]) == 3

    # E4 — повтор отправки: 200, без новых проводок
    before = movement_count(client)
    response = client.post(f"/api/v1/fbs/{shipment['id']}/ship")
    assert response.status_code == 200
    assert response.json()["status"] == "shipped"
    assert movement_count(client) == before

    # E5 — перемещение 101 × 4 Север → Юг
    response = transfer(client, north, south, [(p["SKU-101"], 4)])
    assert response.status_code == 200, response.text
    move = response.json()
    assert move["status"] == "in_transit"
    assert balance(client, "SKU-101", north)["on_hand"] == 3
    assert balance(client, "SKU-101", south)["on_hand"] == 6
    assert client.get("/api/v1/stock").json()["in_transit"]["transfers"][0]["lines"][0]["qty"] == 4

    # E6 — приёмка перемещения
    response = client.post(f"/api/v1/transfers/{move['id']}/receive")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "received"
    assert balance(client, "SKU-101", south)["on_hand"] == 10
    assert client.get("/api/v1/stock").json()["in_transit"]["transfers"] == []

    # E7 — повтор приёмки
    response = client.post(f"/api/v1/transfers/{move['id']}/receive")
    assert response.status_code == 200
    assert response.json()["status"] == "received"
    assert balance(client, "SKU-101", south)["on_hand"] == 10

    # E8 — FBO 102 × 2 с Севера
    response = fbo(client, north, [(p["SKU-102"], 2)], external_ref="Коледино")
    assert response.status_code == 200, response.text
    fbo_doc = response.json()
    assert fbo_doc["status"] == "shipped"
    assert balance(client, "SKU-102", north)["on_hand"] == 3
    assert client.get("/api/v1/stock").json()["in_transit"]["fbo"][0]["lines"][0]["qty"] == 2

    # E9 — приёмка FBO
    response = client.post(f"/api/v1/fbo/{fbo_doc['id']}/accept")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    assert client.get("/api/v1/stock").json()["in_transit"]["fbo"] == []
    accepted = client.get("/api/v1/products/SKU-102").json()["locations"]
    assert any(item["place"] == "mp_warehouse" and item["qty"] == 2 for item in accepted)

    # E10 — нехватка на Севере при достаточном общем остатке
    before = stock(client)
    response = fbs(client, north, [(p["SKU-103"], 3)])
    assert response.status_code == 409
    error = response.json()
    assert error["error"] == "WAREHOUSE_SHORTAGE"
    assert "На ФФ «Север» доступно 0, запрошено 3. Суммарно по ФФ доступно 4." == error["message"]
    assert error["details"]["alternatives"][0]["name"] == SOUTH
    assert error["details"]["alternatives"][0]["available"] == 4
    assert stock(client) == before

    # E11 — выбор «Юг»
    response = fbs(client, south, [(p["SKU-103"], 3)])
    assert response.status_code == 200, response.text
    cell = balance(client, "SKU-103", south)
    assert (cell["on_hand"], cell["reserved"], cell["available"]) == (4, 3, 1)


def test_e12_total_shortage(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    before = stock(client)
    response = fbs(client, wh[SOUTH], [(p["SKU-103"], 5)])
    assert response.status_code == 409
    assert response.json()["error"] == "TOTAL_SHORTAGE"
    assert stock(client) == before


def test_e13_archive_keeps_stock_and_history(client: TestClient) -> None:
    wh = warehouses(client)
    north = wh[NORTH]
    before_history = movement_count(client)
    before_stock = balance(client, "SKU-101", north)

    response = client.post(f"/api/v1/warehouses/{north}/archive")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_archived"] is True
    assert body["stats"]["on_hand"] == 15

    assert balance(client, "SKU-101", north)["on_hand"] == before_stock["on_hand"]
    assert movement_count(client) == before_history
    page = client.get("/warehouses")
    assert page.status_code == 200
    assert "архив" in page.text
    index = client.get("/")
    assert index.status_code == 200
    assert "Север" in index.text


def test_e14_archived_warehouse_rejects_new_operations(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north, south = wh[NORTH], wh[SOUTH]
    client.post(f"/api/v1/warehouses/{north}/archive")

    response = receipt(client, north, [(p["SKU-101"], 1)])
    assert response.status_code == 409
    assert response.json()["error"] == "WAREHOUSE_ARCHIVED"

    response = fbs(client, north, [(p["SKU-101"], 1)])
    assert response.status_code == 409
    assert response.json()["error"] == "WAREHOUSE_ARCHIVED"

    response = fbo(client, north, [(p["SKU-101"], 1)])
    assert response.status_code == 409
    assert response.json()["error"] == "WAREHOUSE_ARCHIVED"

    response = transfer(client, south, north, [(p["SKU-101"], 1)])
    assert response.status_code == 409
    assert response.json()["error"] == "WAREHOUSE_ARCHIVED"


def test_e15_ship_from_archived_warehouse(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north = wh[NORTH]

    response = fbs(client, north, [(p["SKU-101"], 2)])
    assert response.status_code == 200
    shipment = response.json()

    client.post(f"/api/v1/warehouses/{north}/archive")

    response = client.post(f"/api/v1/fbs/{shipment['id']}/ship")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "shipped"
    cell = balance(client, "SKU-101", north)
    assert (cell["on_hand"], cell["reserved"]) == (8, 0)


def test_e16_reuse_archived_name(client: TestClient) -> None:
    wh = warehouses(client)
    north = wh[NORTH]
    client.post(f"/api/v1/warehouses/{north}/archive")

    response = client.post("/api/v1/warehouses", json={"name": "Север"})
    assert response.status_code == 200, response.text
    new_north = response.json()
    assert new_north["id"] != north
    assert new_north["is_archived"] is False

    data = stock(client)
    balances = data["SKU-101"]["balances"]
    assert balances[north]["on_hand"] == 10
    assert balances[new_north["id"]]["on_hand"] == 0
