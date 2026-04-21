"""Signal parsing: FORMAT A (LDC plaintext) and FORMAT B (custom JSON).

Both formats are normalized to a ParsedSignal dataclass.
Auto-detection is performed in parse_signal() based on content-type and body shape.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict
from typing import Optional

from .config import normalize_interval

_TV_SUFFIX_RE = re.compile(r"\d+!$")


def _normalize_symbol(raw: str) -> str:
    """Strip TradingView continuous-contract suffix (e.g. 'MBT1!' -> 'MBT')."""
    return _TV_SUFFIX_RE.sub("", raw.strip().upper())

log = logging.getLogger(__name__)


# Maps raw_action -> (order_side, position_action, direction).
# For kernel signals all three are None.
# "buy"/"sell" are generic strategy orders (Pine strategy.entry/strategy.exit);
# they carry no direction info and are resolved against current DB position state
# in webhook.py before routing.
_ACTION_META = {
    "open_long":      ("BUY",  "open",  "long"),
    "close_long":     ("SELL", "close", "long"),
    "open_short":     ("SELL", "open",  "short"),
    "close_short":    ("BUY",  "close", "short"),
    "kernel_bullish": (None,   None,    None),
    "kernel_bearish": (None,   None,    None),
    "buy":            ("BUY",  None,    None),
    "sell":           ("SELL", None,    None),
}

VALID_ACTIONS = set(_ACTION_META.keys())
GENERIC_ACTIONS = {"buy", "sell"}


class SignalParseError(Exception):
    """Raised when a signal body cannot be parsed into a valid ParsedSignal."""


class UnsupportedSignalError(Exception):
    """Raised when the signal is recognized but intentionally not supported
    (e.g., combined Open/Close Position alerts)."""


@dataclass
class ParsedSignal:
    raw_action: str
    order_side: Optional[str]
    position_action: Optional[str]
    direction: Optional[str]
    symbol: str
    close_price: Optional[float]
    interval: Optional[str]
    strategy: str
    qty: Optional[int]
    parse_format: str  # "json" or "plaintext"
    secret: Optional[str] = None  # if provided in JSON body; webhook handler checks this
    signal_time: Optional[str] = None  # optional ISO8601 from payload

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("secret", None)
        return d


def parse_signal(body: bytes, content_type: Optional[str] = None) -> ParsedSignal:
    """Parse a webhook body into a ParsedSignal.

    Auto-detection logic (PRD):
      1. Try JSON. If result has a valid "action", treat as FORMAT B.
      2. Else treat as FORMAT A plaintext.
      3. Else raise SignalParseError.
    """
    if body is None:
        raise SignalParseError("empty body")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SignalParseError(f"non-utf8 body: {e}") from e
    text = text.strip()
    if not text:
        raise SignalParseError("empty body")

    # Step 1: try JSON.
    parsed = _try_parse_json(text)
    if parsed is not None:
        return parsed
    # Step 2: plain text.
    return _parse_plaintext(text)


def _try_parse_json(text: str) -> Optional[ParsedSignal]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    action = payload.get("action")
    if not isinstance(action, str):
        return None
    action = action.strip().lower()
    if action not in VALID_ACTIONS:
        # Not a recognized action — let plaintext path handle it, but if the body
        # was clearly JSON the plaintext parser will reject too.
        raise SignalParseError(f"invalid action: {action!r}")
    # Accept "symbol" (preferred) or "instrument" (Pine strategy alert alias).
    symbol = payload.get("symbol") or payload.get("instrument")
    if not isinstance(symbol, str) or not symbol.strip():
        raise SignalParseError("missing or empty symbol (or instrument)")
    close_price = _coerce_float(payload.get("close"))
    interval_raw = payload.get("interval")
    interval = normalize_interval(interval_raw if isinstance(interval_raw, str) else None)
    strategy = payload.get("strategy") or "ldc"
    if not isinstance(strategy, str):
        strategy = "ldc"
    qty = payload.get("qty")
    qty_int: Optional[int] = None
    if qty is not None:
        try:
            qty_int = int(qty)
        except (TypeError, ValueError):
            qty_int = None
    secret = payload.get("secret")
    if not isinstance(secret, str):
        secret = None

    side, pos_action, direction = _ACTION_META[action]
    return ParsedSignal(
        raw_action=action,
        order_side=side,
        position_action=pos_action,
        direction=direction,
        symbol=_normalize_symbol(symbol),
        close_price=close_price,
        interval=interval,
        strategy=strategy,
        qty=qty_int,
        parse_format="json",
        secret=secret,
        signal_time=payload.get("signal_time") if isinstance(payload.get("signal_time"), str) else None,
    )


# Matches 'LDC Open Long ▲ | AAPL@182.45 | (1D)' and similar.
# Tolerant of missing arrows, extra whitespace.
_SYMBOL_PRICE_RE = re.compile(r"([A-Za-z0-9._:\-]+)\s*@\s*([0-9]+(?:\.[0-9]+)?)")


def _parse_plaintext(text: str) -> ParsedSignal:
    """Parse the LDC plain-text alert format.

    Examples:
      'LDC Open Long ▲ | AAPL@182.45 | (1D)'
      'LDC Close Short ▼ | NVDA@890.50 | (5)'
      'LDC Kernel Bullish ▲ | SPY@542.31 | (15)'

    Ambiguous combined alerts ("Open Position", "Close Position") raise
    UnsupportedSignalError which the webhook handler maps to HTTP 200 status="unsupported".
    """
    lower = text.lower()

    # Reject combined alerts explicitly (PRD: "Open Position" / "Close Position" -> unsupported).
    if "open position" in lower or "close position" in lower:
        raise UnsupportedSignalError(
            "combined Open/Close Position alerts are ambiguous — use directional alerts only"
        )

    # Match raw_action. Order matters: match "close long" before "open long" can't overlap
    # because they are distinct substrings, but kernel variants include "bullish" / "bearish".
    raw_action: Optional[str] = None
    if "open long" in lower:
        raw_action = "open_long"
    elif "close long" in lower:
        raw_action = "close_long"
    elif "open short" in lower:
        raw_action = "open_short"
    elif "close short" in lower:
        raw_action = "close_short"
    elif "kernel bullish" in lower:
        raw_action = "kernel_bullish"
    elif "kernel bearish" in lower:
        raw_action = "kernel_bearish"

    if raw_action is None:
        raise SignalParseError(f"could not detect signal type in body: {text!r}")

    # Symbol and price via regex anywhere in the body.
    sp = _SYMBOL_PRICE_RE.search(text)
    symbol: str
    close_price: Optional[float] = None
    if sp:
        symbol = _normalize_symbol(sp.group(1))
        try:
            close_price = float(sp.group(2))
        except ValueError:
            close_price = None
    else:
        # Fall back to pipe segmentation: "prefix | TICKER@PRICE | (interval)"
        segs = [s.strip() for s in text.split("|")]
        if len(segs) >= 2 and "@" in segs[1]:
            left, right = segs[1].split("@", 1)
            symbol = _normalize_symbol(left)
            try:
                close_price = float(right.strip())
            except ValueError:
                close_price = None
        else:
            raise SignalParseError(f"could not extract symbol@price from body: {text!r}")

    # Interval: pull anything inside the last parentheses, or the last pipe segment.
    interval_raw: Optional[str] = None
    paren = re.search(r"\(([^)]+)\)", text)
    if paren:
        interval_raw = paren.group(1).strip()
    else:
        segs = [s.strip() for s in text.split("|")]
        if len(segs) >= 3:
            interval_raw = segs[-1].strip("() ").strip()

    interval = normalize_interval(interval_raw) if interval_raw else None

    side, pos_action, direction = _ACTION_META[raw_action]
    return ParsedSignal(
        raw_action=raw_action,
        order_side=side,
        position_action=pos_action,
        direction=direction,
        symbol=symbol,
        close_price=close_price,
        interval=interval,
        strategy="ldc",
        qty=None,
        parse_format="plaintext",
        secret=None,
        signal_time=None,
    )


def _coerce_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
