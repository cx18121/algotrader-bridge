"""WebSocket feed: /ws/feed endpoint, ConnectionManager, snapshot on connect, heartbeat.

Message envelope: {"type": str, "data": dict, "ts": ISO8601}.

Snapshot (sent immediately after accept):
  {"type": "snapshot", "data": {
      "signals": [SignalOut...],          last 50, newest first
      "orders":  [OrderOut...],           last 50, newest first
      "positions": [PositionOut...],      open only, with trail fields
      "account": AccountOut|null,         most recent snapshot
      "tws_status": {"connected": bool, "reason": str|null},
      "maintenance_status": {"mode": str, "message": str, "resumes_at": ISO|null},
      "accepting_signals": bool
  }}

Streamed event types are emitted by order_router / main lifespan / maintenance:
  signal | order_update | fill | position_update | account_update |
  tws_status | order_replaced | orphan_trail_warning | maintenance_status | heartbeat
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from . import webhook as webhook_module
from .config import settings
from .database import get_session
from .models import AccountSnapshot, Order, Position, Signal
from .schemas import AccountOut, OrderOut, PositionOut, SignalOut

log = logging.getLogger(__name__)
router = APIRouter()

HEARTBEAT_INTERVAL_SECONDS = 30
SEND_TIMEOUT_SECONDS = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _envelope(type_: str, data: dict[str, Any]) -> str:
    return json.dumps({"type": type_, "data": data, "ts": _now_iso()}, default=str)


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stopping = False

    # ---------- client lifecycle ----------

    async def register(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)
        log.info("ws_client_connected", extra={"client_count": len(self._clients)})

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
        log.info("ws_client_disconnected", extra={"client_count": len(self._clients)})

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ---------- send helpers ----------

    async def send_to(self, ws: WebSocket, type_: str, data: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(ws.send_text(_envelope(type_, data)), timeout=SEND_TIMEOUT_SECONDS)
        except (WebSocketDisconnect, RuntimeError, asyncio.TimeoutError, Exception) as e:
            log.warning("ws_send_failed", extra={"event_type": type_, "error": str(e)})
            await self.unregister(ws)
            try:
                await ws.close()
            except Exception:
                pass

    async def broadcast(self, type_: str, data: dict[str, Any]) -> None:
        if not self._clients:
            return
        msg = _envelope(type_, data)
        # Snapshot the client set so we can iterate without holding the lock during sends.
        clients = list(self._clients)
        results = await asyncio.gather(
            *(self._safe_send(ws, msg) for ws in clients), return_exceptions=True
        )
        # Drop any client whose send raised.
        dead = [ws for ws, res in zip(clients, results) if isinstance(res, BaseException) or res is False]
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
            for ws in dead:
                try:
                    await ws.close()
                except Exception:
                    pass
            log.info("ws_dead_clients_pruned", extra={"dropped": len(dead), "client_count": len(self._clients)})

    async def _safe_send(self, ws: WebSocket, msg: str) -> bool:
        try:
            await asyncio.wait_for(ws.send_text(msg), timeout=SEND_TIMEOUT_SECONDS)
            return True
        except Exception:
            return False

    # ---------- heartbeat ----------

    def start_heartbeat(self) -> None:
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if self._clients:
                    await self.broadcast("heartbeat", {"server_time": _now_iso()})
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("ws_heartbeat_error", extra={"error": str(e)})

    async def stop(self) -> None:
        self._stopping = True
        t = self._heartbeat_task
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        # Close all live clients.
        async with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for ws in clients:
            try:
                await ws.close()
            except Exception:
                pass


# ---------------- Auth ----------------

def _ws_token_ok(token: Optional[str], client_host: Optional[str]) -> tuple[bool, str]:
    """Return (ok, reason). PRD: token query param. We honor dashboard_auth mode:
      - none           -> always ok
      - ip_allowlist   -> client IP must be in DASHBOARD_ALLOWED_IPS
      - basic_auth     -> token must equal DASHBOARD_PASSWORD (constant-time)
    """
    cfg = settings()
    mode = cfg.dashboard_auth
    if mode == "none":
        return True, ""
    if mode == "ip_allowlist":
        if client_host and client_host in cfg.dashboard_allowed_ips:
            return True, ""
        return False, f"ip {client_host} not in allowlist"
    if mode == "basic_auth":
        expected = (cfg.dashboard_password or "").encode()
        provided = (token or "").encode()
        if expected and secrets.compare_digest(expected, provided):
            return True, ""
        return False, "invalid token"
    return False, f"unknown auth mode: {mode}"


# ---------------- Snapshot builder ----------------

async def _build_snapshot() -> dict[str, Any]:
    """Read current state for a freshly-connected client."""
    # Lazy imports of app-state-derived values to avoid a circular import.
    from .main import get_state
    state = get_state()
    ibkr = state.get("ibkr")
    maint = state.get("maintenance")

    async with get_session() as session:
        sig_rows = (await session.execute(
            select(Signal).order_by(Signal.received_at.desc()).limit(50)
        )).scalars().all()
        ord_rows = (await session.execute(
            select(Order).order_by(Order.created_at.desc()).limit(50)
        )).scalars().all()
        pos_rows = (await session.execute(
            select(Position).where(Position.qty != 0).order_by(Position.last_updated.desc())
        )).scalars().all()
        # Resolve trail order per open position.
        positions: list[dict] = []
        for p in pos_rows:
            trail_stmt = (
                select(Order)
                .where(Order.symbol == p.symbol)
                .where(Order.direction == p.direction)
                .where(Order.interval == p.interval)
                .where(Order.order_role == "trail_stop")
                .where(Order.status.in_(("submitted", "working")))
                .order_by(Order.created_at.desc())
                .limit(1)
            )
            trail = (await session.execute(trail_stmt)).scalars().first()
            d = PositionOut.model_validate(p).model_dump(mode="json")
            if trail is not None:
                d["trail_order_id"] = trail.id
                d["trail_amount"] = trail.trail_amount
                d["trail_stop_price"] = trail.trail_stop_price
            positions.append(d)

        acct_row = (await session.execute(
            select(AccountSnapshot).order_by(AccountSnapshot.snapshot_time.desc()).limit(1)
        )).scalars().first()

    account = (
        AccountOut.model_validate(acct_row).model_dump(mode="json") if acct_row else None
    )

    tws_status = {
        "connected": bool(getattr(ibkr, "connected", False)),
        "reason": getattr(ibkr, "last_disconnect_reason", None),
    }

    maintenance_status = {
        "mode": getattr(maint, "mode", "normal") if maint else "normal",
        "message": getattr(maint, "message", "") if maint else "",
        "resumes_at": getattr(maint, "resumes_at", None) if maint else None,
    }

    return {
        "signals": [SignalOut.model_validate(s).model_dump(mode="json") for s in sig_rows],
        "orders": [OrderOut.model_validate(o).model_dump(mode="json") for o in ord_rows],
        "positions": positions,
        "account": account,
        "tws_status": tws_status,
        "maintenance_status": maintenance_status,
        "accepting_signals": webhook_module.accepting_signals(),
    }


# ---------------- /ws/feed ----------------

@router.websocket("/ws/feed")
async def ws_feed(websocket: WebSocket, token: Optional[str] = Query(default=None)) -> None:
    client_host = websocket.client.host if websocket.client else None
    ok, reason = _ws_token_ok(token, client_host)
    if not ok:
        log.info("ws_auth_rejected", extra={"reason": reason, "client": client_host})
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()

    from .main import get_state
    manager: Optional[ConnectionManager] = get_state().get("ws_manager")
    if manager is None:
        log.error("ws_manager_missing_at_connect")
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    await manager.register(websocket)
    try:
        snapshot = await _build_snapshot()
        await manager.send_to(websocket, "snapshot", snapshot)

        # Read loop: we don't expect client traffic, but keep the connection open
        # and respond to client ping/close. Any received text is ignored except
        # an explicit {"type":"ping"} which we echo back as pong.
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                msg = json.loads(raw)
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    await manager.send_to(websocket, "pong", {"server_time": _now_iso()})
            except Exception:
                # Ignore malformed client messages.
                pass
    finally:
        await manager.unregister(websocket)


__all__ = ["ConnectionManager", "router"]
