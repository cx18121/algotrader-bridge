"""POST /webhook: auth, format detection, dedup, queue-push."""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, Request, Response, status
from sqlalchemy import select

from .config import settings
from .database import get_session
from .models import Position, Signal
from .schemas import WebhookResponse
from .signal_parser import (
    GENERIC_ACTIONS,
    ParsedSignal,
    SignalParseError,
    UnsupportedSignalError,
    _ACTION_META,
    parse_signal,
)

log = logging.getLogger(__name__)
router = APIRouter()


# Simple token-bucket rate limiter — per-process, fine for single-host deployment.
class _RateLimiter:
    def __init__(self, rps: int) -> None:
        self.capacity = max(1, rps)
        self.rps = max(1, rps)
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rps)
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False


_rate_limiter: Optional[_RateLimiter] = None


def _limiter() -> _RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = _RateLimiter(settings().webhook_rate_limit_rps)
    return _rate_limiter


# Injected at startup by main.py.
_signal_queue: Optional[asyncio.Queue] = None
_broadcast = None  # async callable: (event_type: str, data: dict) -> None
# Maintenance flag — toggled by the maintenance scheduler.
_accepting_signals: bool = True


def set_signal_queue(q: asyncio.Queue) -> None:
    global _signal_queue
    _signal_queue = q


def set_broadcast(fn) -> None:
    global _broadcast
    _broadcast = fn


def set_accepting_signals(accept: bool) -> None:
    global _accepting_signals
    _accepting_signals = accept


def accepting_signals() -> bool:
    return _accepting_signals


async def _push_signal(sig_id: int, parsed_or_none, status_: str, reason: Optional[str] = None) -> None:
    if _broadcast is None:
        return
    data = {"signal_id": sig_id, "status": status_}
    if reason:
        data["reason"] = reason
    if parsed_or_none is not None:
        data.update({
            "symbol": parsed_or_none.symbol,
            "raw_action": parsed_or_none.raw_action,
            "interval": parsed_or_none.interval,
            "direction": parsed_or_none.direction,
            "qty": parsed_or_none.qty,
            "close_price": parsed_or_none.close_price,
            "strategy": parsed_or_none.strategy,
        })
    try:
        result = _broadcast("signal", data)
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:
        log.warning("signal_broadcast_failed", extra={"signal_id": sig_id, "error": str(e)})


async def _find_recent_unclosed_open(symbol: str) -> Optional[Signal]:
    """Return the most recent accepted open_* Signal for `symbol` that has no
    matching close after it. Used to detect a failed/pending entry whose order
    never produced a Position row, so a follow-up generic alert in the opposite
    direction can be rejected instead of silently flipping intent."""
    async with get_session() as session:
        stmt = (
            select(Signal)
            .where(Signal.symbol == symbol)
            .where(Signal.status == "accepted")
            .where(Signal.raw_action.in_(("open_long", "open_short", "close_long", "close_short", "long", "short", "l-ts", "s-ts")))
            .order_by(Signal.received_at.desc())
            .limit(1)
        )
        last = (await session.execute(stmt)).scalar_one_or_none()
    if last is None:
        return None
    if last.raw_action in ("open_long", "open_short", "long", "short"):
        return last
    return None


async def _resolve_generic_action(parsed: ParsedSignal) -> Optional[str]:
    """Resolve generic Pine strategy 'buy'/'sell' actions to concrete directional actions
    using current DB position state. Mutates `parsed` in place.

    Under one-position-at-a-time semantics:
      buy  + flat           -> open_long
      sell + flat           -> open_short
      sell + long position  -> close_long
      buy  + short position -> close_short
      buy  + long position  -> error (would be pyramid/duplicate entry)
      sell + short position -> error (would be pyramid/duplicate entry)

    Returns a rejection reason string on ambiguous/invalid state, else None.
    """
    if parsed.raw_action not in GENERIC_ACTIONS:
        return None

    async with get_session() as session:
        stmt = (
            select(Position)
            .where(Position.symbol == parsed.symbol)
            .where(Position.qty > 0)
            .limit(1)
        )
        res = await session.execute(stmt)
        pos = res.scalar_one_or_none()

    raw = parsed.raw_action
    if pos is None:
        # No actual position — but a prior accepted open may have failed to fill
        # (e.g. contract unresolvable on IBKR). If so, a generic action in the
        # opposite direction would silently flip intent. Reject instead.
        recent_open = await _find_recent_unclosed_open(parsed.symbol)
        if recent_open is not None:
            recent_dir = "long" if recent_open.raw_action in ("open_long", "long") else "short"
            if (raw == "buy" and recent_dir == "short") or (raw == "sell" and recent_dir == "long"):
                return (
                    f"{raw!r} signal conflicts with recent unresolved "
                    f"{recent_open.raw_action} (signal id={recent_open.id}) on {parsed.symbol}"
                )
        resolved = "open_long" if raw == "buy" else "open_short"
    elif pos.direction == "long":
        if raw == "sell":
            resolved = "close_long"
        else:
            return f"{raw!r} signal received while long position already open for {parsed.symbol}"
    else:  # short
        if raw == "buy":
            resolved = "close_short"
        else:
            return f"{raw!r} signal received while short position already open for {parsed.symbol}"

    side, pos_action, direction = _ACTION_META[resolved]
    parsed.raw_action = resolved
    parsed.order_side = side
    parsed.position_action = pos_action
    parsed.direction = direction
    # For resolved closes, align interval with the position's interval so the
    # downstream (symbol, direction, interval) position lookup matches.
    if pos_action == "close" and pos is not None and pos.interval:
        parsed.interval = pos.interval
    return None


