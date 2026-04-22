"""Order router: consumes the signal queue, applies risk checks, routes orders,
handles fills, manages trailing stops, and keeps the positions table in sync.

Key invariants:
  * Position key is ALWAYS (symbol, direction, interval).
  * Per-symbol asyncio.Lock serializes signal processing for the same symbol
    so close/open flips can't interleave.
  * signal_close_price is stamped on every order row at creation time so
    slippage can be computed at fill time without a join.
  * Every fill is persisted, even orphan trail fills (Rule 4).
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import select, func as sql_func

from .config import settings
from .database import get_session
from .models import Fill, Order, Position, Signal, TradeHistory
from .signal_parser import ParsedSignal

log = logging.getLogger(__name__)


# Broadcast callback set by main at startup.
BroadcastCallback = Callable[[str, dict], Any]  # returns awaitable or None


class OrderRouter:
    def __init__(
        self,
        queue: asyncio.Queue,
        ibkr_client,  # IBKRClient or MockIBKRClient
        broadcast: Optional[BroadcastCallback] = None,
    ) -> None:
        self.queue = queue
        self.ibkr = ibkr_client
        self.broadcast = broadcast
        self._symbol_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._consumer_task: Optional[asyncio.Task] = None
        self._running = False

    # ---- lifecycle ----
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Wire fill handler into IBKR client (it may have been wired at construct
        # time too — idempotent).
        self.ibkr.on_fill = self._on_fill
        self._consumer_task = asyncio.create_task(self._consume())
        log.info("order_router_started")

    async def stop(self) -> None:
        self._running = False
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except (asyncio.CancelledError, Exception):
                pass

    # ---- queue consumer ----
    async def _consume(self) -> None:
        while self._running:
            try:
                item = await self.queue.get()
            except asyncio.CancelledError:
                break
            try:
                parsed: ParsedSignal = item["parsed"]
                signal_id: int = item["signal_id"]
                resolved_qty: Optional[int] = item.get("resolved_qty")
                lock = self._symbol_locks[parsed.symbol]
                async with lock:
                    await self._process(signal_id, parsed, resolved_qty)
            except Exception as e:
                log.exception("order_router_consume_error", extra={"error": str(e)})
            finally:
                self.queue.task_done()

    async def _process(self, signal_id: int, parsed: ParsedSignal, resolved_qty: Optional[int]) -> None:
        # TWS connectivity check.
        if not self.ibkr.connected:
            await self._reject(signal_id, "TWS disconnected")
            return

        if parsed.raw_action in ("open_long", "long"):
            await self._handle_open(signal_id, parsed, "long", resolved_qty or settings().default_qty, "BUY")
        elif parsed.raw_action in ("open_short", "short"):
            await self._handle_open(signal_id, parsed, "short", resolved_qty or settings().default_qty, "SELL")
        elif parsed.raw_action in ("close_long", "l-ts"):
            await self._handle_close(signal_id, parsed, "long", "SELL")
        elif parsed.raw_action in ("close_short", "s-ts"):
            await self._handle_close(signal_id, parsed, "short", "BUY")
        else:
            log.info("router_skip_nonrouted", extra={"signal_id": signal_id, "raw_action": parsed.raw_action})

    # ---- open flow ----
    async def _handle_open(
        self, signal_id: int, parsed: ParsedSignal, direction: str, qty: int, action: str
    ) -> None:
        symbol = parsed.symbol
        interval = parsed.interval

        # In-flight same-direction entry => Rule 5 replacement.
        inflight = await self._find_inflight_entry(symbol, direction, interval)
        if inflight is not None:
            await self._replace_order(inflight, signal_id, parsed, qty, action)
            return

        # Filled same-direction position => close it first, then open a new one.
        open_pos = await self._get_open_position(symbol, direction, interval)
        re_entering = False
        if open_pos is not None and open_pos.qty > 0:
            close_action = "SELL" if direction == "long" else "BUY"
            await self._place_close_for_position(signal_id, parsed, open_pos, close_action)
            re_entering = True
            log.info("same_direction_flip", extra={"symbol": symbol, "direction": direction, "qty": open_pos.qty})

        # Opposite-direction in-flight => cancel + close partial, then proceed.
        opp = "short" if direction == "long" else "long"
        opp_inflight = await self._find_inflight_entry(symbol, opp, interval)
        if opp_inflight is not None:
            await self._cancel_entry_for_flip(opp_inflight, signal_id)

        # Opposite-direction filled position => close first, then open.
        opp_open = await self._get_open_position(symbol, opp, interval)
        flipping = False
        if opp_open is not None and opp_open.qty > 0:
            close_action = "SELL" if opp == "long" else "BUY"
            await self._place_close_for_position(signal_id, parsed, opp_open, close_action)
            flipping = True

        # Pre-trade risk checks.
        # skip_open_count: opposite-direction close was just submitted but not yet filled,
        #   so it still appears open in the DB — don't count it against max_open_positions.
        # re_entering: same-direction close was just submitted but not yet filled,
        #   so cur_qty would double-count the being-closed position — check qty alone.
        err = await self._risk_checks(
            symbol, direction, interval, qty,
            skip_open_count=flipping,
            re_entering=re_entering,
        )
        if err is not None:
            await self._reject(signal_id, err)
            return

        # Place entry order.
        await self._place_entry(signal_id, parsed, direction, action, qty)

    async def _place_entry(
        self, signal_id: int, parsed: ParsedSignal, direction: str, action: str, qty: int
    ) -> int:
        ibkr_id = await self.ibkr.place_market(parsed.symbol, action, qty)
        now = datetime.now(timezone.utc)
        async with get_session() as session:
            o = Order(
                signal_id=signal_id,
                ibkr_order_id=ibkr_id,
                symbol=parsed.symbol,
                action=action,
                qty=qty,
                order_type="MKT",
                direction=direction,
                order_role="entry",
                status="submitted" if ibkr_id is not None else "error",
                submitted_at=now if ibkr_id is not None else None,
                error_msg=None if ibkr_id is not None else "placeOrder failed",
                signal_close_price=parsed.close_price,
                interval=parsed.interval,
                created_at=now,
            )
            session.add(o)
            await session.commit()
            await session.refresh(o)
            order_id = o.id
        await self._broadcast("order_update", {"order_id": order_id, "status": o.status})
        log.info(
            "order_submitted",
            extra={"order_id": order_id, "ibkr_order_id": ibkr_id, "symbol": parsed.symbol,
                   "action": action, "qty": qty, "order_type": "MKT", "order_role": "entry"},
        )
        return order_id

    async def _replace_order(
        self, existing: Order, new_signal_id: int, parsed: ParsedSignal, new_qty: int, action: str
    ) -> None:
        """Rule 5: cancel existing in-flight entry, then place new."""
        log.info(
            "order_replaced_start",
            extra={
                "old_order_id": existing.id,
                "old_ibkr_order_id": existing.ibkr_order_id,
                "new_signal_id": new_signal_id,
                "new_qty": new_qty,
            },
        )
        if existing.ibkr_order_id is not None:
            await self.ibkr.cancel_order(existing.ibkr_order_id)
        # Mark old cancelled.
        async with get_session() as session:
            old = await session.get(Order, existing.id)
            if old is not None:
                old.status = "cancelled"
                old.cancelled_at = datetime.now(timezone.utc)
                old.replaced_by_signal_id = new_signal_id
                old.error_msg = f"replaced_by_signal_{new_signal_id}"
                await session.commit()

        # Handle partial fill before replacement.
        if existing.fill_qty and existing.fill_qty > 0:
            mode = settings().partial_fill_replacement_mode
            log.warning(
                "partial_fill_before_replacement",
                extra={"symbol": parsed.symbol, "filled_qty": existing.fill_qty,
                       "new_signal_qty": new_qty, "mode": mode},
            )
            if mode == "replace":
                # Close the partial fill first. For a long entry, close = SELL.
                close_action = "SELL" if existing.direction == "long" else "BUY"
                closer_ibkr = await self.ibkr.place_market(
                    parsed.symbol, close_action, existing.fill_qty
                )
                now = datetime.now(timezone.utc)
                async with get_session() as session:
                    closer = Order(
                        signal_id=new_signal_id,
                        ibkr_order_id=closer_ibkr,
                        symbol=parsed.symbol,
                        action=close_action,
                        qty=existing.fill_qty,
                        order_type="MKT",
                        direction=existing.direction,
                        order_role="exit",
                        status="submitted" if closer_ibkr else "error",
                        submitted_at=now if closer_ibkr else None,
                        signal_close_price=parsed.close_price,
                        interval=parsed.interval,
                        created_at=now,
                        replaced_order_id=existing.id,
                    )
                    session.add(closer)
                    await session.commit()

        # Place new entry.
        new_id = await self._place_entry(new_signal_id, parsed, existing.direction, action, new_qty)
        # Link replacement back to old order.
        async with get_session() as session:
            new_order = await session.get(Order, new_id)
            if new_order is not None:
                new_order.replaced_order_id = existing.id
                await session.commit()
        await self._broadcast("order_replaced", {
            "old_order_id": existing.id,
            "new_order_id": new_id,
            "symbol": parsed.symbol,
            "interval": parsed.interval,
        })

    async def _cancel_entry_for_flip(self, existing: Order, new_signal_id: int) -> None:
        if existing.ibkr_order_id is not None:
            await self.ibkr.cancel_order(existing.ibkr_order_id)
        async with get_session() as session:
            old = await session.get(Order, existing.id)
            if old is not None:
                old.status = "cancelled"
                old.cancelled_at = datetime.now(timezone.utc)
                old.replaced_by_signal_id = new_signal_id
                await session.commit()

    # ---- close flow ----
    async def _handle_close(
        self, signal_id: int, parsed: ParsedSignal, direction: str, action: str
    ) -> None:
        pos = await self._get_open_position(parsed.symbol, direction, parsed.interval)
        if pos is None or pos.qty == 0:
            log.warning(
                "close_signal_no_position",
                extra={
                    "symbol": parsed.symbol,
                    "direction": direction,
                    "interval": parsed.interval,
                    "signal_id": signal_id,
                },
            )
            # Do not reject — PRD says log warning, return 200, do not place order.
            return
        await self._place_close_for_position(signal_id, parsed, pos, action)

    async def _place_close_for_position(
        self, signal_id: int, parsed: ParsedSignal, pos: Position, action: str
    ) -> None:
        # Cancel trailing stop first (if any).
        trail_order_id = await self._find_active_trail_for_position(pos)
        if trail_order_id is not None:
            async with get_session() as session:
                trail_order = await session.get(Order, trail_order_id)
            if trail_order and trail_order.status not in ("filled", "cancelled") and trail_order.ibkr_order_id:
                cancelled = await self.ibkr.cancel_order(trail_order.ibkr_order_id)
                # Give IBKR up to 2s to confirm.
                if cancelled:
                    await asyncio.sleep(0.1)
                async with get_session() as session:
                    t = await session.get(Order, trail_order_id)
                    if t is not None and t.status not in ("filled", "cancelled"):
                        t.status = "cancelled"
                        t.cancelled_at = datetime.now(timezone.utc)
                        await session.commit()
                log.info("trail_order_cancelled", extra={"order_id": trail_order_id})

        # Place market close.
        qty = pos.qty
        ibkr_id = await self.ibkr.place_market(pos.symbol, action, qty)
        now = datetime.now(timezone.utc)
        async with get_session() as session:
            o = Order(
                signal_id=signal_id,
                ibkr_order_id=ibkr_id,
                symbol=pos.symbol,
                action=action,
                qty=qty,
                order_type="MKT",
                direction=pos.direction,
                order_role="exit",
                status="submitted" if ibkr_id else "error",
                submitted_at=now if ibkr_id else None,
                signal_close_price=parsed.close_price,
                interval=pos.interval,
                created_at=now,
            )
            session.add(o)
            await session.commit()
            await session.refresh(o)
            order_id = o.id
        await self._broadcast("order_update", {"order_id": order_id, "status": o.status})
        log.info(
            "order_submitted",
            extra={"order_id": order_id, "ibkr_order_id": ibkr_id, "symbol": pos.symbol,
                   "action": action, "qty": qty, "order_type": "MKT", "order_role": "exit"},
        )

    # ---- fill handler (called by IBKR client) ----
    async def _on_fill(self, payload: dict) -> None:
        ibkr_order_id = payload.get("ibkr_order_id")
        symbol = payload.get("symbol")
        action = payload.get("action")
        order_type = payload.get("order_type")
        fill_qty = int(payload.get("fill_qty") or 0)
        fill_price = float(payload.get("fill_price") or 0.0)
        fill_time: datetime = payload.get("fill_time") or datetime.now(timezone.utc)
        ibkr_exec_id = payload.get("ibkr_exec_id")
        commission = payload.get("commission")
        exchange = payload.get("exchange")

        if fill_qty <= 0 or not symbol or ibkr_order_id is None:
            log.warning("fill_missing_fields", extra={"payload": {k: payload.get(k) for k in ("ibkr_order_id","symbol","fill_qty")}})
            return

        # Serialize per symbol so close/open flips can't race with fills.
        async with self._symbol_locks[symbol]:
            await self._process_fill(
                ibkr_order_id=ibkr_order_id,
                symbol=symbol,
                action=action,
                order_type=order_type,
                fill_qty=fill_qty,
                fill_price=fill_price,
                fill_time=fill_time,
                ibkr_exec_id=ibkr_exec_id,
                commission=commission,
                exchange=exchange,
            )

        asyncio.create_task(self._refresh_account_after_fill())

    async def _refresh_account_after_fill(self) -> None:
        await asyncio.sleep(2)
        try:
            summary = await self.ibkr.get_account_summary()
            if summary:
                await self.broadcast("account_update", summary)
        except Exception as e:
            log.warning("account_refresh_after_fill_error", extra={"error": str(e)})

    async def _process_fill(
        self,
        ibkr_order_id: int,
        symbol: str,
        action: Optional[str],
        order_type: Optional[str],
        fill_qty: int,
        fill_price: float,
        fill_time: datetime,
        ibkr_exec_id: Optional[str],
        commission: Optional[float],
        exchange: Optional[str],
    ) -> None:
        async with get_session() as session:
            res = await session.execute(
                select(Order).where(Order.ibkr_order_id == ibkr_order_id).limit(1)
            )
            order = res.scalar_one_or_none()

            if order is None:
                # Orphan (Rule 4 case a). Record synthetic order + fill.
                log.warning(
                    "orphan_trail_stop_triggered",
                    extra={"ibkr_order_id": ibkr_order_id, "symbol": symbol,
                           "fill_price": fill_price, "fill_qty": fill_qty, "action": action},
                )
                # Infer direction from action: SELL on an orphan trail implies long position closing;
                # BUY implies short position closing.
                inferred_dir = "long" if action == "SELL" else "short"
                syn = Order(
                    signal_id=0,  # synthetic — no originating signal
                    ibkr_order_id=ibkr_order_id,
                    symbol=symbol,
                    action=action or "SELL",
                    qty=fill_qty,
                    order_type=order_type or "TRAIL",
                    direction=inferred_dir,
                    order_role="trail_stop",
                    status="filled",
                    fill_qty=fill_qty,
                    fill_price=fill_price,
                    fill_time=fill_time,
                    submitted_at=fill_time,
                    created_at=fill_time,
                )
                # signal_id is NOT NULL; use a sentinel signal if needed.
                # Simplest: create a stub signal row so FK isn't violated.
                stub = Signal(
                    received_at=fill_time,
                    raw_action="orphan_trail_fill",
                    symbol=symbol,
                    interval=None,
                    strategy="ldc",
                    status="informational",
                    parse_format="plaintext",
                    raw_body=f"orphan fill ibkr_order_id={ibkr_order_id}",
                )
                session.add(stub)
                await session.flush()
                syn.signal_id = stub.id
                session.add(syn)
                await session.flush()
                await session.refresh(syn)

                fill = Fill(
                    order_id=syn.id,
                    ibkr_exec_id=ibkr_exec_id,
                    ibkr_order_id=ibkr_order_id,
                    fill_qty=fill_qty,
                    fill_price=fill_price,
                    fill_time=fill_time,
                    commission=commission,
                    exchange=exchange,
                )
                session.add(fill)

                # Close matching open position if any.
                pos_res = await session.execute(
                    select(Position).where(Position.symbol == symbol).where(Position.qty > 0).limit(1)
                )
                pos = pos_res.scalar_one_or_none()
                if pos is not None:
                    self._apply_close_to_position(pos, fill_price, fill_qty, fill_time)
                    await session.commit()
                    await self._broadcast("position_update", _position_snapshot(pos))
                else:
                    await session.commit()
                await self._broadcast("orphan_trail_warning", {
                    "ibkr_order_id": ibkr_order_id, "symbol": symbol,
                    "fill_price": fill_price, "fill_qty": fill_qty,
                })
                return

            # Known order: append to fills, update aggregate.
            # Cap fill_qty to the order's remaining unfilled quantity.
            # During position flips IBKR may report a combined execution (close old +
            # open new) as a single fill against the entry order, overstating fill_qty.
            already_filled = order.fill_qty or 0
            remaining = max(0, order.qty - already_filled)
            if fill_qty > remaining:
                log.warning(
                    "fill_qty_capped",
                    extra={
                        "order_id": order.id,
                        "reported_fill_qty": fill_qty,
                        "order_qty": order.qty,
                        "already_filled": already_filled,
                        "capped_to": remaining,
                    },
                )
                fill_qty = remaining

            if fill_qty == 0:
                await session.commit()
                return

            fill = Fill(
                order_id=order.id,
                ibkr_exec_id=ibkr_exec_id,
                ibkr_order_id=ibkr_order_id,
                fill_qty=fill_qty,
                fill_price=fill_price,
                fill_time=fill_time,
                commission=commission,
                exchange=exchange,
            )
            session.add(fill)

            new_total = already_filled + fill_qty
            # Weighted avg fill price.
            if order.fill_price and order.fill_qty:
                new_avg = ((order.fill_price * order.fill_qty) + (fill_price * fill_qty)) / new_total
            else:
                new_avg = fill_price
            order.fill_qty = new_total
            order.fill_price = new_avg
            if new_total >= order.qty:
                order.status = "filled"
                order.fill_time = fill_time
            else:
                order.status = "partially_filled"

            # Slippage: positive = favorable execution vs signal price, negative = unfavorable.
            # Long entry/short exit: lower fill is better → signal - fill (direction_mult=-1, role_mult=+1 or -1*-1)
            # Short entry/long exit: higher fill is better → fill - signal
            just_became_filled = (order.status == "filled")
            if just_became_filled and order.signal_close_price:
                direction_mult = -1.0 if order.direction == "long" else 1.0
                role_mult = -1.0 if order.order_role in ("exit", "trail_stop") else 1.0
                order.fill_deviation_pts = (new_avg - order.signal_close_price) * direction_mult * role_mult
                order.fill_deviation_pct = (order.fill_deviation_pts / order.signal_close_price) * 100.0
                log.info(
                    "slippage_calculated",
                    extra={
                        "order_id": order.id,
                        "signal_price": order.signal_close_price,
                        "fill_price": new_avg,
                        "deviation_pts": order.fill_deviation_pts,
                        "deviation_pct": order.fill_deviation_pct,
                    },
                )

            log.info(
                "order_filled",
                extra={"order_id": order.id, "symbol": symbol, "fill_price": new_avg, "fill_qty": new_total},
            )

            # Effect on positions table.
            direction = order.direction
            interval = order.interval
            if direction and interval:
                pos_res = await session.execute(
                    select(Position)
                    .where(Position.symbol == symbol)
                    .where(Position.direction == direction)
                    .where(Position.interval == interval)
                    .limit(1)
                )
                pos = pos_res.scalar_one_or_none()
            else:
                pos = None

            if order.order_role == "entry" and order.status == "filled":
                if pos is None:
                    pos = Position(
                        symbol=symbol,
                        direction=direction or "long",
                        interval=interval or "unknown",
                        qty=new_total,
                        avg_cost=new_avg,
                        last_updated=fill_time,
                        opened_at=fill_time,
                        realized_pnl=0.0,
                        signal_entry_price=order.signal_close_price,
                    )
                    session.add(pos)
                else:
                    if pos.qty == 0:
                        # Re-entry after a completed close: start a fresh cycle on the same row.
                        # Reset realized_pnl so this cycle's P&L is tracked independently.
                        pos.qty = new_total
                        pos.avg_cost = new_avg
                        pos.last_updated = fill_time
                        pos.opened_at = fill_time
                        pos.closed_at = None
                        pos.realized_pnl = 0.0
                        pos.close_fill_price = None
                        pos.signal_entry_price = order.signal_close_price
                    else:
                        # Pyramid add (shouldn't happen in v1 with reject-if-open guard).
                        total_cost = (pos.avg_cost or 0) * pos.qty + new_avg * new_total
                        combined_qty = pos.qty + new_total
                        pos.qty = combined_qty
                        pos.avg_cost = total_cost / combined_qty if combined_qty else 0
                        pos.last_updated = fill_time
                        pos.closed_at = None
                log.info("position_opened", extra={"symbol": symbol, "direction": direction,
                                                    "qty": new_total, "avg_cost": new_avg})
                await session.commit()
                await self._broadcast("fill", {
                    "order_id": order.id, "symbol": symbol, "fill_qty": new_total, "fill_price": new_avg,
                })
                await self._broadcast("position_update", _position_snapshot(pos))
                # Place trailing stop.
                await self._place_trail_after_entry(order, pos, fill_time)
                return

            if order.order_role in ("exit", "trail_stop") and pos is not None:
                self._apply_close_to_position(pos, new_avg, order.fill_qty, fill_time)
                if pos.qty == 0:
                    session.add(TradeHistory(
                        symbol=pos.symbol,
                        direction=pos.direction,
                        interval=pos.interval,
                        qty=order.fill_qty,
                        avg_cost=pos.avg_cost,
                        close_fill_price=pos.close_fill_price,
                        realized_pnl=pos.realized_pnl,
                        signal_entry_price=pos.signal_entry_price,
                        signal_close_price=order.signal_close_price,
                        opened_at=pos.opened_at,
                        closed_at=pos.closed_at,
                    ))
                await session.commit()
                await self._broadcast("fill", {
                    "order_id": order.id, "symbol": symbol, "fill_qty": order.fill_qty,
                    "fill_price": new_avg, "order_role": order.order_role,
                })
                await self._broadcast("position_update", _position_snapshot(pos))
                return

            # Fallback commit for partial entry or other states.
            await session.commit()
            await self._broadcast("fill", {"order_id": order.id, "symbol": symbol,
                                             "fill_qty": order.fill_qty, "fill_price": new_avg,
                                             "status": order.status})

    def _apply_close_to_position(
        self, pos: Position, fill_price: float, fill_qty: int, fill_time: datetime
    ) -> None:
        """Mutates `pos` to reflect a close/trail fill. Caller must commit."""
        if pos.direction == "long":
            realized_delta = (fill_price - (pos.avg_cost or 0)) * fill_qty
        else:
            realized_delta = ((pos.avg_cost or 0) - fill_price) * fill_qty
        pos.realized_pnl = (pos.realized_pnl or 0.0) + realized_delta
        pos.qty = max(0, (pos.qty or 0) - fill_qty)
        pos.last_updated = fill_time
        if pos.qty == 0:
            pos.closed_at = fill_time
            pos.close_fill_price = fill_price
            log.info(
                "position_closed",
                extra={"symbol": pos.symbol, "direction": pos.direction,
                       "interval": pos.interval, "realized_pnl": pos.realized_pnl},
            )

    async def _place_trail_after_entry(self, entry_order: Order, pos: Position, fill_time: datetime) -> None:
        if settings().disable_trail:
            log.info(
                "trail_skipped_disabled",
                extra={"order_id": entry_order.id, "symbol": entry_order.symbol,
                       "reason": "DISABLE_TRAIL=true"},
            )
            return
        trail_offset = settings().resolve_trail_offset(entry_order.symbol)
        # Warn if trail offset is inappropriate (>= 20% of entry fill price).
        fill_price = entry_order.fill_price or 0.0
        if fill_price > 0 and trail_offset >= fill_price * 0.20:
            log.warning(
                "trail_offset_large_relative_to_price",
                extra={"symbol": entry_order.symbol, "trail_offset": trail_offset, "fill_price": fill_price},
            )
        action = "SELL" if entry_order.direction == "long" else "BUY"
        if entry_order.direction == "long":
            stop_price = fill_price - trail_offset
        else:
            stop_price = fill_price + trail_offset
        ibkr_id = await self.ibkr.place_trail(
            entry_order.symbol, action, entry_order.fill_qty,
            trail_amount=trail_offset, trail_stop_price=stop_price,
        )
        now = datetime.now(timezone.utc)
        async with get_session() as session:
            trail = Order(
                signal_id=entry_order.signal_id,
                parent_order_id=entry_order.id,
                ibkr_order_id=ibkr_id,
                symbol=entry_order.symbol,
                action=action,
                qty=entry_order.fill_qty,
                order_type="TRAIL",
                trail_amount=trail_offset,
                trail_stop_price=stop_price,
                direction=entry_order.direction,
                order_role="trail_stop",
                status="submitted" if ibkr_id else "error",
                submitted_at=now if ibkr_id else None,
                signal_close_price=entry_order.signal_close_price,
                interval=entry_order.interval,
                created_at=now,
            )
            session.add(trail)
            await session.flush()
            await session.refresh(trail)
            # Link entry -> trail.
            entry = await session.get(Order, entry_order.id)
            if entry:
                entry.trail_order_id = trail.id
            await session.commit()
            trail_id = trail.id
        log.info(
            "trail_order_placed",
            extra={"order_id": trail_id, "parent_order_id": entry_order.id,
                   "ibkr_order_id": ibkr_id, "symbol": entry_order.symbol,
                   "action": action, "trail_amount": trail_offset, "trail_stop_price": stop_price},
        )
        await self._broadcast("order_update", {"order_id": trail_id, "status": "submitted", "role": "trail_stop"})

    # ---- helpers ----

    async def _find_inflight_entry(self, symbol: str, direction: str, interval: Optional[str]) -> Optional[Order]:
        async with get_session() as session:
            stmt = (
                select(Order)
                .where(Order.symbol == symbol)
                .where(Order.direction == direction)
                .where(Order.order_role == "entry")
                .where(Order.interval == interval)
                .where(Order.status.in_(("submitted", "partially_filled")))
                .order_by(Order.id.desc())
                .limit(1)
            )
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def _get_open_position(self, symbol: str, direction: str, interval: Optional[str]) -> Optional[Position]:
        async with get_session() as session:
            stmt = (
                select(Position)
                .where(Position.symbol == symbol)
                .where(Position.direction == direction)
                .where(Position.interval == (interval or "unknown"))
                .limit(1)
            )
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def _find_active_trail_for_position(self, pos: Position) -> Optional[int]:
        """Find the active trail_stop order id for this (symbol, direction, interval)."""
        async with get_session() as session:
            stmt = (
                select(Order.id)
                .where(Order.symbol == pos.symbol)
                .where(Order.direction == pos.direction)
                .where(Order.interval == pos.interval)
                .where(Order.order_role == "trail_stop")
                .where(Order.status.in_(("submitted", "working", "partially_filled")))
                .order_by(Order.id.desc())
                .limit(1)
            )
            res = await session.execute(stmt)
            row = res.first()
            return row[0] if row else None

    async def _risk_checks(
        self, symbol: str, direction: str, interval: Optional[str], qty: int,
        skip_open_count: bool = False,
        re_entering: bool = False,
    ) -> Optional[str]:
        cfg = settings()
        # Check 4: max position size (per symbol, interval).
        current = await self._get_open_position(symbol, direction, interval)
        cur_qty = current.qty if current else 0
        # When re-entering (same-direction close just submitted, not yet filled), the old
        # position still appears open in the DB. Check new qty alone, not cur + new.
        effective_qty = qty if re_entering else cur_qty + qty
        if effective_qty > cfg.max_position_size:
            return f"exceeds max position size of {cfg.max_position_size} for {symbol}"
        # Check 5: max open positions (only for new positions).
        # Skipped when flipping: the opposite-direction close was just submitted but hasn't
        # filled yet, so that position still appears open and would falsely trigger this limit.
        if cur_qty == 0 and not skip_open_count:
            async with get_session() as session:
                res = await session.execute(
                    select(sql_func.count()).select_from(Position).where(Position.qty > 0)
                )
                count = res.scalar_one() or 0
            if count >= cfg.max_open_positions:
                return f"exceeds max open positions of {cfg.max_open_positions}"
        return None

    async def _reject(self, signal_id: int, reason: str) -> None:
        log.info("risk_check_failed", extra={"signal_id": signal_id, "reason": reason})
        async with get_session() as session:
            sig = await session.get(Signal, signal_id)
            if sig is not None:
                sig.status = "rejected"
                sig.reject_reason = reason
                await session.commit()
        await self._broadcast("signal", {"signal_id": signal_id, "status": "rejected", "reason": reason})

    async def _broadcast(self, event_type: str, data: dict) -> None:
        if self.broadcast is None:
            return
        try:
            result = self.broadcast(event_type, data)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            log.warning("broadcast_failed", extra={"event_type": event_type, "error": str(e)})


def _position_snapshot(pos: Position) -> dict:
    return {
        "id": pos.id,
        "symbol": pos.symbol,
        "direction": pos.direction,
        "interval": pos.interval,
        "qty": pos.qty,
        "avg_cost": pos.avg_cost,
        "realized_pnl": pos.realized_pnl,
        "close_fill_price": pos.close_fill_price,
        "last_updated": pos.last_updated.isoformat() if pos.last_updated else None,
        "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
        "closed_at": pos.closed_at.isoformat() if pos.closed_at else None,
    }
