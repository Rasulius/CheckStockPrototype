from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import NORTH, fbs, new_key, products, warehouses


def test_ui_inventory_columns_and_transit(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    for label in (
        "Артикул",
        "Товар",
        "Физически на складе",
        "Доступно",
        "В движении (транзит)",
        "Товар в пути между ФФ",
        "Товар в пути на склад маркетплейса (FBO)",
        "Товар в пути к клиенту (FBS)",
    ):
        assert label in page.text
    # Колонка "В резерве FBS" показывается, когда есть активные резервы.
    if "reserved" in page.text:
        pass
    north = warehouses(client)[NORTH]
    sku_101 = products(client)["SKU-101"]
    created = fbs(client, north, [(sku_101, 1)])
    assert created.status_code == 200
    page = client.get("/")
    assert "В резерве FBS" in page.text


def test_ui_delete_with_stock_warns_and_blocks(client: TestClient) -> None:
    north = warehouses(client)[NORTH]

    page = client.get(f"/warehouses/{north}/delete")
    assert page.status_code == 200
    assert "На складе ещё числится товар" in page.text
    assert "Переместить товар на другой ФФ" in page.text

    response = client.post(
        f"/warehouses/{north}/archive",
        data={"idempotency_key": new_key()},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/warehouses/{north}/delete")

    items = client.get("/api/v1/warehouses").json()
    north_item = next(item for item in items if item["id"] == north)
    assert north_item["is_archived"] is False


def test_ui_delete_empty_warehouse_archives(client: TestClient) -> None:
    created = client.post("/api/v1/warehouses", json={"name": "Пустой"})
    assert created.status_code == 200
    empty_id = created.json()["id"]

    page = client.get(f"/warehouses/{empty_id}/delete")
    assert page.status_code == 200
    assert "Остатков нет" in page.text

    response = client.post(
        f"/warehouses/{empty_id}/archive",
        data={"idempotency_key": new_key()},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/warehouses")

    items = client.get("/api/v1/warehouses").json()
    empty_item = next(item for item in items if item["id"] == empty_id)
    assert empty_item["is_archived"] is True


def test_ui_operations_shows_stock_and_availability(client: TestClient) -> None:
    page = client.get("/operations")
    assert page.status_code == 200
    assert "Склады и движения" in page.text
    assert "Новая операция" in page.text
    assert "data-availability-form" in page.text
    assert "data-warehouse" in page.text
    assert "Остаток:" in page.text
    assert "AVAILABILITY" in page.text
    assert "ops-drawer" in page.text


def test_ui_fbs_fbo_page_shows_stock_forms_and_ships_reserved(client: TestClient) -> None:
    north = warehouses(client)[NORTH]
    sku_101 = products(client)["SKU-101"]

    page = client.get("/fbs-fbo")
    assert page.status_code == 200
    assert "Резерв FBS и Отправки FBO" in page.text
    assert "Новая операция" in page.text
    assert "ops-drawer" in page.text
    assert "data-availability-form" in page.text
    assert "AVAILABILITY" in page.text

    created = fbs(client, north, [(sku_101, 2)], external_ref="WB-UI-1")
    assert created.status_code == 200
    shipment = created.json()

    page = client.get("/fbs-fbo")
    assert page.status_code == 200
    assert "Активные резервы FBS" in page.text
    assert "Отправить клиенту" in page.text
    assert "WB-UI-1" in page.text

    response = client.post(
        f"/fbs/{shipment['id']}/ship",
        data={"idempotency_key": new_key()},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/fbs-fbo")

    transit = client.get("/api/v1/stock").json()["in_transit"]["fbs"]
    assert any(item["id"] == shipment["id"] for item in transit)


def test_ui_history_shows_operation_labels_and_status(client: TestClient) -> None:
    page = client.get("/history")
    assert page.status_code == 200
    assert "Журнал движений" in page.text
    assert "Приход" in page.text
    assert "Проведено" in page.text
    assert "Статус" in page.text
