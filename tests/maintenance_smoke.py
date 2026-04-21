"""Phase 6 maintenance smoke test.

In-process exercise of the MaintenanceScheduler close sequence using the
MockIBKRClient. No HTTP or real TWS involved.

Flow:
  1. Spin up DB, MockIBKRClient, OrderRouter, ConnectionManager, scheduler.
  2. Push a synthetic open_long signal onto the router queue.
  3. Wait until the entry fills and the trailing stop is placed.
  4. Call scheduler.run_close_sequence_now() — asserts:
       - webhook.accepting_signals() flips to False
       - scheduler.mode == "pre_close"
       - the trail order is cancelled
       - the position qty drops to 0 within the timeout
       - maintenance_status event is broadcast
  5. Call scheduler.set_mode_now("normal") — asserts accepting_signals True again.

Run: source .venv/bin/activate && IBKR_MOCK=1 python tests/maintenance_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("WEBHOOK_SECRET", "smoke-test-secret")
os.environ.setdefault("DASHBOARD_AUTH", "none")
os.environ.setdefault("MAINTENANCE_WINDOW_ENABLED", "false")
os.environ.setdefault("DB_PATH", "./trading_maintenance_smoke.db")

from sqlalchemy import select

from server import webhook as webhook_module
from server.database import dispose_db, get_session, init_db
from server.ibkr import MockIBKRClient
from server.maintenance import MaintenanceScheduler
from server.models import Order, Position, Signal
from server.order_router import OrderRouter
from server.signal_parser import ParsedSignal
from server.websocket import ConnectionManager


# ----- Helpers -----

class _Recorder:
    """Stand-in for ws_manager that records broadcasts without a real WS."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def broadcast(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


async def _persist_signal(symbol: str, interval: str, raw_action: str) -> int:
    async with get_session() as session:
        s = Signal(
            received_at=datetime.now(timezone.utc),
            raw_action=raw_action,
            order_side="buy",
            position_action="open",
            direction="long",
            symbol=symbol,
            close_price=500.25,
            interval=interval,
            strategy="ldc",
            qty=None,
            status="accepted",
            parse_format="json",
            raw_body="{}",
        )
        session.add(s)
        await session.commit()
        await session.refresh(s)
        return s.id


async def _wait_until(pred, timeout_s: float, label: str) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if await pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for: {label}")


async def main() -> int:
    await init_db()
    try:
        # Wire up the bits that would normally be wired in main.lifespan.
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        webhook_module.set_signal_queue(queue)
        webhook_module.set_accepting_signals(True)

        recorder = _Recorder()
        webhook_module.set_broadcast(recorder.broadcast)

        ibkr = MockIBKRClient()
        await ibkr.start()

        router = OrderRouter(queue=queue, ibkr_client=ibkr, broadcast=recorder.broadcast)
        await router.start()

        scheduler = MaintenanceScheduler(ibkr=ibkr, ws_manager=recorder)

        symbol = f"MAINT{int(datetime.now().timestamp()) % 100000}"
        interval = "5m"
        sig_id = await _persist_signal(symbol, interval, "open_long")

        parsed = ParsedSignal(
            raw_action="open_long",
            order_side="buy",
            position_action="open",
            direction="long",
            symbol=symbol,
            close_price=500.25,
            interval=interval,
            strategy="ldc",
            qty=None,
            parse_format="json",
            secret=None,
        )
        await queue.put({"signal_id": sig_id, "parsed": parsed, "resolved_qty": 1})

        # Wait for entry to fill + trail to be placed.
        async def _pos_open() -> bool:
            async with get_session() as session:
                rows = (
                    await session.execute(
                        select(Position).where(Position.symbol == symbol).where(Position.qty > 0)
                    )
                ).scalars().all()
                return len(rows) == 1

        async def _trail_submitted() -> bool:
            async with get_session() as session:
                rows = (
                    await session.execute(
                        select(Order)
                        .where(Order.symbol == symbol)
                        .where(Order.order_role == "trail_stop")
                        .where(Order.status.in_(("submitted", "working")))
                    )
                ).scalars().all()
                return len(rows) == 1

        await _wait_until(_pos_open, 2.0, "position opened")
        await _wait_until(_trail_submitted, 2.0, "trail order submitted")
        print(f"  setup ok: position + trail open for {symbol}")

        # --- Close sequence ---
        assert webhook_module.accepting_signals() is True
        await scheduler.run_close_sequence_now()

        assert webhook_module.accepting_signals() is False, "accepting_signals should be False in pre_close"
        assert scheduler.mode == "pre_close", f"expected pre_close, got {scheduler.mode}"

        # Position should be closed.
        async def _pos_closed() -> bool:
            async with get_session() as session:
                rows = (
                    await session.execute(
                        select(Position).where(Position.symbol == symbol).where(Position.qty > 0)
                    )
                ).scalars().all()
                return len(rows) == 0

        await _wait_until(_pos_closed, 5.0, "position closed")

        # Trail should be cancelled.
        async with get_session() as session:
            active_trails = (
                await session.execute(
                    select(Order)
                    .where(Order.symbol == symbol)
                    .where(Order.order_role == "trail_stop")
                    .where(Order.status.in_(("submitted", "working", "partially_filled")))
                )
            ).scalars().all()
        assert not active_trails, f"trail not cancelled: {[o.status for o in active_trails]}"

        # At least one maintenance_status broadcast.
        maint_events = [e for e in recorder.events if e[0] == "maintenance_status"]
        assert maint_events, "no maintenance_status broadcast recorded"
        modes = [e[1]["mode"] for e in maint_events]
        assert "pre_close" in modes, f"pre_close not broadcast; modes={modes}"
        print(f"  pre_close ok: modes broadcast = {modes}")

        # --- Resume ---
        await scheduler.set_mode_now("normal")
        assert webhook_module.accepting_signals() is True
        assert scheduler.mode == "normal"
        modes2 = [e[1]["mode"] for e in recorder.events if e[0] == "maintenance_status"]
        assert "normal" in modes2, f"normal not broadcast; modes={modes2}"
        print("  resume ok: mode=normal, accepting_signals=True")

        # Cleanup.
        await router.stop()
        await ibkr.stop()
        scheduler.stop()
        print("ALL OK")
        return 0
    finally:
        await dispose_db()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
