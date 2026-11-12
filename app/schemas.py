from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class LineIn(BaseModel):
    product_id: int
    qty: int = Field(gt=0)


class ReceiptIn(BaseModel):
    warehouse_id: int
    idempotency_key: UUID
    comment: str | None = None
    lines: list[LineIn] = Field(min_length=1)


class TransferIn(BaseModel):
    from_warehouse_id: int
    to_warehouse_id: int
    idempotency_key: UUID
    lines: list[LineIn] = Field(min_length=1)


class ShipmentIn(BaseModel):
    warehouse_id: int
    idempotency_key: UUID
    external_ref: str | None = None
    lines: list[LineIn] = Field(min_length=1)


class SplitIn(BaseModel):
    idempotency_key: UUID
    lines: list[LineIn] = Field(min_length=1)


class WarehouseIn(BaseModel):
    name: str


class RenameIn(BaseModel):
    name: str


class TransitionIn(BaseModel):
    idempotency_key: UUID | None = None
