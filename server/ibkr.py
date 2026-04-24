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

# ib_insync / eventkit cache the "main event loop" at import time. On Python 3.14
# asyncio.get_event_loop() raises when no loop exists, AND if we pre-create a loop
# just to satisfy the import, ib_insync's sockets/Events get bound to *that* loop
# instead of uvicorn's running loop ("attached to a different loop" errors).
#
# Fix: defer the ib_insync import to start() where the running uvicorn loop is
# already the current loop. The names below are placeholders until then.
IB = object  # type: ignore
Contract = object  # type: ignore
ContFuture = object  # type: ignore
Future = object  # type: ignore
MarketOrder = object  # type: ignore
Order = object  # type: ignore
Stock = object  # type: ignore
Trade = object  # type: ignore
util = None
_IB_AVAILABLE = False
_IB_IMPORT_ERROR: Optional[str] = None


def _lazy_import_ib_insync() -> bool:
    """Import ib_insync from inside a running event loop. Returns True on success."""
    global IB, Contract, ContFuture, Future, MarketOrder, Order, Stock, Trade, util, _IB_AVAILABLE, _IB_IMPORT_ERROR
    if _IB_AVAILABLE:
        return True
    # Ensure the current running loop is also the policy's "current event loop".
    # In Python 3.14, asyncio.set_event_loop with a running loop is required for
    # eventkit's get_event_loop_policy().get_event_loop() to find it at import time.
    try:
        running = asyncio.get_running_loop()
        asyncio.set_event_loop(running)
    except RuntimeError:
        pass
    try:
        from ib_insync import (  # type: ignore
            IB as _IB,
            Contract as _Contract,
            ContFuture as _ContFuture,
            Future as _Future,
            MarketOrder as _MarketOrder,
            Order as _Order,
            Stock as _Stock,
            Trade as _Trade,
            util as _util,
        )
        IB = _IB
        Contract = _Contract
        ContFuture = _ContFuture
        Future = _Future
        MarketOrder = _MarketOrder
        Order = _Order
        Stock = _Stock
        Trade = _Trade
        util = _util
        _IB_AVAILABLE = True
        return True
    except Exception as e:  # pragma: no cover
        _IB_IMPORT_ERROR = f"{type(e).__name__}: {e}"
        return False


