"""Environment configuration loader with validation and normalization helpers."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


log = logging.getLogger(__name__)


# TradingView raw {{interval}} string -> normalized label
_INTERVAL_MAP = {
    "1S": "1s",
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "45": "45m",
    "60": "1h",
}

# Already-normalized labels are accepted as-is (idempotent).
_NORMALIZED_INTERVALS = {"1s", "1m", "3m", "5m", "15m", "30m", "45m", "1h"}


def normalize_interval(raw: Optional[str]) -> Optional[str]:
    """Normalize a TradingView interval string to a human-readable label.

    Rules (per PRD Rule 1 and APPENDIX A):
      "1"  -> "1m"
      "3"  -> "3m"
      "5"  -> "5m"
      "15" -> "15m"
      "30" -> "30m"
      "45" -> "45m"
      "60" -> "1h"
      already normalized (e.g. "15m", "1h") -> returned as-is
      anything else -> returned as-is (warn at call site)
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if s in _NORMALIZED_INTERVALS:
        return s
    if s in _INTERVAL_MAP:
        return _INTERVAL_MAP[s]
    # Common TV forms like "1D", "1H" etc. — preserve raw.
    log.warning("interval_not_normalized", extra={"raw_interval": s})
    return s


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        log.warning("invalid_int_env", extra={"key": key, "value": v, "fallback": default})
        return default


def _env_float(key: str, default: float) -> float:
    v = os.getenv(key)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


@dataclass
class Settings:
    # Webhook
    webhook_secret: str
    webhook_rate_limit_rps: int = 10

    # TWS
    tws_host: str = "127.0.0.1"
    tws_port: int = 7497
    tws_client_id: int = 1
    tws_reconnect_interval_seconds: int = 10

    # Server
    server_host: str = "0.0.0.0"
    server_port: int = 8000

    # Storage
    db_path: str = "./trading.db"

    # Logging
    log_level: str = "INFO"

    # Sizing
    default_qty: int = 1

    # Trailing
    trail_offset_points: float = 50.0

    # Risk
    max_position_size: int = 1000
    max_open_positions: int = 10

    # Dedup
    dedup_window_seconds: int = 5

    # Behavior
    ignore_short_signals: bool = False
    partial_fill_replacement_mode: str = "add"

    # Dashboard auth
    dashboard_auth: str = "ip_allowlist"
    dashboard_allowed_ips: list = field(default_factory=list)
    dashboard_username: Optional[str] = None
    dashboard_password: Optional[str] = None

    # Account
    account_snapshot_interval_seconds: int = 300

    # Maintenance window
    maintenance_window_enabled: bool = True
    maintenance_window_start: str = "23:45"
    maintenance_window_end: str = "00:15"
    maintenance_close_minutes_before: int = 5
    maintenance_timezone: str = "America/New_York"

    # --- Per-symbol / per-interval overrides (discovered at resolve time) ---

    def resolve_qty(self, symbol: str, interval: Optional[str], signal_qty: Optional[int]) -> int:
        """Quantity resolution (PRD: Quantity resolution).

        Order of precedence:
          1. signal_qty (from JSON payload)
          2. SYMBOL_INTERVAL_QTY_{SYMBOL}_{INTERVAL}
          3. SYMBOL_QTY_{SYMBOL}
          4. DEFAULT_QTY
          5. fallback: 1
        """
        if signal_qty is not None and signal_qty > 0:
            return int(signal_qty)
        sym = (symbol or "").upper()
        if interval:
            key = f"SYMBOL_INTERVAL_QTY_{sym}_{interval}"
            v = os.getenv(key)
            if v:
                try:
                    return int(v)
                except ValueError:
                    log.warning("invalid_symbol_interval_qty", extra={"key": key, "value": v})
        sym_key = f"SYMBOL_QTY_{sym}"
        v = os.getenv(sym_key)
        if v:
            try:
                return int(v)
            except ValueError:
                log.warning("invalid_symbol_qty", extra={"key": sym_key, "value": v})
        if self.default_qty > 0:
            return self.default_qty
        return 1

    def resolve_trail_offset(self, symbol: str) -> float:
        """Trail offset resolution. Precedence:
          1. TRAIL_OFFSET_POINTS_{SYMBOL}
          2. TRAIL_OFFSET_POINTS (global default)
        """
        sym = (symbol or "").upper()
        key = f"TRAIL_OFFSET_POINTS_{sym}"
        v = os.getenv(key)
        if v:
            try:
                return float(v)
            except ValueError:
                log.warning("invalid_symbol_trail_offset", extra={"key": key, "value": v})
        return self.trail_offset_points


