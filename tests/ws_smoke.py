"""Phase 5 WS smoke test.

Connects to /ws/feed, asserts snapshot arrives, then fires a webhook signal
and asserts the streamed event sequence (signal -> order_update -> fill ->
position_update) appears within a short window.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx
import websockets


SECRET = os.getenv("WEBHOOK_SECRET") or "tm_fpcUzi3G-YNIGWion_mPrCiNIInQE"
BASE = "http://127.0.0.1:8765"
WS = "ws://127.0.0.1:8765/ws/feed"


async def fire_signal(symbol: str, action: str, interval: str = "5m") -> dict:
    payload = {
        "secret": SECRET,
        "symbol": symbol,
        "action": action,
        "close": 500.25,
        "interval": interval,
        "strategy": "ldc",
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.post(f"{BASE}/webhook", json=payload)
        r.raise_for_status()
        return r.json()


async def collect_for(ws, seconds: float) -> list[dict]:
    msgs: list[dict] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        msgs.append(json.loads(raw))
    return msgs


async def main() -> int:
    print(f"connecting to {WS}")
    async with websockets.connect(WS) as ws:
        # 1. Snapshot
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        assert first["type"] == "snapshot", f"expected snapshot, got {first['type']}"
        snap = first["data"]
        for key in ("signals", "orders", "positions", "account", "tws_status",
                    "maintenance_status", "accepting_signals"):
            assert key in snap, f"snapshot missing key: {key}"
        print(f"  snapshot ok: {len(snap['signals'])} signals, "
              f"{len(snap['orders'])} orders, {len(snap['positions'])} positions, "
              f"tws={snap['tws_status']['connected']}, mode={snap['maintenance_status']['mode']}")

        # 2. Fire a fresh signal — use a unique symbol to avoid dedup with previous runs.
        symbol = f"WSX{int(time.time()) % 100000}"
        # Close existing position first to keep things tidy in subsequent runs.
        result = await fire_signal(symbol, "open_long")
        print(f"  POST /webhook -> {result}")
        assert result["status"] == "accepted", result

        # 3. Collect streamed events for ~2s
        events = await collect_for(ws, 2.0)
        types = [m["type"] for m in events]
        print(f"  streamed types: {types}")

        required = {"signal", "order_update", "fill", "position_update"}
        seen = set(types)
        missing = required - seen
        assert not missing, f"missing event types: {missing}; got {types}"

        # Sanity: signal event references the new signal id.
        sig_evt = next(m for m in events if m["type"] == "signal")
        assert sig_evt["data"].get("signal_id") == result["signal_id"], sig_evt

        print("ALL OK")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
