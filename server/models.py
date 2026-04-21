"""SQLAlchemy ORM models matching the PRD DATA MODEL spec exactly."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Float,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())
    signal_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    raw_action: Mapped[str] = mapped_column(String, nullable=False)
    order_side: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    position_action: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    direction: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    close_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    interval: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    strategy: Mapped[str] = mapped_column(String, default="ldc", nullable=False)
    qty: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reject_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    dedup_of: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("signals.id"), nullable=True)
    parse_format: Mapped[str] = mapped_column(String, nullable=False)
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    source_ip: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_signals_symbol_received_at", "symbol", "received_at"),
        Index("ix_signals_raw_action", "raw_action"),
        Index("ix_signals_status", "status"),
        Index("ix_signals_strategy", "strategy"),
    )


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(Integer, ForeignKey("signals.id"), nullable=False)
    parent_order_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=True
    )
    trail_order_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=True
    )
    ibkr_order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)  # BUY / SELL
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    order_type: Mapped[str] = mapped_column(String, nullable=False)  # MKT / TRAIL
    trail_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trail_stop_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    direction: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # long / short
    order_role: Mapped[str] = mapped_column(String, nullable=False)  # entry / exit / trail_stop
    status: Mapped[str] = mapped_column(String, nullable=False)
    fill_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fill_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fill_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    replaced_by_signal_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("signals.id"), nullable=True
    )
    replaced_order_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=True
    )
    signal_close_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fill_deviation_pts: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fill_deviation_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    interval: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())

    __table_args__ = (
        Index("ix_orders_signal_id", "signal_id"),
        Index("ix_orders_symbol_status", "symbol", "status"),
        Index("ix_orders_ibkr_order_id", "ibkr_order_id"),
        Index("ix_orders_parent_order_id", "parent_order_id"),
        Index("ix_orders_order_role", "order_role"),
    )


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), nullable=False)
    ibkr_exec_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ibkr_order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fill_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    fill_price: Mapped[float] = mapped_column(Float, nullable=False)
    fill_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    commission: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exchange: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())

    __table_args__ = (
        Index("ix_fills_order_id", "order_id"),
        Index("ix_fills_fill_time", "fill_time"),
    )


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    interval: Mapped[str] = mapped_column(String, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    market_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    market_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0)
    last_updated: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("symbol", "direction", "interval", name="uq_positions_symbol_dir_interval"),
        Index("ix_positions_symbol", "symbol"),
        Index("ix_positions_interval", "interval"),
        Index("ix_positions_qty", "qty"),
    )


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())
    net_liquidation: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_cash: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    day_trades_remaining: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    equity_with_loan: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


__all__ = ["Signal", "Order", "Fill", "Position", "AccountSnapshot"]