def _auth_ok(provided: Optional[str]) -> bool:
    if not provided:
        return False
    expected = settings().webhook_secret
    return hmac.compare_digest(provided, expected)


@router.post("/webhook", response_model=WebhookResponse)
async def handle_webhook(
    request: Request,
    response: Response,
    x_webhook_secret: Optional[str] = Header(default=None, alias="X-Webhook-Secret"),
):
    body = await request.body()
    source_ip = request.client.host if request.client else None
    content_type = request.headers.get("content-type", "").split(";")[0].strip() or None

    # Log without the secret.
    log.info(
        "webhook_received",
        extra={"source_ip": source_ip, "content_type": content_type, "bytes": len(body)},
    )

    # --- Rate limit ---
    if not await _limiter().take():
        response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
        return WebhookResponse(status="rate_limited", reason="rate limit exceeded")

    # --- Parse (needed to check body-secret fallback) ---
    try:
        parsed = parse_signal(body, content_type=content_type)
    except UnsupportedSignalError as e:
        # Record unsupported combined alerts but do not reject with 4xx.
        sig_id = await _store_unsupported(body, source_ip, reason=str(e))
        await _push_signal(sig_id, None, "unsupported", reason=str(e))
        return WebhookResponse(status="unsupported", signal_id=sig_id, reason=str(e))
    except SignalParseError as e:
        log.warning("webhook_parse_error", extra={"error": str(e), "body": body[:500]})
        response.status_code = status.HTTP_400_BAD_REQUEST
        return WebhookResponse(status="rejected", reason=str(e))

    # --- Auth: X-Webhook-Secret header OR "secret" field in JSON body ---
    secret_candidate = x_webhook_secret or parsed.secret
    if not _auth_ok(secret_candidate):
        log.warning("webhook_rejected_auth", extra={"source_ip": source_ip})
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return WebhookResponse(status="unauthorized", reason="unauthorized")

    # --- Maintenance gate ---
    if not accepting_signals():
        log.info("webhook_maintenance_block", extra={"raw_action": parsed.raw_action})
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return WebhookResponse(status="maintenance", reason="server in maintenance")

    # --- Resolve generic buy/sell (Pine strategy.order.action) against position state ---
    if parsed.raw_action in GENERIC_ACTIONS:
        reject_reason = await _resolve_generic_action(parsed)
        if reject_reason is not None:
            sig_id = await _persist_signal(
                parsed, body, source_ip, status_="rejected", qty=None,
                reject_reason=reject_reason,
            )
            log.warning("generic_action_unresolvable", extra={"signal_id": sig_id, "reason": reject_reason})
            await _push_signal(sig_id, parsed, "rejected", reason=reject_reason)
            return WebhookResponse(status="rejected", signal_id=sig_id, reason=reject_reason)
        log.info(
            "generic_action_resolved",
            extra={"symbol": parsed.symbol, "resolved_to": parsed.raw_action, "interval": parsed.interval},
        )

    # --- Resolve qty for open signals ---
    resolved_qty: Optional[int] = None
    if parsed.position_action == "open":
        resolved_qty = settings().resolve_qty(parsed.symbol, parsed.interval, parsed.qty)

    # --- Kernel / informational: persist and return without routing ---
    if parsed.raw_action in ("kernel_bullish", "kernel_bearish"):
        sig_id = await _persist_signal(parsed, body, source_ip, status_="informational", qty=None)
        log.info("signal_informational", extra={"signal_id": sig_id, "raw_action": parsed.raw_action})
        await _push_signal(sig_id, parsed, "informational")
        return WebhookResponse(status="informational", signal_id=sig_id)

    # --- Dedup window check ---
    dup_id = await _find_recent_duplicate(parsed)
    if dup_id is not None:
        sig_id = await _persist_signal(
            parsed, body, source_ip, status_="deduped", qty=resolved_qty, dedup_of=dup_id
        )
        log.info("signal_deduped", extra={"signal_id": sig_id, "dedup_of": dup_id})
        await _push_signal(sig_id, parsed, "deduped", reason=f"duplicate of signal {dup_id}")
        return WebhookResponse(status="deduplicated", signal_id=sig_id, reason=f"duplicate of signal {dup_id}")

    # --- Short-signal toggle ---
    if (
        parsed.raw_action in ("open_short", "short")
        and settings().ignore_short_signals
    ):
        sig_id = await _persist_signal(
            parsed, body, source_ip, status_="rejected", qty=resolved_qty,
            reject_reason="short signals ignored by server config",
        )
        await _push_signal(sig_id, parsed, "rejected", reason="short signals ignored by server config")
        return WebhookResponse(
            status="rejected", signal_id=sig_id, reason="short signals ignored by server config"
        )

    # --- Accept: persist and enqueue ---
    sig_id = await _persist_signal(parsed, body, source_ip, status_="accepted", qty=resolved_qty)
    log.info(
        "signal_accepted",
        extra={
            "signal_id": sig_id,
            "symbol": parsed.symbol,
            "raw_action": parsed.raw_action,
            "interval": parsed.interval,
            "resolved_qty": resolved_qty,
        },
    )
    await _push_signal(sig_id, parsed, "accepted")

    if _signal_queue is not None:
        try:
            _signal_queue.put_nowait({"signal_id": sig_id, "parsed": parsed, "resolved_qty": resolved_qty})
        except asyncio.QueueFull:
            log.error("signal_queue_full", extra={"signal_id": sig_id})

    return WebhookResponse(status="accepted", signal_id=sig_id)