async def _build_contract(symbol: str):
    """Resolve a TradingView symbol to an ib_insync contract.

    Queries the contract_map DB table first (hot-updatable for futures rolls).
    Falls back to the CONTRACT_MAP env spec, then Stock(symbol, SMART, USD).
    Strips the TV continuous-future suffix (``MBT1!`` -> ``MBT``) before lookup.
    Caller is responsible for qualifyContractsAsync().
    """
    import re

    from sqlalchemy import select

    from .database import get_session
    from .models import ContractMap

    raw = (symbol or "").upper()
    base = re.sub(r"\d+!$", "", raw)

    sec, sym, exch, ccy, last = "stock", base, "SMART", "USD", None
    async with get_session() as sess:
        row = await sess.execute(
            select(ContractMap).where(ContractMap.tv_symbol == base)
        )
        mapping = row.scalar_one_or_none()
    if mapping:
        sec, sym, exch, ccy, last = (
            mapping.sec_type, mapping.ib_symbol, mapping.exchange,
            mapping.currency, mapping.last_trade_date,
        )
    else:
        # Fall back to env-based spec so existing configs keep working.
        spec = settings().resolve_contract_spec(symbol)
        sec, sym, exch, ccy, last = (
            spec["sec_type"], spec["symbol"], spec["exchange"],
            spec["currency"], spec.get("last_trade_date"),
        )

    if sec == "cont_future":
        return ContFuture(sym, exch, ccy)
    if sec == "future":
        if last:
            return Future(sym, last, exch, currency=ccy)
        return Future(sym, exchange=exch, currency=ccy)
    return Stock(sym, exch or "SMART", ccy or "USD")

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
        # Instantiate IB() lazily inside start() so the socket binds to the
        # running event loop (uvicorn's), not whichever loop exists at import time.
        self.ib = None
        self.on_fill = on_fill
        self.on_status = on_status
        self._connected: bool = False
        self._last_connected: Optional[datetime] = None
        self._disconnect_reason: Optional[str] = None
        self._account_ids: list[str] = []
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

    @property
    def account_ids(self) -> list[str]:
        return list(self._account_ids)

    async def start(self) -> None:
        """Start connection attempts in the background."""
        # Import ib_insync now, from inside the running uvicorn loop, so
        # eventkit's import-time get_event_loop() call binds to the right loop.
        if not _lazy_import_ib_insync():
            log.warning(
                "ib_insync_unavailable_running_in_mock_mode",
                extra={"import_error": _IB_IMPORT_ERROR},
            )
            return
        if self.ib is None:
            self.ib = IB()
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
                    await asyncio.wait_for(
                        self.ib.connectAsync(
                            host=cfg.tws_host,
                            port=cfg.tws_port,
                            clientId=cfg.tws_client_id,
                            readonly=False,
                        ),
                        timeout=30,
                    )
                    self._connected = True
                    self._last_connected = datetime.now(timezone.utc)
                    self._disconnect_reason = None
                    if not await self._validate_account_guardrail():
                        self._connected = False
                    else:
                        log.info("tws_connected", extra={"host": cfg.tws_host, "port": cfg.tws_port})
                        self._wire_events()
                        if self.on_status:
                            await self.on_status(True, None)
                        await self._reconcile_on_connect()
                except asyncio.TimeoutError:
                    self._disconnect_reason = "connect timeout (30s)"
                    log.warning("tws_connect_timeout", extra={"host": cfg.tws_host, "port": cfg.tws_port})
                    if self.on_status:
                        await self.on_status(False, "connect timeout")
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

    async def _validate_account_guardrail(self) -> bool:
        """Verify the connected IBKR session is the configured account.

        This is especially important on live deployments where paper/live
        sessions can be accidentally cross-wired by credentials or ports.
        """
        cfg = settings()
        self._account_ids = []
        try:
            accounts = []
            if self.ib is not None:
                accounts = list(self.ib.managedAccounts() or [])
            self._account_ids = [str(a) for a in accounts if a]
        except Exception as e:
            reason = f"managedAccounts failed: {e}"
            self._disconnect_reason = reason
            log.warning("ibkr_account_guardrail_failed", extra={"reason": reason})
            if self.on_status:
                await self.on_status(False, reason)
            return False

        expected = cfg.expected_ibkr_account
        if expected and expected not in self._account_ids:
            reason = (
                f"connected IBKR accounts {self._account_ids} do not include "
                f"EXPECTED_IBKR_ACCOUNT={expected}"
            )
            self._disconnect_reason = reason
            log.error(
                "ibkr_account_mismatch",
                extra={"expected": expected, "connected_accounts": self._account_ids},
            )
            try:
                if self.ib is not None and self.ib.isConnected():
                    self.ib.disconnect()
            except Exception:
                pass
            if self.on_status:
                await self.on_status(False, reason)
            return False

        log.info(
            "ibkr_account_guardrail_ok",
            extra={
                "expected": expected,
                "connected_accounts": self._account_ids,
                "trading_mode": cfg.trading_mode,
            },
        )
        return True

    def _wire_events(self) -> None:
        if self.ib is None:
            return
        try:
            # Remove first so repeated reconnects don't accumulate duplicate handlers.
            for ev, handler in [
                (self.ib.execDetailsEvent, self._on_exec_details),
                (self.ib.orderStatusEvent, self._on_order_status),
                (self.ib.disconnectedEvent, self._on_disconnected),
            ]:
                try:
                    ev -= handler
                except Exception as e:
                    log.warning("wire_events_handler_remove_failed", extra={"handler": handler.__name__, "error": str(e)})
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
        """On (re)connect: compare IBKR live positions against the DB.

        Any DB position with qty > 0 that IBKR reports as zero (or opposite
        direction) was closed during the disconnect window. We zero it out so
        the router doesn't treat it as still open.
        """
        from sqlalchemy import select

        from .database import get_session
        from .models import Position as DbPosition

        try:
            ibkr_positions = []
            open_orders = []
            if self.ib is not None:
                ibkr_positions = await asyncio.wait_for(self.ib.reqPositionsAsync(), timeout=10)
                open_orders = await asyncio.wait_for(
                    asyncio.to_thread(self.ib.reqOpenOrders), timeout=10
                ) or []
            log.info(
                "startup_position_sync",
                extra={"ibkr_positions": len(ibkr_positions), "open_orders": len(open_orders)},
            )
        except asyncio.TimeoutError:
            log.warning("reconcile_on_connect_timeout", extra={"error": "timeout after 10s"})
            return
        except Exception as e:
            log.warning("reconcile_on_connect_failed", extra={"error": str(e)})
            return

        # Build symbol → net qty map from IBKR (positive=long, negative=short).
        ibkr_by_symbol: dict[str, float] = {}
        for p in ibkr_positions:
            sym = getattr(getattr(p, "contract", None), "symbol", None)
            qty = float(getattr(p, "position", 0) or 0)
            if sym:
                ibkr_by_symbol[sym.upper()] = qty

        try:
            async with get_session() as session:
                db_positions = (
                    await session.execute(select(DbPosition).where(DbPosition.qty > 0))
                ).scalars().all()

                matched_symbols: set[str] = set()
                for pos in db_positions:
                    ibkr_qty = ibkr_by_symbol.get(pos.symbol.upper(), 0.0)
                    ibkr_is_long = ibkr_qty > 0
                    ibkr_is_short = ibkr_qty < 0
                    consistent = (pos.direction == "long" and ibkr_is_long) or (
                        pos.direction == "short" and ibkr_is_short
                    )

                    if ibkr_qty == 0.0 or not consistent:
                        reason = "closed_during_disconnect" if ibkr_qty == 0.0 else "direction_mismatch"
                        log.warning(
                            "reconcile_position_zeroed",
                            extra={
                                "position_id": pos.id,
                                "symbol": pos.symbol,
                                "direction": pos.direction,
                                "interval": pos.interval,
                                "db_qty": pos.qty,
                                "ibkr_qty": ibkr_qty,
                                "reason": reason,
                            },
                        )
                        pos.qty = 0
                        pos.closed_at = datetime.now(timezone.utc)
                    else:
                        log.info(
                            "reconcile_position_ok",
                            extra={
                                "position_id": pos.id,
                                "symbol": pos.symbol,
                                "direction": pos.direction,
                                "ibkr_qty": ibkr_qty,
                            },
                        )
                        matched_symbols.add(pos.symbol.upper())

                await session.commit()

            # Warn about IBKR positions that have no open DB row — these can't be
            # auto-reconciled without signal/interval context.
            for sym, ibkr_qty in ibkr_by_symbol.items():
                if sym not in matched_symbols:
                    log.warning(
                        "reconcile_ibkr_position_unmatched",
                        extra={"symbol": sym, "ibkr_qty": ibkr_qty},
                    )
        except Exception as e:
            log.warning("reconcile_db_update_failed", extra={"error": str(e)})

    # --- Order placement ---

    async def place_market(self, symbol: str, action: str, qty: int) -> Optional[int]:
        """Place a market order, return IBKR order id (or None on failure)."""
        if self.ib is None or not self.connected:
            log.error("place_market_disconnected", extra={"symbol": symbol})
            return None
        try:
            contract = await _build_contract(symbol)
            await asyncio.wait_for(self.ib.qualifyContractsAsync(contract), timeout=10)
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
            contract = await _build_contract(symbol)
            await asyncio.wait_for(self.ib.qualifyContractsAsync(contract), timeout=10)
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
            positions = await asyncio.wait_for(self.ib.reqPositionsAsync(), timeout=10)
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
            values = await asyncio.wait_for(self.ib.accountSummaryAsync(), timeout=10)
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
        self._account_ids = [settings().expected_ibkr_account or "MOCK-PAPER"]
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
