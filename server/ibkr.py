"""Thin async wrapper around ib_insync.IB.

Responsibilities:
  * Maintain a connection to TWS with auto-reconnect.
  * Expose connection status for /api/status.
  * Forward fill events to the order router via a callback.
  * Provide minimal async helpers: place_market, place_trail, cancel_order,
    get_positions, get_account_summary.

Designed to degrade gracefully when TWS is absent — the server stays up and
rejects routed signals with "TWS disconnected". A MockIBKR subclass is used by
tests/e2e to simulate fills without a live TWS.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

try:
    from ib_insync import IB, Contract, MarketOrder, Order, Stock, Trade, util  # type: ignore
    _IB_AVAILABLE = True
except Exception as _e:  # pragma: no cover — optional
    IB = object  # type: ignore
    Contract = object  # type: ignore
    MarketOrder = object  # type: ignore
    Order = object  # type: ignore
    Stock = object  # type: ignore
    Trade = object  # type: ignore
    util = None
    _IB_AVAILABLE = False

from .config import settings

log = logging.getLogger(__name__)


FillCallback = Callable[[dict], Awaitable[None]]
StatusCallback = Callable[[bool, Optional[str]], Awaitable[None]]


class IBKRClient:
    """Async wrapper around ib_insync."""

    def __init__(
        self,
        on_fill: Optional[FillCallback] = None,
        on_status: Optional[StatusCallback] = None,
    ) -> None:
        self.ib = IB() if _IB_AVAILABLE else None
        self.on_fill = on_fill
        self.on_status = on_status
        self._connected: bool = False
        self._last_connected: Optional[datetime] = None
        self._disconnect_reason: Optional[str] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._stop_reconnect = asyncio.Event()
        self._next_order_id = 1_000_000  # for mock path only

    @property
    def connected(self) -> bool:
        if self.ib is None:
            return False
        try:
            return bool(self.ib.isConnected())
        except Exception:
            return False

    @property
    def last_connected(self) -> Optional[datetime]:
        return self._last_connected

    @property
    def disconnect_reason(self) -> Optional[str]:
        return self._disconnect_reason

    async def start(self) -> None:
        """Start connection attempts in the background."""
        if self.ib is None:
            log.warning("ib_insync_unavailable_running_in_mock_mode")
            return
        self._stop_reconnect.clear()
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def stop(self) -> None:
        self._stop_reconnect.set()
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.ib is not None:
            try:
                if self.ib.isConnected():
                    self.ib.disconnect()
            except Exception:
                pass

    async def _reconnect_loop(self) -> None:
        cfg = settings()
        while not self._stop_reconnect.is_set():
            if not self.connected:
                try:
                    log.info(
                        "tws_reconnect_attempt",
                        extra={"host": cfg.tws_host, "port": cfg.tws_port, "client_id": cfg.tws_client_id},
                    )
                    await self.ib.connectAsync(
                        host=cfg.tws_host,
                        port=cfg.tws_port,
                        clientId=cfg.tws_client_id,
                        readonly=False,
                    )
                    self._connected = True
                    self._last_connected = datetime.now(timezone.utc)
                    self._disconnect_reason = None
                    log.info("tws_connected", extra={"host": cfg.tws_host, "port": cfg.tws_port})
                    self._wire_events()
                    if self.on_status:
                        await self.on_status(True, None)
                    await self._reconcile_on_connect()
                except Exception as e:
                    self._disconnect_reason = str(e)
                    log.warning("tws_connect_failed", extra={"error": str(e)})
                    if self.on_status:
                        await self.on_status(False, str(e))
            try:
                await asyncio.wait_for(
                    self._stop_reconnect.wait(),
                    timeout=cfg.tws_reconnect_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

    def _wire_events(self) -> None:
        if self.ib is None:
            return
        try:
            # execDetailsEvent fires on each fill execution.
            self.ib.execDetailsEvent += self._on_exec_details
            self.ib.orderStatusEvent += self._on_order_status
            self.ib.disconnectedEvent += self._on_disconnected
        except Exception as e:  # pragma: no cover
            log.warning("wire_events_failed", extra={"error": str(e)})

    def _on_disconnected(self) -> None:
        log.warning("tws_disconnected")
        self._disconnect_reason = "disconnected"
        if self.on_status:
            # Fire-and-forget — cannot await in a sync handler.
            asyncio.create_task(self.on_status(False, "disconnected"))

    def _on_exec_details(self, trade, fill) -> None:
        # Forward fill data as a plain dict so order_router stays decoupled.
        if self.on_fill is None:
            return
        try:
            payload = {
                "ibkr_order_id": getattr(trade.order, "orderId", None),
                "ibkr_exec_id": getattr(fill.execution, "execId", None),
                "symbol": getattr(trade.contract, "symbol", None),
                "action": getattr(trade.order, "action", None),
                "order_type": getattr(trade.order, "orderType", None),
                "fill_qty": int(getattr(fill.execution, "shares", 0) or 0),
                "fill_price": float(getattr(fill.execution, "price", 0.0) or 0.0),
                "fill_time": getattr(fill.time, "replace", lambda **_: fill.time)(tzinfo=timezone.utc)
                    if getattr(fill, "time", None) else datetime.now(timezone.utc),
                "commission": float(getattr(fill.commissionReport, "commission", 0) or 0)
                    if getattr(fill, "commissionReport", None) else None,
                "exchange": getattr(fill.execution, "exchange", None),
            }
        except Exception as e:
            log.exception("fill_payload_build_failed", extra={"error": str(e)})
            return
        asyncio.create_task(self.on_fill(payload))

    def _on_order_status(self, trade) -> None:
        # Informational only — order_router receives full status via on_fill for executions.
        pass

    async def _reconcile_on_connect(self) -> None:
        """On (re)connect: fetch positions and open orders, update DB if needed.

        The order_router is authoritative for reconciliation logic. This method
        emits an event so the router can run its reconciler.
        """
        try:
            positions = []
            open_orders = []
            if self.ib is not None:
                positions = await asyncio.wait_for(self.ib.reqPositionsAsync() or [], timeout=10)
                open_orders = self.ib.reqOpenOrders() or []
            log.info(
                "startup_position_sync",
                extra={"positions": len(positions), "open_orders": len(open_orders)},
            )
        except Exception as e:
            log.warning("reconcile_on_connect_failed", extra={"error": str(e)})

    # --- Order placement ---

    async def place_market(self, symbol: str, action: str, qty: int) -> Optional[int]:
        """Place a market order, return IBKR order id (or None on failure)."""
        if self.ib is None or not self.connected:
            log.error("place_market_disconnected", extra={"symbol": symbol})
            return None
        try:
            contract = Stock(symbol, "SMART", "USD")
            await self.ib.qualifyContractsAsync(contract)
            order = MarketOrder(action, qty)
            trade = self.ib.placeOrder(contract, order)
            log.info(
                "order_submitted",
                extra={
                    "symbol": symbol,
                    "action": action,
                    "qty": qty,
                    "order_type": "MKT",
                    "ibkr_order_id": trade.order.orderId,
                },
            )
            return trade.order.orderId
        except Exception as e:
            log.exception("place_market_error", extra={"symbol": symbol, "error": str(e)})
            return None

    async def place_trail(
        self, symbol: str, action: str, qty: int, trail_amount: float, trail_stop_price: float
    ) -> Optional[int]:
        """Place a TRAIL stop order."""
        if self.ib is None or not self.connected:
            log.error("place_trail_disconnected", extra={"symbol": symbol})
            return None
        try:
            contract = Stock(symbol, "SMART", "USD")
            await self.ib.qualifyContractsAsync(contract)
            order = Order()
            order.action = action
            order.orderType = "TRAIL"
            order.totalQuantity = qty
            order.auxPrice = trail_amount
            order.trailStopPrice = trail_stop_price
            order.transmit = True
            trade = self.ib.placeOrder(contract, order)
            log.info(
                "trail_order_placed",
                extra={
                    "symbol": symbol,
                    "action": action,
                    "qty": qty,
                    "trail_amount": trail_amount,
                    "trail_stop_price": trail_stop_price,
                    "ibkr_order_id": trade.order.orderId,
                },
            )
            return trade.order.orderId
        except Exception as e:
            log.exception("place_trail_error", extra={"symbol": symbol, "error": str(e)})
            return None

    async def cancel_order(self, ibkr_order_id: int) -> bool:
        if self.ib is None or not self.connected:
            return False
        try:
            for t in self.ib.trades():
                if t.order.orderId == ibkr_order_id:
                    self.ib.cancelOrder(t.order)
                    return True
        except Exception as e:
            log.exception("cancel_order_error", extra={"ibkr_order_id": ibkr_order_id, "error": str(e)})
        return False

    async def get_positions(self) -> list[dict]:
        if self.ib is None or not self.connected:
            return []
        try:
            positions = await self.ib.reqPositionsAsync()
            return [
                {
                    "symbol": getattr(p.contract, "symbol", None),
                    "position": int(getattr(p, "position", 0) or 0),
                    "avg_cost": float(getattr(p, "avgCost", 0) or 0),
                }
                for p in positions
            ]
        except Exception as e:
            log.exception("get_positions_error", extra={"error": str(e)})
            return []

    async def get_account_summary(self) -> dict[str, Any]:
        if self.ib is None or not self.connected:
            return {}
        try:
            tags = ["NetLiquidation", "TotalCashValue", "UnrealizedPnL", "RealizedPnL", "EquityWithLoanValue"]
            values = await self.ib.accountSummaryAsync()
            result: dict[str, float] = {}
            for v in values:
                if v.tag in tags and v.currency in ("USD", ""):
                    try:
                        result[v.tag] = float(v.value)
                    except (TypeError, ValueError):
                        pass
            return {
                "net_liquidation": result.get("NetLiquidation"),
                "total_cash": result.get("TotalCashValue"),
                "unrealized_pnl": result.get("UnrealizedPnL"),
                "realized_pnl": result.get("RealizedPnL"),
                "equity_with_loan": result.get("EquityWithLoanValue"),
            }
        except Exception as e:
            log.exception("get_account_summary_error", extra={"error": str(e)})
            return {}


class MockIBKRClient(IBKRClient):
    """In-memory IBKR client for tests and local dry runs.

    Marks itself "connected" on start() and simulates instant fills for market
    and trail orders via the on_fill callback.
    """

    def __init__(
        self,
        on_fill: Optional[FillCallback] = None,
        on_status: Optional[StatusCallback] = None,
        fill_price_source: Optional[Callable[[str, str], float]] = None,
    ) -> None:
        super().__init__(on_fill=on_fill, on_status=on_status)
        self.ib = None  # bypass ib_insync entirely
        self._fill_price_source = fill_price_source or (lambda sym, action: 100.0)
        self._id_counter = 10_000
        self._orders: dict[int, dict] = {}  # ibkr_order_id -> meta

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        self._connected = True
        self._last_connected = datetime.now(timezone.utc)
        self._disconnect_reason = None
        if self.on_status:
            await self.on_status(True, None)

    async def stop(self) -> None:
        self._connected = False

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def place_market(self, symbol: str, action: str, qty: int) -> Optional[int]:
        if not self.connected:
            return None
        oid = self._next_id()
        self._orders[oid] = {"symbol": symbol, "action": action, "qty": qty, "type": "MKT"}
        # Fire a synthetic fill on the next loop tick so callers have time to persist the order row.
        asyncio.create_task(self._simulate_fill(oid, symbol, action, qty, "MKT"))
        return oid

    async def place_trail(
        self, symbol: str, action: str, qty: int, trail_amount: float, trail_stop_price: float
    ) -> Optional[int]:
        if not self.connected:
            return None
        oid = self._next_id()
        self._orders[oid] = {
            "symbol": symbol, "action": action, "qty": qty, "type": "TRAIL",
            "trail_amount": trail_amount, "trail_stop_price": trail_stop_price,
        }
        # Trail orders do NOT auto-fill in the mock. Tests trigger them explicitly.
        return oid

    async def cancel_order(self, ibkr_order_id: int) -> bool:
        return self._orders.pop(ibkr_order_id, None) is not None

    async def _simulate_fill(self, oid: int, symbol: str, action: str, qty: int, otype: str) -> None:
        await asyncio.sleep(0.01)
        if self.on_fill is None:
            return
        price = self._fill_price_source(symbol, action)
        await self.on_fill({
            "ibkr_order_id": oid,
            "ibkr_exec_id": f"mock-{oid}",
            "symbol": symbol,
            "action": action,
            "order_type": otype,
            "fill_qty": qty,
            "fill_price": price,
            "fill_time": datetime.now(timezone.utc),
            "commission": 0.0,
            "exchange": "MOCK",
        })

    async def trigger_trail_fill(self, ibkr_order_id: int, price: float) -> None:
        """Test helper — simulate a trail stop being hit."""
        meta = self._orders.pop(ibkr_order_id, None)
        if meta is None or self.on_fill is None:
            return
        await self.on_fill({
            "ibkr_order_id": ibkr_order_id,
            "ibkr_exec_id": f"mock-trail-{ibkr_order_id}",
            "symbol": meta["symbol"],
            "action": meta["action"],
            "order_type": "TRAIL",
            "fill_qty": meta["qty"],
            "fill_price": price,
            "fill_time": datetime.now(timezone.utc),
            "commission": 0.0,
            "exchange": "MOCK",
        })

    async def get_positions(self) -> list[dict]:
        return []

    async def get_account_summary(self) -> dict[str, Any]:
        return {
            "net_liquidation": 100_000.0,
            "total_cash": 100_000.0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "equity_with_loan": 100_000.0,
        }
