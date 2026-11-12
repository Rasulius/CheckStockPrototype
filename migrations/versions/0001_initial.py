"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE_SQL = """
CREATE TABLE shops (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE warehouses (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    is_archived BOOLEAN NOT NULL DEFAULT false,
    archived_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX ux_wh_active_name
    ON warehouses (lower(btrim(name))) WHERE NOT is_archived;

CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE stock_balances (
    product_id INT NOT NULL REFERENCES products(id),
    warehouse_id INT NOT NULL REFERENCES warehouses(id),
    on_hand INT NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
    reserved INT NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (product_id, warehouse_id),
    CHECK (reserved <= on_hand)
);

CREATE TABLE receipts (
    id SERIAL PRIMARY KEY,
    warehouse_id INT NOT NULL REFERENCES warehouses(id),
    idempotency_key UUID NOT NULL UNIQUE,
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE receipt_lines (
    id SERIAL PRIMARY KEY,
    receipt_id INT NOT NULL REFERENCES receipts(id),
    product_id INT NOT NULL REFERENCES products(id),
    qty INT NOT NULL CHECK (qty > 0),
    UNIQUE (receipt_id, product_id)
);

CREATE TABLE transfers (
    id SERIAL PRIMARY KEY,
    from_warehouse_id INT NOT NULL REFERENCES warehouses(id),
    to_warehouse_id INT NOT NULL REFERENCES warehouses(id),
    status TEXT NOT NULL CHECK (status IN ('in_transit','received')),
    idempotency_key UUID NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    received_at TIMESTAMPTZ,
    CHECK (from_warehouse_id <> to_warehouse_id)
);

CREATE TABLE transfer_lines (
    id SERIAL PRIMARY KEY,
    transfer_id INT NOT NULL REFERENCES transfers(id),
    product_id INT NOT NULL REFERENCES products(id),
    qty INT NOT NULL CHECK (qty > 0),
    UNIQUE (transfer_id, product_id)
);

CREATE TABLE shipments (
    id SERIAL PRIMARY KEY,
    shop_id INT NOT NULL REFERENCES shops(id),
    kind TEXT NOT NULL CHECK (kind IN ('FBS','FBO')),
    warehouse_id INT NOT NULL REFERENCES warehouses(id),
    status TEXT NOT NULL,
    external_ref TEXT,
    idempotency_key UUID NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    shipped_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    CHECK ((kind='FBS' AND status IN ('reserved','shipped','cancelled'))
        OR (kind='FBO' AND status IN ('shipped','accepted')))
);

CREATE TABLE shipment_lines (
    id SERIAL PRIMARY KEY,
    shipment_id INT NOT NULL REFERENCES shipments(id),
    product_id INT NOT NULL REFERENCES products(id),
    qty INT NOT NULL CHECK (qty > 0),
    UNIQUE (shipment_id, product_id)
);

CREATE TABLE stock_movements (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    op_type TEXT NOT NULL CHECK (op_type IN (
        'receipt','reserve','unreserve','fbs_ship',
        'transfer_out','transfer_in','fbo_ship','fbo_accept')),
    product_id INT NOT NULL REFERENCES products(id),
    qty INT NOT NULL CHECK (qty > 0),
    from_place TEXT NOT NULL,
    from_warehouse_id INT REFERENCES warehouses(id),
    to_place TEXT NOT NULL,
    to_warehouse_id INT REFERENCES warehouses(id),
    doc_type TEXT NOT NULL,
    doc_id INT NOT NULL
);

CREATE INDEX ix_mov_product ON stock_movements (product_id, created_at DESC);
CREATE INDEX ix_mov_doc ON stock_movements (doc_type, doc_id);

CREATE OR REPLACE FUNCTION forbid_stock_movements_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'stock_movements is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_stock_movements_append_only
BEFORE UPDATE OR DELETE ON stock_movements
FOR EACH ROW EXECUTE FUNCTION forbid_stock_movements_mutation();

CREATE OR REPLACE VIEW v_product_locations AS
SELECT product_id, 'free'::text AS place, warehouse_id,
       (on_hand - reserved) AS qty, NULL::text AS doc_type, NULL::int AS doc_id
FROM stock_balances
WHERE on_hand - reserved > 0
UNION ALL
SELECT product_id, 'reserved'::text, warehouse_id,
       reserved, NULL::text, NULL::int
FROM stock_balances
WHERE reserved > 0
UNION ALL
SELECT tl.product_id, 'transfer_transit'::text, t.from_warehouse_id,
       tl.qty, 'transfer'::text, t.id
FROM transfers t
JOIN transfer_lines tl ON tl.transfer_id = t.id
WHERE t.status = 'in_transit'
UNION ALL
SELECT sl.product_id, 'fbo_transit'::text, s.warehouse_id,
       sl.qty, 'shipment'::text, s.id
FROM shipments s
JOIN shipment_lines sl ON sl.shipment_id = s.id
WHERE s.kind = 'FBO' AND s.status = 'shipped'
UNION ALL
SELECT sl.product_id, 'fbs_transit'::text, s.warehouse_id,
       sl.qty, 'shipment'::text, s.id
FROM shipments s
JOIN shipment_lines sl ON sl.shipment_id = s.id
WHERE s.kind = 'FBS' AND s.status = 'shipped'
UNION ALL
SELECT sl.product_id, 'mp_warehouse'::text, NULL::int,
       sl.qty, 'shipment'::text, s.id
FROM shipments s
JOIN shipment_lines sl ON sl.shipment_id = s.id
WHERE s.kind = 'FBO' AND s.status = 'accepted';
"""

DOWNGRADE_SQL = """
DROP VIEW IF EXISTS v_product_locations;
DROP TRIGGER IF EXISTS trg_stock_movements_append_only ON stock_movements;
DROP FUNCTION IF EXISTS forbid_stock_movements_mutation();
DROP TABLE IF EXISTS stock_movements;
DROP TABLE IF EXISTS shipment_lines;
DROP TABLE IF EXISTS shipments;
DROP TABLE IF EXISTS transfer_lines;
DROP TABLE IF EXISTS transfers;
DROP TABLE IF EXISTS receipt_lines;
DROP TABLE IF EXISTS receipts;
DROP TABLE IF EXISTS stock_balances;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS warehouses;
DROP TABLE IF EXISTS shops;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
