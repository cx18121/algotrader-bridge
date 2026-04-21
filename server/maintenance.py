"""Maintenance window scheduler (PRD Rule 3).

Responsibilities:
  * Before MAINTENANCE_WINDOW_START each day, run a close sequence:
      - Stop accepting signals (webhook returns 503 "maintenance").
      - Cancel every active trailing stop.
      - Place a market close for every open position.
      - Wait up to CLOSE_FILL_TIMEOUT_SECONDS per position for the close to fill.
      - Cancel any remaining in-flight entry orders.
      - Verify no open positions remain; log `maintenance_close_failed` if any do.
  * During the window itself (start -> end), stay in "maintenance" mode with
    accepting_signals = False.
  * At MAINTENANCE_WINDOW_END, resume: accepting_signals = True, mode = "normal".

Runtime surface:
  MaintenanceScheduler(ibkr, ws_manager)
    .mode        : "normal" | "pre_close" | "maintenance"
    .message     : human-readable status for the dashboard
    .resumes_at  : ISO8601 for the end of the current/next window (or None)
    .start()     : schedule the next cycle (no-op if already running)
    .stop()      : cancel the background task
    .run_close_sequence_now()  : test hook — run the close sequence out of band
    .set_mode_now(mode)        : test hook — force a mode transition + broadcast

Window times are interpreted in MAINTENANCE_TIMEZONE (default America/New_York).
If MAINTENANCE_WINDOW_END <= MAINTENANCE_WINDOW_START, the window is treated as
crossing midnight (end is on the next calendar day).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import webhook as webhook_module
from .config import settings
from .database import get_session
from .models import Order, Position, Signal

log = logging.getLogger(__name__)

# Per-position wait for the market close to fill before we move on.
CLOSE_FILL_TIMEOUT_SECONDS = 30
CLOSE_FILL_POLL_INTERVAL_SECONDS = 0.5


def _parse_hhmm(raw: str) -> time:
    h, m = raw.strip().split(":", 1)
    return time(int(h), int(m))


class MaintenanceScheduler:
    def __init__(self, ibkr, ws_manager) -> None:
        self.ibkr = ibkr
        self.ws_manager = ws_manager
        self.mode: str = "normal"
        self.message: str = ""
        self.resumes_at: Optional[str] = None

        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._tz: ZoneInfo = ZoneInfo(settings().maintenance_timezone)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())
        log.info("maintenance_scheduler_started")

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        log.info("maintenance_scheduler_stopped")

    # ------------------------------------------------------------------
    # Scheduler loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                pre_close_dt, window_start_dt, window_end_dt = self._compute_next_window()
                log.info(
                    "maintenance_next_window",
                    extra={
                        "pre_close": pre_close_dt.isoformat(),
                        "window_start": window_start_dt.isoformat(),
                        "window_end": window_end_dt.isoformat(),
                    },
                )

                # Phase A: wait until pre_close.
                if not await self._sleep_until(pre_close_dt):
                    return

                # Phase B: pre_close — run the close sequence.
                await self._enter_pre_close(window_end_dt)
                await self._run_close_sequence()

                # Phase C: wait until window_start, then flip to "maintenance".
                if not await self._sleep_until(window_start_dt):
                    return
                await self._enter_maintenance(window_end_dt)

                # Phase D: wait until window_end, then resume.
                if not await self._sleep_until(window_end_dt):
                    return
                await self._exit_maintenance()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("maintenance_scheduler_crashed", extra={"error": str(e)})

    async def _sleep_until(self, target_utc: datetime) -> bool:
        """Sleep until `target_utc`. Returns False if stopped before arrival."""
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            remaining = (target_utc - now).total_seconds()
            if remaining <= 0:
                return True
            # Cap each wait so the loop stays responsive to `.stop()`.
            wait_for = min(remaining, 30.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_for)
                return False
            except asyncio.TimeoutError:
                continue
        return False

    # ------------------------------------------------------------------
    # Window math
    # ------------------------------------------------------------------

    def _compute_next_window(self) -> tuple[datetime, datetime, datetime]:
        """Return (pre_close_utc, window_start_utc, window_end_utc).

        Chooses today's window if pre_close is still in the future, otherwise
        the next day's window. Handles windows that cross midnight.
        """
        cfg = settings()
        start_t = _parse_hhmm(cfg.maintenance_window_start)
        end_t = _parse_hhmm(cfg.maintenance_window_end)
        minutes_before = max(0, int(cfg.maintenance_close_minutes_before))

        now_local = datetime.now(self._tz)
        candidate_day = now_local.date()
        for _ in range(2):
            start_local = datetime.combine(candidate_day, start_t, tzinfo=self._tz)
            if end_t <= start_t:
                # Window crosses midnight.
                end_local = datetime.combine(candidate_day + timedelta(days=1), end_t, tzinfo=self._tz)
            else:
                end_local = datetime.combine(candidate_day, end_t, tzinfo=self._tz)
            pre_close_local = start_local - timedelta(minutes=minutes_before)
            if pre_close_local > now_local:
                return (
                    pre_close_local.astimezone(timezone.utc),
                    start_local.astimezone(timezone.utc),
                    end_local.astimezone(timezone.utc),
                )
            candidate_day = candidate_day + timedelta(days=1)
        # Fallback: scheduler can't find a future window (shouldn't happen).
        raise RuntimeError("maintenance_window_math_error")

    # ------------------------------------------------------------------
    # Mode transitions
    # ------------------------------------------------------------------

    async def _enter_pre_close(self, window_end_utc: datetime) -> None:
        webhook_module.set_accepting_signals(False)
        self.mode = "pre_close"
        self.resumes_at = window_end_utc.isoformat()
        self.message = "PRE-MAINTENANCE CLOSE IN PROGRESS — Closing all positions"
        log.info("maintenance_pre_close_start")
        await self._broadcast_status()

    async def _enter_maintenance(self, window_end_utc: datetime) -> None:
        webhook_module.set_accepting_signals(False)
        self.mode = "maintenance"
        self.resumes_at = window_end_utc.isoformat()
        self.message = f"MAINTENANCE MODE — No orders will be accepted until {self.resumes_at}"
        log.info("maintenance_window_start")
        await self._broadcast_status()

    async def _exit_maintenance(self) -> None:
        self.mode = "normal"
        self.resumes_at = None
        self.message = ""
        webhook_module.set_accepting_signals(True)
        log.info("maintenance_window_end")
        await self._broadcast_status()

    async def _broadcast_status(self) -> None:
        if self.ws_manager is None:
            return
        try:
            await self.ws_manager.broadcast(
                "maintenance_status",
                {"mode": self.mode, "message": self.message, "resumes_at": self.resumes_at},
            )
        except Exception as e:
            log.warning("maintenance_broadcast_failed", extra={"error": str(e)})

    # ------------------------------------------------------------------
    # Close sequence
    # ------------------------------------------------------------------

    async def _run_close_sequence(self) -> None:
        """Cancel trails, close positions, cancel stragglers, verify."""
        # 1. Cancel every active trailing stop.
        trail_orders = await self._active_trail_orders()
        for t in trail_orders:
            await self._cancel_order(t)

        # 2. Close every open position with a market order.
        positions = await self._open_positions()
        remaining_positions: list[int] = []
        for pos in positions:
            closed_ok = await self._close_position(pos)
            if not closed_ok:
                remaining_positions.append(pos.id)

        # 3. Cancel any in-flight entry orders that never filled.
        leftover_entries = await self._inflight_entries()
        for o in leftover_entries:
            await self._cancel_order(o)

        # 4. Verify all positions are closed.
        still_open = await self._open_positions()
        if still_open:
            ids = [p.id for p in still_open]
            log.error(
                "maintenance_close_failed",
                extra={"unclosed_position_ids": ids, "count": len(ids)},
            )
            if self.ws_manager is not None:
                try:
                    await self.ws_manager.broadcast(
                        "maintenance_close_failed",
                        {"unclosed_position_ids": ids, "count": len(ids)},
                    )
                except Exception as e:
                    log.warning("maintenance_broadcast_failed", extra={"error": str(e)})
        else:
            log.info(
                "maintenance_close_complete",
                extra={
                    "positions_closed": len(positions),
                    "trails_cancelled": len(trail_orders),
                    "entries_cancelled": len(leftover_entries),
                },
            )

    async def _active_trail_orders(self) -> list[Order]:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(Order)
                    .where(Order.order_role == "trail_stop")
                    .where(Order.status.in_(("submitted", "working", "partially_filled")))
                )
            ).scalars().all()
        return list(rows)

    async def _inflight_entries(self) -> list[Order]:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(Order)
                    .where(Order.order_role == "entry")
                    .where(Order.status.in_(("submitted", "partially_filled")))
                )
            ).scalars().all()
        return list(rows)

    async def _open_positions(self) -> list[Position]:
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(Position).where(Position.qty > 0)
                )
            ).scalars().all()
        return list(rows)

    async def _cancel_order(self, order: Order) -> None:
        if order.ibkr_order_id is None:
            return
        try:
            await self.ibkr.cancel_order(order.ibkr_order_id)
        except Exception as e:
            log.warning(
                "maintenance_cancel_order_error",
                extra={"order_id": order.id, "error": str(e)},
            )
        now = datetime.now(timezone.utc)
        async with get_session() as session:
            o = await session.get(Order, order.id)
            if o is not None and o.status not in ("filled", "cancelled"):
                o.status = "cancelled"
                o.cancelled_at = now
                o.error_msg = "maintenance_cancel"
                await session.commit()

    async def _close_position(self, pos: Position) -> bool:
        """Place a market close for `pos` and wait up to the timeout for fill.

        Returns True if the position reaches qty == 0 within the timeout.
        Creates a stub Signal + exit Order row up front so the fill flowing
        through order_router._on_fill correlates back to an Order (and not
        the orphan-trail path).
        """
        close_action = "SELL" if pos.direction == "long" else "BUY"
        qty = pos.qty
        now = datetime.now(timezone.utc)
        async with get_session() as session:
            stub_sig = Signal(
                received_at=now,
                raw_action="maintenance_close",
                symbol=pos.symbol,
                interval=pos.interval,
                strategy="ldc",
                status="informational",
                parse_format="plaintext",
                raw_body=f"maintenance_close position_id={pos.id}",
            )
            session.add(stub_sig)
            await session.flush()
            order = Order(
                signal_id=stub_sig.id,
                ibkr_order_id=None,
                symbol=pos.symbol,
                action=close_action,
                qty=qty,
                order_type="MKT",
                direction=pos.direction,
                order_role="exit",
                status="submitted",
                submitted_at=now,
                signal_close_price=None,
                interval=pos.interval,
                created_at=now,
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            order_id = order.id
        try:
            ibkr_id = await self.ibkr.place_market(pos.symbol, close_action, qty)
        except Exception as e:
            log.warning(
                "maintenance_place_market_error",
                extra={"position_id": pos.id, "symbol": pos.symbol, "error": str(e)},
            )
            return False
        if ibkr_id is None:
            log.error(
                "maintenance_place_market_failed",
                extra={"position_id": pos.id, "symbol": pos.symbol},
            )
            async with get_session() as session:
                o = await session.get(Order, order_id)
                if o is not None:
                    o.status = "error"
                    o.error_msg = "placeOrder failed"
                    await session.commit()
            return False
        # Stamp the ibkr_order_id so the fill handler can correlate.
        async with get_session() as session:
            o = await session.get(Order, order_id)
            if o is not None:
                o.ibkr_order_id = ibkr_id
                await session.commit()

        # Poll the DB until qty drops to 0 or the timeout expires.
        # The order_router's fill handler owns the state transitions; we just
        # wait for them to land.
        deadline = asyncio.get_event_loop().time() + CLOSE_FILL_TIMEOUT_SECONDS
        while asyncio.get_event_loop().time() < deadline:
            async with get_session() as session:
                cur = await session.get(Position, pos.id)
                if cur is None or cur.qty == 0:
                    log.info(
                        "maintenance_position_closed",
                        extra={
                            "position_id": pos.id,
                            "symbol": pos.symbol,
                            "direction": pos.direction,
                            "qty": qty,
                        },
                    )
                    return True
            await asyncio.sleep(CLOSE_FILL_POLL_INTERVAL_SECONDS)

        log.error(
            "maintenance_position_close_timeout",
            extra={
                "position_id": pos.id,
                "symbol": pos.symbol,
                "direction": pos.direction,
                "qty": qty,
                "timeout_seconds": CLOSE_FILL_TIMEOUT_SECONDS,
            },
        )
        return False

    # ------------------------------------------------------------------
    # Test hooks
    # ------------------------------------------------------------------

    async def run_close_sequence_now(self, window_end_utc: Optional[datetime] = None) -> None:
        """Force a close sequence out of band (used by tests and manual ops)."""
        end = window_end_utc or (datetime.now(timezone.utc) + timedelta(minutes=30))
        await self._enter_pre_close(end)
        await self._run_close_sequence()

    async def set_mode_now(self, mode: str, window_end_utc: Optional[datetime] = None) -> None:
        if mode == "pre_close":
            await self._enter_pre_close(window_end_utc or datetime.now(timezone.utc))
        elif mode == "maintenance":
            await self._enter_maintenance(window_end_utc or datetime.now(timezone.utc))
        elif mode == "normal":
            await self._exit_maintenance()
        else:
            raise ValueError(f"unknown mode: {mode}")


__all__ = ["MaintenanceScheduler"]
