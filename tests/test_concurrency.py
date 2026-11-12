from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal
from tests.helpers import (
    NORTH,
    SOUTH,
    balance,
    fbs,
    movement_count,
    new_key,
    products,
    receipt,
    transfer,
    warehouses,
)


def test_same_idempotency_key_parallel(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north = wh[NORTH]
    key = new_key()
    before = movement_count(client)

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(
            pool.map(
                lambda _: receipt(client, north, [(p["SKU-101"], 1)], key=key),
                range(5),
            )
        )

    assert all(response.status_code == 200 for response in responses), [
        response.text for response in responses if response.status_code != 200
    ]
    assert len({response.json()["id"] for response in responses}) == 1
    assert movement_count(client) == before + 1

    with SessionLocal() as session:
        count = session.execute(
            text("SELECT count(*) FROM receipts WHERE idempotency_key = :key"),
            {"key": key},
        ).scalar_one()
        assert count == 1


def test_twenty_parallel_reserves_for_ten_units(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north = wh[NORTH]

    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(
            pool.map(
                lambda _: fbs(client, north, [(p["SKU-101"], 1)]),
                range(20),
            )
        )

    successes = [response for response in responses if response.status_code == 200]
    shortages = [response for response in responses if response.status_code == 409]
    assert len(successes) == 10
    assert len(shortages) == 10
    assert all(response.json()["error"] == "WAREHOUSE_SHORTAGE" for response in shortages)
    assert balance(client, "SKU-101", north)["reserved"] == 10


def test_ten_parallel_ships_of_one_order(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north = wh[NORTH]
    shipment = fbs(client, north, [(p["SKU-101"], 3)]).json()
    before = movement_count(client)

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(
            pool.map(
                lambda _: client.post(f"/api/v1/fbs/{shipment['id']}/ship"),
                range(10),
            )
        )

    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["status"] == "shipped" for response in responses)
    assert movement_count(client) == before + 1
    cell = balance(client, "SKU-101", north)
    assert (cell["on_hand"], cell["reserved"]) == (7, 0)


def test_opposite_transfers_do_not_deadlock(client: TestClient) -> None:
    wh = warehouses(client)
    p = products(client)
    north, south = wh[NORTH], wh[SOUTH]

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_one = pool.submit(transfer, client, north, south, [(p["SKU-101"], 1)])
        future_two = pool.submit(transfer, client, south, north, [(p["SKU-101"], 1)])
        first = future_one.result(timeout=30)
        second = future_two.result(timeout=30)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert balance(client, "SKU-101", north)["on_hand"] == 9
    assert balance(client, "SKU-101", south)["on_hand"] == 5
