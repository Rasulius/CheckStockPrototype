from __future__ import annotations

from sqlalchemy import text

from .db import engine

SQLITE_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK (trim(name) <> ''),
    is_archived BOOLEAN NOT NULL DEFAULT 0,
    archived_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_wh_active_name
    ON warehouses (lower(trim(name))) WHERE NOT is_archived;

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_balances (
    product_id INTEGER NOT NULL REFERENCES products(id),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    on_hand INTEGER NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
    reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (product_id, warehouse_id),
    CHECK (reserved <= on_hand)
);

CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS receipt_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    UNIQUE (receipt_id, product_id)
);

CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    to_warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    status TEXT NOT NULL CHECK (status IN ('in_transit','received')),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    received_at TEXT,
    CHECK (from_warehouse_id <> to_warehouse_id)
);

CREATE TABLE IF NOT EXISTS transfer_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id INTEGER NOT NULL REFERENCES transfers(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    UNIQUE (transfer_id, product_id)
);

CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    kind TEXT NOT NULL CHECK (kind IN ('FBS','FBO')),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    status TEXT NOT NULL,
    external_ref TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    shipped_at TEXT,
    closed_at TEXT,
    CHECK ((kind='FBS' AND status IN ('reserved','shipped','cancelled'))
        OR (kind='FBO' AND status IN ('shipped','accepted')))
);

CREATE TABLE IF NOT EXISTS shipment_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id INTEGER NOT NULL REFERENCES shipments(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    UNIQUE (shipment_id, product_id)
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    op_type TEXT NOT NULL CHECK (op_type IN (
        'receipt','reserve','unreserve','fbs_ship',
        'transfer_out','transfer_in','fbo_ship','fbo_accept')),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    from_place TEXT NOT NULL,
    from_warehouse_id INTEGER REFERENCES warehouses(id),
    to_place TEXT NOT NULL,
    to_warehouse_id INTEGER REFERENCES warehouses(id),
    doc_type TEXT NOT NULL,
    doc_id INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_mov_product ON stock_movements (product_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_mov_doc ON stock_movements (doc_type, doc_id);

DROP TRIGGER IF EXISTS trg_stock_movements_no_update;
CREATE TRIGGER trg_stock_movements_no_update
BEFORE UPDATE ON stock_movements
BEGIN
    SELECT RAISE(ABORT, 'stock_movements is append-only');
END;

DROP TRIGGER IF EXISTS trg_stock_movements_no_delete;
CREATE TRIGGER trg_stock_movements_no_delete
BEFORE DELETE ON stock_movements
BEGIN
    SELECT RAISE(ABORT, 'stock_movements is append-only');
END;

DROP VIEW IF EXISTS v_product_locations;
CREATE VIEW v_product_locations AS
SELECT product_id, 'free' AS place, warehouse_id,
       (on_hand - reserved) AS qty, NULL AS doc_type, NULL AS doc_id
FROM stock_balances
WHERE on_hand - reserved > 0
UNION ALL
SELECT product_id, 'reserved', warehouse_id,
       reserved, NULL, NULL
FROM stock_balances
WHERE reserved > 0
UNION ALL
SELECT tl.product_id, 'transfer_transit', t.from_warehouse_id,
       tl.qty, 'transfer', t.id
FROM transfers t
JOIN transfer_lines tl ON tl.transfer_id = t.id
WHERE t.status = 'in_transit'
UNION ALL
SELECT sl.product_id, 'fbo_transit', s.warehouse_id,
       sl.qty, 'shipment', s.id
FROM shipments s
JOIN shipment_lines sl ON sl.shipment_id = s.id
WHERE s.kind = 'FBO' AND s.status = 'shipped'
UNION ALL
SELECT sl.product_id, 'fbs_transit', s.warehouse_id,
       sl.qty, 'shipment', s.id
FROM shipments s
JOIN shipment_lines sl ON sl.shipment_id = s.id
WHERE s.kind = 'FBS' AND s.status = 'shipped'
UNION ALL
SELECT sl.product_id, 'mp_warehouse', NULL,
       sl.qty, 'shipment', s.id
FROM shipments s
JOIN shipment_lines sl ON sl.shipment_id = s.id
WHERE s.kind = 'FBO' AND s.status = 'accepted';
"""


def init_sqlite_db() -> None:
    raw = engine.raw_connection()
    try:
        raw.executescript(SQLITE_SCHEMA_SQL)
        raw.commit()
    finally:
        raw.close()