async def _persist_signal(
    parsed: ParsedSignal,
    body: bytes,
    source_ip: Optional[str],
    status_: str,
    qty: Optional[int],
    reject_reason: Optional[str] = None,
    dedup_of: Optional[int] = None,
) -> int:
    async with get_session() as session:
        s = Signal(
            received_at=datetime.now(timezone.utc),
            raw_action=parsed.raw_action,
            order_side=parsed.order_side,
            position_action=parsed.position_action,
            direction=parsed.direction,
            symbol=parsed.symbol,
            close_price=parsed.close_price,
            interval=parsed.interval,
            strategy=parsed.strategy,
            qty=qty,
            status=status_,
            reject_reason=reject_reason,
            dedup_of=dedup_of,
            parse_format=parsed.parse_format,
            raw_body=body.decode("utf-8", errors="replace")[:64_000],
            source_ip=source_ip,
        )
        session.add(s)
        await session.commit()
        await session.refresh(s)
        return s.id


async def _store_unsupported(body: bytes, source_ip: Optional[str], reason: str) -> int:
    """Persist an unsupported (e.g. combined Open/Close Position) alert for audit."""
    async with get_session() as session:
        s = Signal(
            received_at=datetime.now(timezone.utc),
            raw_action="unsupported",
            order_side=None,
            position_action=None,
            direction=None,
            symbol="?",
            close_price=None,
            interval=None,
            strategy="ldc",
            qty=None,
            status="unsupported",
            reject_reason=reason,
            dedup_of=None,
            parse_format="plaintext",
            raw_body=body.decode("utf-8", errors="replace")[:64_000],
            source_ip=source_ip,
        )
        session.add(s)
        await session.commit()
        await session.refresh(s)
        return s.id


async def inject_close_signal(symbol: str, direction: str, interval: Optional[str]) -> int:
    """Inject a manual close signal from the dashboard, bypassing webhook auth/dedup."""
    from .signal_parser import ParsedSignal, _ACTION_META
    raw_action = "close_long" if direction == "long" else "close_short"
    side, pos_action, dir_ = _ACTION_META[raw_action]
    parsed = ParsedSignal(
        raw_action=raw_action, order_side=side, position_action=pos_action, direction=dir_,
        symbol=symbol, close_price=None, interval=interval,
        strategy="admin", qty=None, parse_format="json", secret=None,
    )
    sig_id = await _persist_signal(parsed, b"{}", source_ip="dashboard", status_="accepted", qty=None)
    await _push_signal(sig_id, parsed, "accepted")
    if _signal_queue is not None:
        try:
            _signal_queue.put_nowait({"signal_id": sig_id, "parsed": parsed, "resolved_qty": None})
        except asyncio.QueueFull:
            log.error("signal_queue_full", extra={"signal_id": sig_id})
    return sig_id


async def _find_recent_duplicate(parsed: ParsedSignal) -> Optional[int]:
    """Dedup key per PRD: (symbol, raw_action, strategy, interval)."""
    window = settings().dedup_window_seconds
    if window <= 0:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window)
    async with get_session() as session:
        # Only consider accepted signals (not already-deduped ones) as the "primary" match.
        # Allows a rejected duplicate to still be deduplicated against a prior accepted one.
        stmt = (
            select(Signal.id)
            .where(Signal.symbol == parsed.symbol)
            .where(Signal.raw_action == parsed.raw_action)
            .where(Signal.strategy == parsed.strategy)
            .where(Signal.interval == parsed.interval)
            .where(Signal.received_at >= cutoff)
            .where(Signal.status.in_(("accepted", "informational")))
            .order_by(Signal.received_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        row = result.first()
        return row[0] if row else None
