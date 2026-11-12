from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class Warehouse(Base):
    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_archived: Mapped[bool] = mapped_column(default=False, server_default="false")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (CheckConstraint("btrim(name) <> ''", name="ck_warehouses_name"),)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class StockBalance(Base):
    __tablename__ = "stock_balances"

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), primary_key=True
    )
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    product: Mapped[Product] = relationship()
    warehouse: Mapped[Warehouse] = relationship()

    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="ck_balance_on_hand"),
        CheckConstraint("reserved >= 0", name="ck_balance_reserved"),
        CheckConstraint("reserved <= on_hand", name="ck_balance_reserved_le_on_hand"),
    )


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    warehouse: Mapped[Warehouse] = relationship()
    lines: Mapped[list[ReceiptLine]] = relationship(
        back_populates="receipt", cascade="all, delete-orphan", order_by="ReceiptLine.id"
    )


class ReceiptLine(Base):
    __tablename__ = "receipt_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)

    receipt: Mapped[Receipt] = relationship(back_populates="lines")
    product: Mapped[Product] = relationship()

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_receipt_lines_qty"),
        UniqueConstraint("receipt_id", "product_id", name="ux_receipt_lines_product"),
    )


class Transfer(Base):
    __tablename__ = "transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    to_warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    from_warehouse: Mapped[Warehouse] = relationship(foreign_keys=[from_warehouse_id])
    to_warehouse: Mapped[Warehouse] = relationship(foreign_keys=[to_warehouse_id])
    lines: Mapped[list[TransferLine]] = relationship(
        back_populates="transfer", cascade="all, delete-orphan", order_by="TransferLine.id"
    )

    __table_args__ = (
        CheckConstraint("status IN ('in_transit','received')", name="ck_transfers_status"),
        CheckConstraint("from_warehouse_id <> to_warehouse_id", name="ck_transfers_different"),
    )


class TransferLine(Base):
    __tablename__ = "transfer_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transfer_id: Mapped[int] = mapped_column(ForeignKey("transfers.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)

    transfer: Mapped[Transfer] = relationship(back_populates="lines")
    product: Mapped[Product] = relationship()

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_transfer_lines_qty"),
        UniqueConstraint("transfer_id", "product_id", name="ux_transfer_lines_product"),
    )


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    external_ref: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    shop: Mapped[Shop] = relationship()
    warehouse: Mapped[Warehouse] = relationship()
    lines: Mapped[list[ShipmentLine]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan", order_by="ShipmentLine.id"
    )

    __table_args__ = (
        CheckConstraint(
            "(kind='FBS' AND status IN ('reserved','shipped','cancelled')) "
            "OR (kind='FBO' AND status IN ('shipped','accepted'))",
            name="ck_shipments_kind_status",
        ),
    )


class ShipmentLine(Base):
    __tablename__ = "shipment_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)

    shipment: Mapped[Shipment] = relationship(back_populates="lines")
    product: Mapped[Product] = relationship()

    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_shipment_lines_qty"),
        UniqueConstraint("shipment_id", "product_id", name="ux_shipment_lines_product"),
    )


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    op_type: Mapped[str] = mapped_column(Text, nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    from_place: Mapped[str] = mapped_column(Text, nullable=False)
    from_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    to_place: Mapped[str] = mapped_column(Text, nullable=False)
    to_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)
    doc_id: Mapped[int] = mapped_column(Integer, nullable=False)

    product: Mapped[Product] = relationship()
    from_warehouse: Mapped[Warehouse | None] = relationship(foreign_keys=[from_warehouse_id])
    to_warehouse: Mapped[Warehouse | None] = relationship(foreign_keys=[to_warehouse_id])

    __table_args__ = (
        CheckConstraint(
            "op_type IN ('receipt','reserve','unreserve','fbs_ship',"
            "'transfer_out','transfer_in','fbo_ship','fbo_accept')",
            name="ck_movements_op_type",
        ),
        CheckConstraint("qty > 0", name="ck_movements_qty"),
        Index("ix_mov_product", "product_id", "created_at"),
        Index("ix_mov_doc", "doc_type", "doc_id"),
    )
