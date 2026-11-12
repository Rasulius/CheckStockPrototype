from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy import text

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = "postgresql+psycopg://checkstock:checkstock@localhost:5433/checkstock"


def _test_database_url() -> str:
    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        return explicit
    base = os.getenv("DATABASE_URL")
    if not base:
        base = dotenv_values(BASE_DIR / ".env").get("DATABASE_URL") or DEFAULT_DATABASE_URL
    parts = urlsplit(base)
    return urlunsplit((parts.scheme, parts.netloc, "/checkstock_test", "", ""))


TEST_DATABASE_URL = _test_database_url()
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services.stock import assert_consistent  # noqa: E402
from seed import seed_database  # noqa: E402

TABLES = (
    "stock_movements, shipment_lines, shipments, transfer_lines, transfers, "
    "receipt_lines, receipts, stock_balances, products, warehouses, shops"
)


def _admin_url() -> str:
    parts = urlsplit(TEST_DATABASE_URL)
    return urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))


def _database_name() -> str:
    return urlsplit(TEST_DATABASE_URL).path.lstrip("/") or "checkstock_test"


def _ensure_database() -> None:
    with psycopg.connect(
        _admin_url().replace("postgresql+psycopg://", "postgresql://"), autocommit=True
    ) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (_database_name(),)
        ).fetchone()
        if not exists:
            connection.execute(f'CREATE DATABASE "{_database_name()}"')


def _migrate() -> None:
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def prepared_database():
    _ensure_database()
    _migrate()
    yield
    engine.dispose()


@pytest.fixture()
def client():
    with SessionLocal() as session:
        session.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        session.commit()
        seed_database(session)
        assert_consistent(session)

    with TestClient(app) as test_client:
        yield test_client

    with SessionLocal() as session:
        assert_consistent(session)


@pytest.fixture()
def session():
    with SessionLocal() as db_session:
        yield db_session
