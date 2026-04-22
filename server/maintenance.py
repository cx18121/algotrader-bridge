"""Maintenance window scheduler (PRD Rule 3).

Supports multiple recurring windows:
  * Daily windows  — repeat every day at HH:MM–HH:MM (cross-midnight OK).
  * Weekly windows — anchored to specific days of week (e.g. Fri 16:00 – Sun 17:00).

For each cycle the scheduler:
  1. Finds the soonest upcoming pre-close moment across all enabled windows.
  2. Pre-close: stops signals, cancels trails, closes all positions.
  3. Window start: enters "maintenance" mode.
  4. Window end: resumes signals, returns to "normal".

Runtime surface:
  MaintenanceScheduler(ibkr, ws_manager)
    .mode        : "normal" | "pre_close" | "maintenance"
    .message     : human-readable status for the dashboard
    .resumes_at  : ISO8601 for the end of the current/next window (or None)
    .start()     : schedule the next cycle (no-op if already running)
    .stop()      : cancel the background task
    .run_close_sequence_now()  : test hook
    .set_mode_now(mode)        : test hook
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

CLOSE_FILL_TIMEOUT_SECONDS = 30
CLOSE_FILL_POLL_INTERVAL_SECONDS = 0.5

_DAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _parse_hhmm(raw: str) -> time:
    h, m = raw.strip().split(":", 1)
    return time(int(h), int(m))


# ------------------------------------------------------------------
# Window math helpers
# ------------------------------------------------------------------

def _next_daily_window(
    start_t: time, end_t: time, minutes_before: int, tz: ZoneInfo, now_local: datetime
) -> tuple[datetime, datetime, datetime]:
    """Return (pre_close_utc, start_utc, end_utc) for the next daily window."""
    candidate = now_local.date()
    for _ in range(3):
        start_local = datetime.combine(candidate, start_t, tzinfo=tz)
        if end_t <= start_t:
            end_local = datetime.combine(candidate + timedelta(days=1), end_t, tzinfo=tz)
        else:
            end_local = datetime.combine(candidate, end_t, tzinfo=tz)
        pre_close_local = start_local - timedelta(minutes=minutes_before)
        if pre_close_local > now_local:
            return (
                pre_close_local.astimezone(timezone.utc),
                start_local.astimezone(timezone.utc),
                end_local.astimezone(timezone.utc),
            )
        candidate += timedelta(days=1)
    raise RuntimeError("daily_window_math_error")


def _next_weekly_window(
    start_day: str, start_t: time,
    end_day: str, end_t: time,
    minutes_before: int, tz: ZoneInfo, now_local: datetime
) -> tuple[datetime, datetime, datetime]:
    """Return (pre_close_utc, start_utc, end_utc) for the next weekly window."""
    start_dow = _DAY_MAP[start_day.lower()]
    end_dow = _DAY_MAP[end_day.lower()]
    today = now_local.date()
    for offset in range(9):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() != start_dow:
            continue
        start_local = datetime.combine(candidate, start_t, tzinfo=tz)
        days_to_end = (end_dow - start_dow) % 7 or 7
        end_local = datetime.combine(candidate + timedelta(days=days_to_end), end_t, tzinfo=tz)
        pre_close_local = start_local - timedelta(minutes=minutes_before)
        if pre_close_local > now_local:
            return (
                pre_close_local.astimezone(timezone.utc),
                start_local.astimezone(timezone.utc),
                end_local.astimezone(timezone.utc),
            )
    raise RuntimeError("weekly_window_math_error")


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

                if not await self._sleep_until(pre_close_dt):
                    return
                await self._enter_pre_close(window_end_dt)
                await self._run_close_sequence()

                if not await self._sleep_until(window_start_dt):
                    return
                await self._enter_maintenance(window_end_dt)

                if not await self._sleep_until(window_end_dt):
                    return
                await self._exit_maintenance()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("maintenance_scheduler_crashed", extra={"error": str(e)})

    async def _sleep_until(self, target_utc: datetime) -> bool:
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            remaining = (target_utc - now).total_seconds()
            if remaining <= 0:
                return True
            wait_for = min(remaining, 30.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_for)
                return False
            except asyncio.TimeoutError:
                continue
        return False

    # ------------------------------------------------------------------
    # Window math — picks soonest upcoming window across all definitions
    # ------------------------------------------------------------------

    def _compute_next_window(self) -> tuple[datetime, datetime, datetime]:
        cfg = settings()
        minutes_before = max(0, int(cfg.maintenance_close_minutes_before))
        now_local = datetime.now(self._tz)
        candidates: list[tuple[datetime, datetime, datetime]] = []

        # Window 1: primary nightly (always enabled when scheduler is running).
        try:
            candidates.append(_next_daily_window(
                _parse_hhmm(cfg.maintenance_window_start),
                _parse_hhmm(cfg.maintenance_window_end),
                minutes_before, self._tz, now_local,
            ))
        except Exception as e:
            log.warning("maintenance_window1_math_error", extra={"error": str(e)})

        # Window 2: secondary daily (e.g. 17:00–18:00).
        if cfg.maintenance_window_2_enabled:
            try:
                candidates.append(_next_daily_window(
                    _parse_hhmm(cfg.maintenance_window_2_start),
                    _parse_hhmm(cfg.maintenance_window_2_end),
                    minutes_before, self._tz, now_local,
                ))
            except Exception as e:
                log.warning("maintenance_window2_math_error", extra={"error": str(e)})

        # Window 3: weekly (e.g. Fri 16:00 – Sun 17:00).
        if cfg.maintenance_weekend_enabled:
            try:
                candidates.append(_next_weekly_window(
                    cfg.maintenance_weekend_start_day,
                    _parse_hhmm(cfg.maintenance_weekend_start_time),
                    cfg.maintenance_weekend_end_day,
                    _parse_hhmm(cfg.maintenance_weekend_end_time),
                    minutes_before, self._tz, now_local,
                ))
            except Exception as e:
                log.warning("maintenance_weekend_math_error", extra={"error": str(e)})

        if not candidates:
            raise RuntimeError("no_maintenance_windows_computed")

        # Return the window whose pre_close fires soonest.
        return min(candidates, key=lambda t: t[0])

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
        trail_orders = await self._active_trail_orders()
        for t in trail_orders:
            await self._cancel_order(t)

        positions = await self._open_positions()
        for pos in positions:
            await self._close_position(pos)

        leftover_entries = await self._inflight_entries()
        for o in leftover_entries:
            await self._cancel_order(o)

        still_open = await self._open_positions()
        if still_open:
            ids = [p.id for p in still_open]
            log.error("maintenance_close_failed", extra={"unclosed_position_ids": ids, "count": len(ids)})
            if self.ws_manager is not None:
                try:
                    await self.ws_manager.broadcast("maintenance_close_failed", {"unclosed_position_ids": ids, "count": len(ids)})
                except Exception as e:
                    log.warning("maintenance_broadcast_failed", extra={"error": str(e)})
        else:
            log.info("maintenance_close_complete", extra={
                "positions_closed": len(positions),
                "trails_cancelled": len(trail_orders),
                "entries_cancelled": len(leftover_entries),
            })

    async def _active_trail_orders(self) -> list[Order]:
        async with get_session() as session:
            rows = (await session.execute(
                select(Order).where(Order.order_role == "trail_stop")
                .where(Order.status.in_(("submitted", "working", "partially_filled")))
            )).scalars().all()
        return list(rows)

    async def _inflight_entries(self) -> list[Order]:
        async with get_session() as session:
            rows = (await session.execute(
                select(Order).where(Order.order_role == "entry")
                .where(Order.status.in_(("submitted", "partially_filled")))
            )).scalars().all()
        return list(rows)

    async def _open_positions(self) -> list[Position]:
        async with get_session() as session:
            rows = (await session.execute(select(Position).where(Position.qty > 0))).scalars().all()
        return list(rows)

    async def _cancel_order(self, order: Order) -> None:
        if order.ibkr_order_id is None:
            return
        try:
            await self.ibkr.cancel_order(order.ibkr_order_id)
        except Exception as e:
            log.warning("maintenance_cancel_order_error", extra={"order_id": order.id, "error": str(e)})
        now = datetime.now(timezone.utc)
        async with get_session() as session:
            o = await session.get(Order, order.id)
            if o is not None and o.status not in ("filled", "cancelled"):
                o.status = "cancelled"
                o.cancelled_at = now
                o.error_msg = "maintenance_cancel"
                await session.commit()

    async def _close_position(self, pos: Position) -> bool:
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
            log.warning("maintenance_place_market_error", extra={"position_id": pos.id, "symbol": pos.symbol, "error": str(e)})
            return False
        if ibkr_id is None:
            log.error("maintenance_place_market_failed", extra={"position_id": pos.id, "symbol": pos.symbol})
            async with get_session() as session:
                o = await session.get(Order, order_id)
                if o is not None:
                    o.status = "error"
                    o.error_msg = "placeOrder failed"
                    await session.commit()
            return False
        async with get_session() as session:
            o = await session.get(Order, order_id)
            if o is not None:
                o.ibkr_order_id = ibkr_id
                await session.commit()

        deadline = asyncio.get_event_loop().time() + CLOSE_FILL_TIMEOUT_SECONDS
        while asyncio.get_event_loop().time() < deadline:
            async with get_session() as session:
                cur = await session.get(Position, pos.id)
                if cur is None or cur.qty == 0:
                    log.info("maintenance_position_closed", extra={"position_id": pos.id, "symbol": pos.symbol, "direction": pos.direction, "qty": qty})
                    return True
            await asyncio.sleep(CLOSE_FILL_POLL_INTERVAL_SECONDS)

        log.error("maintenance_position_close_timeout", extra={"position_id": pos.id, "symbol": pos.symbol, "direction": pos.direction, "qty": qty, "timeout_seconds": CLOSE_FILL_TIMEOUT_SECONDS})
        return False

    # ------------------------------------------------------------------
    # Test hooks
    # ------------------------------------------------------------------

    async def run_close_sequence_now(self, window_end_utc: Optional[datetime] = None) -> None:
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
