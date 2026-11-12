from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

NORTH = "Север"
SOUTH = "Юг"


def new_key() -> str:
    return str(uuid4())


def warehouses(client: TestClient) -> dict[str, int]:
    response = client.get("/api/v1/warehouses")
    assert response.status_code == 200
    return {item["name"]: item["id"] for item in response.json()}


def products(client: TestClient) -> dict[str, int]:
    response = client.get("/api/v1/products")
    assert response.status_code == 200
    return {item["sku"]: item["id"] for item in response.json()}


def stock(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/api/v1/stock")
    assert response.status_code == 200
    result: dict[str, dict[str, Any]] = {}
    for product in response.json()["products"]:
        result[product["sku"]] = {
            "totals": product["totals"],
            "balances": {item["warehouse_id"]: item for item in product["balances"]},
        }
    return result


def balance(client: TestClient, sku: str, warehouse_id: int) -> dict[str, int]:
    return stock(client)[sku]["balances"][warehouse_id]


def receipt(
    client: TestClient,
    warehouse_id: int,
    lines: list[tuple[int, int]],
    key: str | None = None,
):
    return client.post(
        "/api/v1/receipts",
        json={
            "warehouse_id": warehouse_id,
            "idempotency_key": key or new_key(),
            "lines": [{"product_id": product_id, "qty": qty} for product_id, qty in lines],
        },
    )


def transfer(
    client: TestClient,
    from_id: int,
    to_id: int,
    lines: list[tuple[int, int]],
    key: str | None = None,
):
    return client.post(
        "/api/v1/transfers",
        json={
            "from_warehouse_id": from_id,
            "to_warehouse_id": to_id,
            "idempotency_key": key or new_key(),
            "lines": [{"product_id": product_id, "qty": qty} for product_id, qty in lines],
        },
    )


def fbs(
    client: TestClient,
    warehouse_id: int,
    lines: list[tuple[int, int]],
    key: str | None = None,
    external_ref: str | None = None,
):
    return client.post(
        "/api/v1/fbs",
        json={
            "warehouse_id": warehouse_id,
            "idempotency_key": key or new_key(),
            "external_ref": external_ref,
            "lines": [{"product_id": product_id, "qty": qty} for product_id, qty in lines],
        },
    )


def fbo(
    client: TestClient,
    warehouse_id: int,
    lines: list[tuple[int, int]],
    key: str | None = None,
    external_ref: str | None = None,
):
    return client.post(
        "/api/v1/fbo",
        json={
            "warehouse_id": warehouse_id,
            "idempotency_key": key or new_key(),
            "external_ref": external_ref,
            "lines": [{"product_id": product_id, "qty": qty} for product_id, qty in lines],
        },
    )


def history(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get("/api/v1/history", params=params)
    assert response.status_code == 200
    return response.json()


def movement_count(client: TestClient, **params: Any) -> int:
    return history(client, **params)["count"]
