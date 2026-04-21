"""FastAPI app + lifespan: init DB, connect IBKR, start router, start maintenance."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import webhook as webhook_module
from .config import settings
from .database import init_db, dispose_db
from .ibkr import IBKRClient, MockIBKRClient
from .order_router import OrderRouter
from .webhook import router as webhook_router

log = logging.getLogger(__name__)


# ---- Global runtime references (set in lifespan) ----
app_state: dict = {
    "signal_queue": None,
    "ibkr": None,
    "router": None,
    "ws_manager": None,
    "maintenance": None,
    "start_time": None,
}


def _configure_logging() -> None:
    level = getattr(logging, settings().log_level, logging.INFO)
    # Structured-ish logging: emit JSON-formatted lines with key fields.
    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            import json
            payload = {
                "time": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "event": record.getMessage(),
                "context": {
                    k: v for k, v in record.__dict__.items()
                    if k not in (
                        "args", "asctime", "created", "exc_info", "exc_text", "filename",
                        "funcName", "levelname", "levelno", "lineno", "message", "module",
                        "msecs", "msg", "name", "pathname", "process", "processName",
                        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
                    )
                },
            }
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            return json.dumps(payload, default=str)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    # Replace handlers so uvicorn's default formatter doesn't double-write.
    root.handlers = [handler]
    root.setLevel(level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    log.info("server_starting")
    cfg = settings()
    app_state["start_time"] = datetime.now(timezone.utc)

    # DB
    await init_db()

    # Signal queue + router
    signal_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    app_state["signal_queue"] = signal_queue
    webhook_module.set_signal_queue(signal_queue)

    # WebSocket manager (lazy import avoids circular).
    from .websocket import ConnectionManager
    ws_manager = ConnectionManager()
    app_state["ws_manager"] = ws_manager
    ws_manager.start_heartbeat()
    webhook_module.set_broadcast(ws_manager.broadcast)

    # IBKR client — use mock if env var set (for tests).
    use_mock = os.getenv("IBKR_MOCK", "").lower() in ("1", "true", "yes")
    ibkr: IBKRClient
    if use_mock:
        log.info("ibkr_using_mock")
        ibkr = MockIBKRClient()
    else:
        ibkr = IBKRClient()

    # Wire status updates to the websocket feed.
    async def _on_status(connected: bool, reason: Optional[str]) -> None:
        await ws_manager.broadcast("tws_status", {"connected": connected, "reason": reason})
    ibkr.on_status = _on_status

    router = OrderRouter(
        queue=signal_queue,
        ibkr_client=ibkr,
        broadcast=lambda t, d: ws_manager.broadcast(t, d),
    )
    app_state["ibkr"] = ibkr
    app_state["router"] = router

    await ibkr.start()
    await router.start()

    # Maintenance scheduler.
    from .maintenance import MaintenanceScheduler
    maint = MaintenanceScheduler(ibkr=ibkr, ws_manager=ws_manager)
    app_state["maintenance"] = maint
    if cfg.maintenance_window_enabled:
        maint.start()

    # Periodic account snapshot.
    async def _snapshot_loop():
        while True:
            try:
                await asyncio.sleep(cfg.account_snapshot_interval_seconds)
                if ibkr.connected:
                    summary = await ibkr.get_account_summary()
                    if summary:
                        from .database import get_session
                        from .models import AccountSnapshot
                        async with get_session() as session:
                            snap = AccountSnapshot(
                                snapshot_time=datetime.now(timezone.utc),
                                net_liquidation=summary.get("net_liquidation"),
                                total_cash=summary.get("total_cash"),
                                unrealized_pnl=summary.get("unrealized_pnl"),
                                realized_pnl=summary.get("realized_pnl"),
                                equity_with_loan=summary.get("equity_with_loan"),
                            )
                            session.add(snap)
                            await session.commit()
                        await ws_manager.broadcast("account_update", summary)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("account_snapshot_error", extra={"error": str(e)})
    snapshot_task = asyncio.create_task(_snapshot_loop())
    app_state["snapshot_task"] = snapshot_task

    log.info("server_started")
    try:
        yield
    finally:
        log.info("server_stopping")
        snapshot_task.cancel()
        try:
            await snapshot_task
        except (asyncio.CancelledError, Exception):
            pass
        if maint:
            maint.stop()
        await router.stop()
        await ibkr.stop()
        await ws_manager.stop()
        await dispose_db()
        log.info("server_stopped")


app = FastAPI(title="AlgoTrader Bridge", version="1.0.0", lifespan=lifespan)

# Permissive CORS for the local dashboard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers.
app.include_router(webhook_router)

# Lazy-import to avoid import cycles.
from .api import router as api_router  # noqa: E402
from .websocket import router as ws_router  # noqa: E402

app.include_router(api_router)
app.include_router(ws_router)


# Serve static dashboard at /.
_DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "dashboard", "static")
_DASHBOARD_DIR = os.path.abspath(_DASHBOARD_DIR)
if os.path.isdir(_DASHBOARD_DIR):
    app.mount("/static", StaticFiles(directory=_DASHBOARD_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def _index(request: Request):
        from .api import _session_cookie_ok
        cfg = settings()
        if cfg.dashboard_auth == "basic_auth":
            session = request.cookies.get("session")
            if not _session_cookie_ok(session):
                from fastapi.responses import RedirectResponse
                return RedirectResponse("/login", status_code=302)
        index_path = os.path.join(_DASHBOARD_DIR, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path)
        return JSONResponse({"error": "dashboard not built"}, status_code=503)

    @app.get("/login", include_in_schema=False)
    async def _login():
        login_path = os.path.join(_DASHBOARD_DIR, "login.html")
        if os.path.isfile(login_path):
            return FileResponse(login_path)
        return JSONResponse({"error": "login page not found"}, status_code=503)


# Expose app_state to the API module via import-safe accessor.
def get_state() -> dict:
    return app_state


if __name__ == "__main__":
    import uvicorn
    cfg = settings()
    uvicorn.run("server.main:app", host=cfg.server_host, port=cfg.server_port, reload=False)
