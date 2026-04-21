"""Pydantic I/O schemas for REST and WebSocket responses."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)


class WebhookResponse(BaseModel):
    status: str
    signal_id: Optional[int] = None
    reason: Optional[str] = None


class SignalOut(_Out):
    id: int
    received_at: datetime
    signal_time: Optional[datetime] = None
    raw_action: str
    order_side: Optional[str] = None
    position_action: Optional[str] = None
    direction: Optional[str] = None
    symbol: str
    close_price: Optional[float] = None
    interval: Optional[str] = None
    strategy: str
    qty: Optional[int] = None
    status: str
    reject_reason: Optional[str] = None
    parse_format: str


class OrderOut(_Out):
    id: int
    signal_id: int
    parent_order_id: Optional[int] = None
    trail_order_id: Optional[int] = None
    ibkr_order_id: Optional[int] = None
    symbol: str
    action: str
    qty: int
    order_type: str
    trail_amount: Optional[float] = None
    trail_stop_price: Optional[float] = None
    direction: Optional[str] = None
    order_role: str
    status: str
    fill_qty: int
    fill_price: Optional[float] = None
    fill_time: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    error_msg: Optional[str] = None
    replaced_by_signal_id: Optional[int] = None
    replaced_order_id: Optional[int] = None
    signal_close_price: Optional[float] = None
    fill_deviation_pts: Optional[float] = None
    fill_deviation_pct: Optional[float] = None
    interval: Optional[str] = None
    created_at: datetime


class PositionOut(_Out):
    id: int
    symbol: str
    direction: str
    interval: str
    qty: int
    avg_cost: Optional[float] = None
    market_price: Optional[float] = None
    market_value: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    realized_pnl: Optional[float] = None
    last_updated: datetime
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    trail_order_id: Optional[int] = None
    trail_amount: Optional[float] = None
    trail_stop_price: Optional[float] = None


class AccountOut(_Out):
    net_liquidation: Optional[float] = None
    total_cash: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    realized_pnl: Optional[float] = None
    equity_with_loan: Optional[float] = None
    snapshot_time: Optional[datetime] = None


class StatusOut(BaseModel):
    server: str
    tws_connected: bool
    tws_last_connected: Optional[datetime] = None
    tws_disconnect_reason: Optional[str] = None
    uptime_seconds: int
    signals_today: int
    orders_today: int
    open_positions: int
    accepting_signals: bool = True
    maintenance_mode: str = "normal"  # "normal" | "pre_close" | "maintenance"
    maintenance_message: str = ""
    maintenance_resumes_at: Optional[str] = None
    active_intervals: list[str] = []


class SlippageByInterval(BaseModel):
    interval: str
    total_fills: int
    avg_deviation_pts: float
    avg_deviation_pct: float
    max_deviation_pts: float
    min_deviation_pts: float


class SlippageOut(BaseModel):
    filters: dict[str, Any]
    total_fills: int
    avg_deviation_pts: float
    avg_deviation_pct: float
    max_deviation_pts: float
    min_deviation_pts: float
    pct_within_0_1: float
    pct_within_0_5: float
    pct_within_1_0: float
    pct_over_1_0: float
    by_interval: list[SlippageByInterval]


class WebSocketEvent(BaseModel):
    type: str
    data: dict[str, Any]
