"""REST API endpoints: status, signals, orders, positions, account, slippage stats."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from sqlalchemy import and_, delete, func, select

from . import webhook as webhook_module
from .config import settings
from .database import get_session
from .models import AccountSnapshot, Fill, Order, Position, Signal, TradeHistory
from .schemas import (
    AccountOut,
    OrderOut,
    PositionOut,
    SignalDetailOut,
    SignalOut,
    SlippageByInterval,
    SlippageOut,
    StatusOut,
    TradeHistoryOut,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

_basic = HTTPBasic(auto_error=False)

SESSION_COOKIE = "session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days


def _compute_session_token() -> str:
    cfg = settings()
    key = (cfg.webhook_secret or "").encode()
    msg = f"{cfg.dashboard_username}:{cfg.dashboard_password}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _session_cookie_ok(cookie: Optional[str]) -> bool:
    if not cookie:
        return False
    return secrets.compare_digest(_compute_session_token().encode(), cookie.encode())


# ---------------- Auth dependency ----------------

async def _auth_guard(
    request: Request,
    creds: Optional[HTTPBasicCredentials] = Depends(_basic),
    session: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
) -> None:
    cfg = settings()
    mode = cfg.dashboard_auth
    if mode == "none":
        return
    if mode == "basic_auth" and _session_cookie_ok(session):
        return
    if mode == "ip_allowlist":
        client_ip = request.client.host if request.client else None
        if client_ip is None or client_ip not in cfg.dashboard_allowed_ips:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"ip {client_ip} not in allowlist",
            )
        return
    if mode == "basic_auth":
        if not creds:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="basic auth required",
                headers={"WWW-Authenticate": "Basic"},
            )
        user_ok = secrets.compare_digest(
            creds.username.encode(), (cfg.dashboard_username or "").encode()
        )
        pass_ok = secrets.compare_digest(
            creds.password.encode(), (cfg.dashboard_password or "").encode()
        )
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return


# ---------------- /api/health (public, for healthchecks) ----------------

@router.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok"}


# ---------------- /api/login & /api/logout (public) ----------------

class _LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login", include_in_schema=False)
async def login(body: _LoginIn, response: Response) -> dict:
    cfg = settings()
    if cfg.dashboard_auth != "basic_auth":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="login not available")
    user_ok = secrets.compare_digest(body.username.encode(), (cfg.dashboard_username or "").encode())
    pass_ok = secrets.compare_digest(body.password.encode(), (cfg.dashboard_password or "").encode())
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    token = _compute_session_token()
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True, samesite="lax", max_age=SESSION_MAX_AGE,
    )
    return {"status": "ok"}


@router.post("/logout", include_in_schema=False)
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "ok"}


# ---------------- /api/status ----------------

@router.get("/status", response_model=StatusOut, dependencies=[Depends(_auth_guard)])
async def get_status(request: Request) -> StatusOut:
    from .main import get_state
    state = get_state()
    ibkr = state.get("ibkr")
    maint = state.get("maintenance")
    start_time = state.get("start_time") or datetime.now(timezone.utc)
    uptime = int((datetime.now(timezone.utc) - start_time).total_seconds())

    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with get_session() as session:
        signals_today = (await session.execute(
            select(func.count(Signal.id)).where(Signal.received_at >= midnight)
        )).scalar_one()
        orders_today = (await session.execute(
            select(func.count(Order.id)).where(Order.created_at >= midnight)
        )).scalar_one()
        open_positions = (await session.execute(
            select(func.count(Position.id)).where(Position.qty != 0)
        )).scalar_one()
        active_rows = (await session.execute(
            select(Position.interval).where(Position.qty != 0).distinct()
        )).all()
        active_intervals = sorted({r[0] for r in active_rows if r[0]})

    maintenance_mode = "normal"
    maintenance_message = ""
    maintenance_resumes_at: Optional[str] = None
    if maint is not None:
        maintenance_mode = getattr(maint, "mode", "normal")
        maintenance_message = getattr(maint, "message", "") or ""
        maintenance_resumes_at = getattr(maint, "resumes_at", None)

    return StatusOut(
        server="ok",
        tws_connected=bool(getattr(ibkr, "connected", False)),
        tws_last_connected=getattr(ibkr, "last_connected_at", None),
        tws_disconnect_reason=getattr(ibkr, "last_disconnect_reason", None),
        uptime_seconds=uptime,
        signals_today=int(signals_today or 0),
        orders_today=int(orders_today or 0),
        open_positions=int(open_positions or 0),
        accepting_signals=webhook_module.accepting_signals(),
        maintenance_mode=maintenance_mode,
        maintenance_message=maintenance_message,
        maintenance_resumes_at=maintenance_resumes_at,
        active_intervals=active_intervals,
    )


# ---------------- /api/signals ----------------

@router.get("/signals", response_model=list[SignalOut], dependencies=[Depends(_auth_guard)])
async def list_signals(
    symbol: Optional[str] = None,
    raw_action: Optional[str] = None,
    status_: Optional[str] = Query(default=None, alias="status"),
    interval: Optional[str] = None,
    strategy: Optional[str] = None,
    from_: Optional[datetime] = Query(default=None, alias="from"),
    to: Optional[datetime] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[SignalOut]:
    stmt = select(Signal)
    if symbol:
        stmt = stmt.where(Signal.symbol == symbol.upper())
    if raw_action:
        stmt = stmt.where(Signal.raw_action == raw_action)
    if status_:
        stmt = stmt.where(Signal.status == status_)
    if interval:
        stmt = stmt.where(Signal.interval == interval)
    if strategy:
        stmt = stmt.where(Signal.strategy == strategy)
    if from_:
        stmt = stmt.where(Signal.received_at >= from_)
    if to:
        stmt = stmt.where(Signal.received_at <= to)
    stmt = stmt.order_by(Signal.received_at.desc()).limit(limit).offset(offset)
    async with get_session() as session:
        result = await session.execute(stmt)
        rows = result.scalars().all()
    return [SignalOut.model_validate(r) for r in rows]


@router.get("/signals/{signal_id}", response_model=SignalDetailOut, dependencies=[Depends(_auth_guard)])
async def get_signal(signal_id: int) -> SignalDetailOut:
    async with get_session() as session:
        sig = await session.get(Signal, signal_id)
        if sig is None:
            raise HTTPException(status_code=404, detail="signal not found")
        return SignalDetailOut.model_validate(sig)


# ---------------- /api/orders ----------------

@router.get("/orders", response_model=list[OrderOut], dependencies=[Depends(_auth_guard)])
async def list_orders(
    symbol: Optional[str] = None,
    status_: Optional[str] = Query(default=None, alias="status"),
    order_role: Optional[str] = None,
    signal_id: Optional[int] = None,
    interval: Optional[str] = None,
    from_: Optional[datetime] = Query(default=None, alias="from"),
    to: Optional[datetime] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[OrderOut]:
    stmt = select(Order)
    if symbol:
        stmt = stmt.where(Order.symbol == symbol.upper())
    if status_:
        stmt = stmt.where(Order.status == status_)
    if order_role:
        stmt = stmt.where(Order.order_role == order_role)
    if signal_id is not None:
        stmt = stmt.where(Order.signal_id == signal_id)
    if interval:
        stmt = stmt.where(Order.interval == interval)
    if from_:
        stmt = stmt.where(Order.created_at >= from_)
    if to:
        stmt = stmt.where(Order.created_at <= to)
    stmt = stmt.order_by(Order.created_at.desc()).limit(limit).offset(offset)
    async with get_session() as session:
        result = await session.execute(stmt)
        rows = result.scalars().all()
    return [OrderOut.model_validate(r) for r in rows]


@router.get("/orders/{order_id}", dependencies=[Depends(_auth_guard)])
async def get_order(order_id: int) -> dict:
    from .models import Fill
    async with get_session() as session:
        order = await session.get(Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        fills_result = await session.execute(
            select(Fill).where(Fill.order_id == order_id).order_by(Fill.fill_time.asc())
        )
        fills = fills_result.scalars().all()
    return {
        "order": OrderOut.model_validate(order).model_dump(mode="json"),
        "fills": [
            {
                "id": f.id,
                "ibkr_exec_id": f.ibkr_exec_id,
                "fill_qty": f.fill_qty,
                "fill_price": f.fill_price,
                "fill_time": f.fill_time.isoformat() if f.fill_time else None,
                "commission": f.commission,
                "exchange": f.exchange,
            }
            for f in fills
        ],
    }


# ---------------- /api/positions ----------------

@router.get("/positions", response_model=list[PositionOut], dependencies=[Depends(_auth_guard)])
async def list_positions(
    direction: Optional[str] = None,
    interval: Optional[str] = None,
    active_only: bool = True,
) -> list[PositionOut]:
    stmt = select(Position)
    if direction:
        stmt = stmt.where(Position.direction == direction)
    if interval:
        stmt = stmt.where(Position.interval == interval)
    if active_only:
        stmt = stmt.where(Position.qty != 0)
    stmt = stmt.order_by(Position.last_updated.desc())
    async with get_session() as session:
        result = await session.execute(stmt)
        rows = result.scalars().all()
        # Attach trail order details if an active trail exists for this position key.
        out: list[PositionOut] = []
        for p in rows:
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
            d = PositionOut.model_validate(p).model_dump()
            if trail is not None:
                d["trail_order_id"] = trail.id
                d["trail_amount"] = trail.trail_amount
                d["trail_stop_price"] = trail.trail_stop_price
            out.append(PositionOut(**d))
    return out


# ---------------- /api/account ----------------

@router.get("/account", response_model=AccountOut, dependencies=[Depends(_auth_guard)])
async def get_account() -> AccountOut:
    async with get_session() as session:
        stmt = select(AccountSnapshot).order_by(AccountSnapshot.snapshot_time.desc()).limit(1)
        result = await session.execute(stmt)
        snap = result.scalars().first()
    if snap is None:
        return AccountOut()
    return AccountOut(
        net_liquidation=snap.net_liquidation,
        total_cash=snap.total_cash,
        unrealized_pnl=snap.unrealized_pnl,
        realized_pnl=snap.realized_pnl,
        equity_with_loan=snap.equity_with_loan,
        snapshot_time=snap.snapshot_time,
    )


# ---------------- /api/stats/slippage ----------------

@router.get("/stats/slippage", response_model=SlippageOut, dependencies=[Depends(_auth_guard)])
async def slippage_stats(
    symbol: Optional[str] = None,
    interval: Optional[str] = None,
    from_: Optional[datetime] = Query(default=None, alias="from"),
    to: Optional[datetime] = None,
) -> SlippageOut:
    conds = [Order.fill_deviation_pts.is_not(None), Order.fill_qty > 0]
    if symbol:
        conds.append(Order.symbol == symbol.upper())
    if interval:
        conds.append(Order.interval == interval)
    if from_:
        conds.append(Order.fill_time >= from_)
    if to:
        conds.append(Order.fill_time <= to)

    async with get_session() as session:
        rows = (await session.execute(
            select(
                Order.interval,
                Order.fill_deviation_pts,
                Order.fill_deviation_pct,
            ).where(and_(*conds))
        )).all()

    if not rows:
        return SlippageOut(
            filters={"symbol": symbol, "interval": interval,
                     "from": from_.isoformat() if from_ else None,
                     "to": to.isoformat() if to else None},
            total_fills=0,
            avg_deviation_pts=0.0, avg_deviation_pct=0.0,
            max_deviation_pts=0.0, min_deviation_pts=0.0,
            pct_within_0_1=0.0, pct_within_0_5=0.0, pct_within_1_0=0.0, pct_over_1_0=0.0,
            by_interval=[],
        )

    pts = [float(r[1]) for r in rows]
    pcts = [float(r[2]) if r[2] is not None else 0.0 for r in rows]
    total = len(pts)
    abs_pcts = [abs(p) for p in pcts]

    def _pct_within(threshold: float) -> float:
        return 100.0 * sum(1 for x in abs_pcts if x <= threshold) / total

    by_int: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        key = r[0] or "unknown"
        by_int.setdefault(key, []).append((float(r[1]), float(r[2]) if r[2] is not None else 0.0))

    by_interval_out = []
    for ivl, vals in sorted(by_int.items()):
        p = [v[0] for v in vals]
        pc = [v[1] for v in vals]
        by_interval_out.append(SlippageByInterval(
            interval=ivl,
            total_fills=len(vals),
            avg_deviation_pts=sum(p) / len(p),
            avg_deviation_pct=sum(pc) / len(pc),
            max_deviation_pts=max(p),
            min_deviation_pts=min(p),
        ))

    return SlippageOut(
        filters={"symbol": symbol, "interval": interval,
                 "from": from_.isoformat() if from_ else None,
                 "to": to.isoformat() if to else None},
        total_fills=total,
        avg_deviation_pts=sum(pts) / total,
        avg_deviation_pct=sum(pcts) / total,
        max_deviation_pts=max(pts),
        min_deviation_pts=min(pts),
        pct_within_0_1=_pct_within(0.1),
        pct_within_0_5=_pct_within(0.5),
        pct_within_1_0=_pct_within(1.0),
        pct_over_1_0=100.0 * sum(1 for x in abs_pcts if x > 1.0) / total,
        by_interval=by_interval_out,
    )


# ---------------- /api/trade-history ----------------

@router.get("/trade-history", response_model=list[TradeHistoryOut], dependencies=[Depends(_auth_guard)])
async def list_trade_history(
    symbol: Optional[str] = None,
    direction: Optional[str] = None,
    interval: Optional[str] = None,
) -> list[TradeHistoryOut]:
    stmt = select(TradeHistory)
    if symbol:
        stmt = stmt.where(TradeHistory.symbol == symbol.upper())
    if direction:
        stmt = stmt.where(TradeHistory.direction == direction)
    if interval:
        stmt = stmt.where(TradeHistory.interval == interval)
    stmt = stmt.order_by(TradeHistory.closed_at.desc())
    async with get_session() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [TradeHistoryOut.model_validate(r) for r in rows]


# ---------------- /api/admin ----------------

class _ClosePositionIn(BaseModel):
    symbol: str
    direction: str
    interval: Optional[str] = None


@router.get("/admin/ibkr-positions", dependencies=[Depends(_auth_guard)])
async def admin_ibkr_positions() -> list:
    from .main import get_state
    ibkr = get_state().get("ibkr")
    if ibkr is None:
        return []
    return await ibkr.get_positions()


@router.post("/admin/close-position", dependencies=[Depends(_auth_guard)])
async def admin_close_position(body: _ClosePositionIn) -> dict:
    sig_id = await webhook_module.inject_close_signal(body.symbol, body.direction, body.interval)
    return {"status": "accepted", "signal_id": sig_id}


@router.post("/admin/clear-db", dependencies=[Depends(_auth_guard)])
async def admin_clear_db() -> dict:
    from .main import get_state
    ibkr = get_state().get("ibkr")

    # Close all open positions in IBKR before wiping the DB.
    closed: list[dict] = []
    failed: list[dict] = []
    if ibkr is not None:
        ibkr_positions = await ibkr.get_positions()
        for p in ibkr_positions:
            symbol = p.get("symbol", "")
            qty = int(p.get("position", 0))
            if qty == 0 or not symbol:
                continue
            action = "SELL" if qty > 0 else "BUY"
            order_id = await ibkr.place_market(symbol, action, abs(qty))
            if order_id:
                closed.append({"symbol": symbol, "qty": qty, "order_id": order_id})
                log.info("admin_clear_db_closed", extra={"symbol": symbol, "qty": qty})
            else:
                failed.append({"symbol": symbol, "qty": qty})
                log.warning("admin_clear_db_close_failed", extra={"symbol": symbol, "qty": qty})

    counts: dict[str, int] = {}
    async with get_session() as session:
        for model, name in [
            (Fill, "fills"), (Order, "orders"), (Signal, "signals"),
            (Position, "positions"), (AccountSnapshot, "account_snapshots"),
            (TradeHistory, "trade_history"),
        ]:
            result = await session.execute(delete(model))
            counts[name] = result.rowcount
        await session.commit()
    log.info("admin_clear_db", extra={"counts": counts})
    return {"status": "cleared", "counts": counts, "positions_closed": closed, "positions_failed": failed}


# ---------------- /api/contracts ----------------

from .models import ContractMap  # noqa: E402 -- imported here to avoid circular at top


class _ContractMapOut(BaseModel):
    tv_symbol: str
    ib_symbol: str
    sec_type: str
    exchange: str
    currency: str
    last_trade_date: Optional[str]
    updated_at: datetime

    model_config = {"from_attributes": True}


class _ContractMapIn(BaseModel):
    ib_symbol: str
    sec_type: str = "stock"
    exchange: str = "SMART"
    currency: str = "USD"
    last_trade_date: Optional[str] = None


@router.get("/contracts", dependencies=[Depends(_auth_guard)])
async def list_contracts() -> list[_ContractMapOut]:
    async with get_session() as session:
        rows = (await session.execute(select(ContractMap).order_by(ContractMap.tv_symbol))).scalars().all()
    return [_ContractMapOut.model_validate(r) for r in rows]


@router.put("/contracts/{tv_symbol}", dependencies=[Depends(_auth_guard)])
async def upsert_contract(tv_symbol: str, body: _ContractMapIn) -> _ContractMapOut:
    key = tv_symbol.upper()
    async with get_session() as session:
        row = (await session.execute(select(ContractMap).where(ContractMap.tv_symbol == key))).scalar_one_or_none()
        if row is None:
            row = ContractMap(tv_symbol=key)
            session.add(row)
        row.ib_symbol = body.ib_symbol
        row.sec_type = body.sec_type
        row.exchange = body.exchange
        row.currency = body.currency
        row.last_trade_date = body.last_trade_date
        await session.commit()
        await session.refresh(row)
    log.info("contract_map_upserted", extra={"tv_symbol": key})
    return _ContractMapOut.model_validate(row)


@router.delete("/contracts/{tv_symbol}", dependencies=[Depends(_auth_guard)])
async def delete_contract(tv_symbol: str) -> dict:
    key = tv_symbol.upper()
    async with get_session() as session:
        result = await session.execute(delete(ContractMap).where(ContractMap.tv_symbol == key))
        await session.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"No mapping for {key}")
    log.info("contract_map_deleted", extra={"tv_symbol": key})
    return {"status": "deleted", "tv_symbol": key}


__all__ = ["router"]