def load_settings() -> Settings:
    """Load all env vars, validate the required ones, return a Settings object."""
    secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "WEBHOOK_SECRET is required. Copy .env.example to .env and set a value."
        )

    dashboard_auth = os.getenv("DASHBOARD_AUTH", "ip_allowlist").strip().lower()
    if dashboard_auth not in ("ip_allowlist", "basic_auth", "none"):
        raise RuntimeError(
            f"Invalid DASHBOARD_AUTH value: {dashboard_auth}. "
            "Must be one of: ip_allowlist, basic_auth, none."
        )

    username = os.getenv("DASHBOARD_USERNAME")
    password = os.getenv("DASHBOARD_PASSWORD")
    if dashboard_auth == "basic_auth" and not (username and password):
        raise RuntimeError(
            "DASHBOARD_AUTH=basic_auth requires DASHBOARD_USERNAME and DASHBOARD_PASSWORD."
        )

    allowed_ips = [
        ip.strip()
        for ip in os.getenv("DASHBOARD_ALLOWED_IPS", "127.0.0.1").split(",")
        if ip.strip()
    ]

    partial_mode = os.getenv("PARTIAL_FILL_REPLACEMENT_MODE", "add").strip().lower()
    if partial_mode not in ("add", "replace"):
        log.warning(
            "invalid_partial_fill_replacement_mode",
            extra={"value": partial_mode, "fallback": "add"},
        )
        partial_mode = "add"

    return Settings(
        webhook_secret=secret,
        webhook_rate_limit_rps=_env_int("WEBHOOK_RATE_LIMIT_RPS", 10),
        tws_host=os.getenv("TWS_HOST", "127.0.0.1"),
        tws_port=_env_int("TWS_PORT", 7497),
        tws_client_id=_env_int("TWS_CLIENT_ID", 1),
        tws_reconnect_interval_seconds=_env_int("TWS_RECONNECT_INTERVAL_SECONDS", 10),
        server_host=os.getenv("SERVER_HOST", "0.0.0.0"),
        server_port=_env_int("SERVER_PORT", 8000),
        db_path=os.getenv("DB_PATH", "./trading.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        default_qty=_env_int("DEFAULT_QTY", 1),
        trail_offset_points=_env_float("TRAIL_OFFSET_POINTS", 50.0),
        max_position_size=_env_int("MAX_POSITION_SIZE", 1000),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", 10),
        dedup_window_seconds=_env_int("DEDUP_WINDOW_SECONDS", 5),
        ignore_short_signals=_env_bool("IGNORE_SHORT_SIGNALS", False),
        partial_fill_replacement_mode=partial_mode,
        dashboard_auth=dashboard_auth,
        dashboard_allowed_ips=allowed_ips,
        dashboard_username=username,
        dashboard_password=password,
        account_snapshot_interval_seconds=_env_int("ACCOUNT_SNAPSHOT_INTERVAL_SECONDS", 300),
        maintenance_window_enabled=_env_bool("MAINTENANCE_WINDOW_ENABLED", True),
        maintenance_window_start=os.getenv("MAINTENANCE_WINDOW_START", "23:45"),
        maintenance_window_end=os.getenv("MAINTENANCE_WINDOW_END", "00:15"),
        maintenance_close_minutes_before=_env_int("MAINTENANCE_CLOSE_MINUTES_BEFORE", 5),
        maintenance_timezone=os.getenv("MAINTENANCE_TIMEZONE", "America/New_York"),
    )


# Module-level singleton, lazily initialized to allow tests to set env first.
_settings: Optional[Settings] = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None
