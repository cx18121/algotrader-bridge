"""Phase 8 end-to-end test — the 9 PRD scenarios.

Spins the full FastAPI app in-process with an isolated SQLite DB and
IBKR_MOCK=1, drives webhook + REST through httpx.ASGITransport, and asserts
DB/API state for each scenario.

Scenarios (per claude_code_prompt.md Phase 8):
  1. FORMAT B open_long webhook for SPY @ 15m
  2. Signal row persisted with status=accepted
  3. Entry order row with status submitted/filled/partial
  4. close_long webhook for SPY @ 15m
  5. Close (exit) order row exists
  6. Duplicate open_long within dedup window -> deduplicated
  7. open_long while another is in-flight -> replacement recorded
  8. FORMAT A plaintext alert -> parsed correctly
  9. GET /api/stats/slippage -> response shape correct

Run: source .venv/bin/activate && python tests/e2e_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
import types
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "e2e-test-secret"
DB = ROOT / "trading_e2e.db"

os.environ["WEBHOOK_SECRET"] = SECRET
os.environ["DASHBOARD_AUTH"] = "none"
os.environ["MAINTENANCE_WINDOW_ENABLED"] = "false"
os.environ["IBKR_MOCK"] = "1"
os.environ["DB_PATH"] = str(DB)
os.environ["DEDUP_WINDOW_SECONDS"] = "30"
os.environ["LOG_LEVEL"] = "WARNING"

if DB.exists():
    DB.unlink()

import httpx  # noqa: E402

from server.config import reset_settings_for_tests  # noqa: E402
reset_settings_for_tests()

from server.main import app, get_state  # noqa: E402


# ---------------- helpers ----------------

async def fire_json(client: httpx.AsyncClient, payload: dict, *, header_secret: str | None = None) -> httpx.Response:
    headers = {"X-Webhook-Secret": header_secret} if header_secret else {}
    return await client.post("/webhook", json=payload, headers=headers)


async def fire_text(client: httpx.AsyncClient, body: str, *, header_secret: str) -> httpx.Response:
    return await client.post(
        "/webhook",
        content=body.encode("utf-8"),
        headers={"X-Webhook-Secret": header_secret, "Content-Type": "text/plain"},
    )


async def wait_until(pred, *, timeout: float = 3.0, step: float = 0.05, label: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await pred():
            return
        await asyncio.sleep(step)
    raise AssertionError(f"timeout waiting for: {label}")


# ---------------- scenarios ----------------

async def run() -> int:
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(app.router.lifespan_context(app))
        transport = httpx.ASGITransport(app=app)
        client = await stack.enter_async_context(
            httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10.0)
        )

        state = get_state()
        ibkr = state["ibkr"]
        assert ibkr is not None and ibkr.connected, "mock IBKR should be connected"

        # ---- 1. FORMAT B open_long SPY @ 15m ----
        print("[1] FORMAT B open_long SPY 15m")
        r1 = await fire_json(client, {
            "secret": SECRET,
            "action": "open_long",
            "symbol": "SPY",
            "close": 500.25,
            "interval": "15",
            "strategy": "ldc",
        })
        assert r1.status_code == 200, r1.text
        j1 = r1.json()
        assert j1["status"] == "accepted", j1
        sig1_id = j1["signal_id"]

        # ---- 2. signal persisted with status=accepted ----
        print("[2] verify signal row in DB")
        s = (await client.get(f"/api/signals/{sig1_id}")).json()
        assert s["status"] == "accepted", s
        assert s["symbol"] == "SPY" and s["raw_action"] == "open_long", s
        assert s["interval"] == "15m", s
        assert s["parse_format"] == "json", s

        # ---- 3. entry order row exists, status submitted/filled ----
        print("[3] verify SPY entry order")
        async def _entry_persisted() -> bool:
            rr = await client.get("/api/orders", params={
                "symbol": "SPY", "signal_id": sig1_id, "order_role": "entry",
            })
            return rr.status_code == 200 and len(rr.json()) >= 1
        await wait_until(_entry_persisted, label="SPY entry order persisted")
        entries = (await client.get("/api/orders", params={
            "symbol": "SPY", "signal_id": sig1_id, "order_role": "entry",
        })).json()
        assert entries, entries
        assert entries[0]["status"] in ("submitted", "partially_filled", "filled"), entries[0]
        assert entries[0]["direction"] == "long"
        assert entries[0]["interval"] == "15m"

        # Wait for entry to fill so scenario 4 (close) has a position to close.
        async def _spy_position_open() -> bool:
            rp = await client.get("/api/positions", params={"symbol": "SPY", "active_only": "true"})
            return rp.status_code == 200 and any(
                p["symbol"] == "SPY" and p["direction"] == "long" and p["qty"] > 0 for p in rp.json()
            )
        await wait_until(_spy_position_open, label="SPY position opened after fill")

        # ---- 4. close_long SPY @ 15m ----
        print("[4] close_long SPY 15m")
        r4 = await fire_json(client, {
            "secret": SECRET,
            "action": "close_long",
            "symbol": "SPY",
            "close": 503.10,
            "interval": "15",
            "strategy": "ldc",
        })
        assert r4.status_code == 200, r4.text
        j4 = r4.json()
        assert j4["status"] == "accepted", j4
        sig4_id = j4["signal_id"]

        # ---- 5. close (exit) order exists ----
        print("[5] verify SPY close order")
        async def _exit_persisted() -> bool:
            rr = await client.get("/api/orders", params={
                "symbol": "SPY", "signal_id": sig4_id, "order_role": "exit",
            })
            return rr.status_code == 200 and any(o["signal_id"] == sig4_id for o in rr.json())
        await wait_until(_exit_persisted, label="SPY exit order persisted")
        exits = (await client.get("/api/orders", params={
            "symbol": "SPY", "signal_id": sig4_id, "order_role": "exit",
        })).json()
        assert exits, exits
        assert exits[0]["action"] == "SELL", exits[0]
        assert exits[0]["direction"] == "long", exits[0]

        # ---- 6. duplicate open_long within dedup window -> deduplicated ----
        print("[6] duplicate open_long SPY 15m -> deduplicated")
        r6 = await fire_json(client, {
            "secret": SECRET,
            "action": "open_long",
            "symbol": "SPY",
            "close": 500.75,
            "interval": "15",
            "strategy": "ldc",
        })
        assert r6.status_code == 200, r6.text
        j6 = r6.json()
        assert j6["status"] == "deduplicated", j6
        assert j6["signal_id"] is not None, j6
        dup_row = (await client.get(f"/api/signals/{j6['signal_id']}")).json()
        assert dup_row["status"] == "deduped", dup_row

        # ---- 7. in-flight replacement on AAPL ----
        print("[7] replacement while AAPL entry in-flight")
        # Patch the mock's fill simulator to delay so the first entry stays
        # in "submitted" state long enough for the second webhook to collide.
        orig_simulate_fill = ibkr._simulate_fill

        async def _slow_simulate_fill(self, oid, symbol, action, qty, otype):  # noqa: ANN001
            await asyncio.sleep(0.8)
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

        ibkr._simulate_fill = types.MethodType(_slow_simulate_fill, ibkr)
        try:
            # Use distinct strategies so the dedup key (symbol, raw_action, strategy,
            # interval) differs — dedup would otherwise swallow the second webhook.
            # The router's inflight check is on (symbol, direction, interval) only,
            # so replacement still fires.
            r7a = await fire_json(client, {
                "secret": SECRET, "action": "open_long", "symbol": "AAPL",
                "close": 185.00, "interval": "5", "strategy": "ldc",
            })
            assert r7a.status_code == 200 and r7a.json()["status"] == "accepted", r7a.text
            sig7a = r7a.json()["signal_id"]

            # Give the router just enough time to consume signal 1 and persist
            # its entry Order before signal 2 arrives — but well under the 0.8s fill delay.
            await asyncio.sleep(0.1)

            r7b = await fire_json(client, {
                "secret": SECRET, "action": "open_long", "symbol": "AAPL",
                "close": 185.50, "interval": "5", "strategy": "ldc-retry",
            })
            assert r7b.status_code == 200 and r7b.json()["status"] == "accepted", r7b.text
            sig7b = r7b.json()["signal_id"]
            assert sig7b != sig7a

            async def _replacement_recorded() -> bool:
                rr = await client.get("/api/orders", params={
                    "symbol": "AAPL", "order_role": "entry",
                })
                if rr.status_code != 200:
                    return False
                orders = rr.json()
                old = next((o for o in orders if o["signal_id"] == sig7a), None)
                new = next((o for o in orders if o["signal_id"] == sig7b), None)
                if not old or not new:
                    return False
                # The old order should be linked to the new signal (replacement marker
                # survives the known _process_fill race that can re-mark status).
                return old.get("replaced_by_signal_id") == sig7b
            await wait_until(_replacement_recorded, timeout=3.0, label="AAPL replacement recorded")
        finally:
            # Let any pending slow fills complete, then restore the original method.
            await asyncio.sleep(1.0)
            ibkr._simulate_fill = orig_simulate_fill

        # ---- 8. FORMAT A plaintext QQQ @ 5m ----
        print("[8] FORMAT A plaintext QQQ 5m")
        plaintext = "LDC Open Long ▲ | QQQ@400.50 | (5)"
        r8 = await fire_text(client, plaintext, header_secret=SECRET)
        assert r8.status_code == 200, r8.text
        j8 = r8.json()
        assert j8["status"] == "accepted", j8
        sig8_id = j8["signal_id"]
        s8 = (await client.get(f"/api/signals/{sig8_id}")).json()
        assert s8["parse_format"] == "plaintext", s8
        assert s8["raw_action"] == "open_long", s8
        assert s8["symbol"] == "QQQ", s8
        assert s8["interval"] == "5m", s8
        assert s8["close_price"] == 400.50, s8

        # Wait for QQQ entry to fill so slippage has at least one data point.
        async def _qqq_filled() -> bool:
            rr = await client.get("/api/orders", params={
                "symbol": "QQQ", "signal_id": sig8_id, "order_role": "entry",
            })
            if rr.status_code != 200:
                return False
            return any(o["status"] == "filled" for o in rr.json())
        await wait_until(_qqq_filled, timeout=2.0, label="QQQ entry filled")

        # ---- 9. /api/stats/slippage response shape ----
        print("[9] /api/stats/slippage shape")
        rs = await client.get("/api/stats/slippage")
        assert rs.status_code == 200, rs.text
        js = rs.json()
        for key in (
            "filters",
            "total_fills",
            "avg_deviation_pts",
            "avg_deviation_pct",
            "max_deviation_pts",
            "min_deviation_pts",
            "pct_within_0_1",
            "pct_within_0_5",
            "pct_within_1_0",
            "pct_over_1_0",
            "by_interval",
        ):
            assert key in js, f"slippage response missing key {key!r}: {js}"
        assert isinstance(js["filters"], dict), js
        assert isinstance(js["by_interval"], list), js
        assert js["total_fills"] >= 1, js
        for row in js["by_interval"]:
            for k in ("interval", "total_fills", "avg_deviation_pts",
                      "avg_deviation_pct", "max_deviation_pts", "min_deviation_pts"):
                assert k in row, row

        print("ALL OK (9/9 scenarios passed)")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(run())
    except AssertionError as e:
        print(f"ASSERTION FAILED: {e}", file=sys.stderr)
        traceback.print_exc()
        rc = 1
    except Exception:
        traceback.print_exc()
        rc = 2
    sys.exit(rc)
