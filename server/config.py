"""Environment configuration loader with validation and normalization helpers."""
from __future__ import annotations

import json
import logging
import os
import re
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
    "S": "1s",
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
    # Deployment identity / mode separation
    trading_mode: str = "paper"
    live_trading_enabled: bool = False
    expected_ibkr_account: Optional[str] = None
    allowed_symbols: list[str] = field(default_factory=list)

    # Webhook
    webhook_secret: str = ""
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
    # When True, the bridge skips placing its own IBKR TRAIL orders on entry fills.
    # Use this when TradingView's Pine strategy.exit trailing stops already drive
    # position closes via webhook alerts (avoids double-exits).
    disable_trail: bool = False

    # Risk
    max_position_size: int = 1000
    max_open_positions: int = 10
    max_daily_realized_loss: float = 0.0

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

    # Maintenance window — primary nightly
    maintenance_window_enabled: bool = True
    maintenance_window_start: str = "23:45"
    maintenance_window_end: str = "00:15"
    maintenance_close_minutes_before: int = 5
    maintenance_timezone: str = "America/New_York"

    # Maintenance window 2 — secondary daily (e.g. 17:00–18:00)
    maintenance_window_2_enabled: bool = False
    maintenance_window_2_start: str = "17:00"
    maintenance_window_2_end: str = "18:00"

    # Maintenance weekend window (e.g. Fri 16:00 – Sun 17:00)
    maintenance_weekend_enabled: bool = False
    maintenance_weekend_start_day: str = "friday"
    maintenance_weekend_start_time: str = "16:00"
    maintenance_weekend_end_day: str = "sunday"
    maintenance_weekend_end_time: str = "17:00"

    # Contract mapping: per-symbol overrides for IBKR contract resolution.
    # Keys are base symbols (TV continuous-future suffixes like "1!"/"2!" are stripped
    # before lookup). Values: {"sec_type": "cont_future"|"future"|"stock",
    # "exchange": str, "currency": str, "last_trade_date": str|None}.
    # When a symbol has no entry, the bridge defaults to Stock(SMART, USD).
    contract_map: dict = field(default_factory=dict)

    def resolve_contract_spec(self, symbol: str) -> dict:
        """Return the contract spec for a symbol, stripping the TV "N!" suffix for
        lookup. Always returns a dict with {"symbol", "sec_type", "exchange", "currency",
        "last_trade_date"}; sec_type defaults to "stock" on SMART/USD when unmapped."""
        raw = (symbol or "").upper()
        base = re.sub(r"\d+!$", "", raw)
        spec = self.contract_map.get(base) or self.contract_map.get(raw)
        if spec:
            return {
                "symbol": base,
                "sec_type": str(spec.get("sec_type", "stock")).lower(),
                "exchange": spec.get("exchange", "SMART"),
                "currency": spec.get("currency", "USD"),
                "last_trade_date": spec.get("last_trade_date"),
            }
        return {
            "symbol": raw,
            "sec_type": "stock",
            "exchange": "SMART",
            "currency": "USD",
            "last_trade_date": None,
        }

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

    def validate_runtime_guardrails(self, *, ibkr_mock: bool = False) -> None:
        """Fail closed on unsafe live/paper configuration.

        Live and paper deployments should differ by env only. These checks make
        live trading explicitly opt-in and prevent common cross-wiring mistakes.
        """
        if self.trading_mode not in ("paper", "live"):
            raise RuntimeError(
                f"Invalid TRADING_MODE value: {self.trading_mode}. Must be paper or live."
            )
        if self.default_qty <= 0:
            raise RuntimeError("DEFAULT_QTY must be greater than zero.")
        if self.max_position_size <= 0:
            raise RuntimeError("MAX_POSITION_SIZE must be greater than zero.")
        if self.max_open_positions <= 0:
            raise RuntimeError("MAX_OPEN_POSITIONS must be greater than zero.")
        if self.default_qty > self.max_position_size:
            raise RuntimeError("DEFAULT_QTY cannot exceed MAX_POSITION_SIZE.")

        if self.trading_mode == "live":
            if ibkr_mock:
                raise RuntimeError("IBKR_MOCK cannot be enabled when TRADING_MODE=live.")
            if not self.live_trading_enabled:
                raise RuntimeError(
                    "TRADING_MODE=live requires LIVE_TRADING_ENABLED=true."
                )
            if not self.expected_ibkr_account:
                raise RuntimeError(
                    "TRADING_MODE=live requires EXPECTED_IBKR_ACCOUNT to prevent account mixups."
                )
            if self.webhook_secret in ("change_me_min_32_chars_recommended", "changeme"):
                raise RuntimeError("WEBHOOK_SECRET must be changed before live trading.")
            if len(self.webhook_secret) < 32:
                raise RuntimeError("WEBHOOK_SECRET must be at least 32 characters in live mode.")
            if self.dashboard_auth == "none":
                raise RuntimeError("DASHBOARD_AUTH=none is not allowed in live mode.")

            if self.tws_port not in (4001, 7496):
                log.warning(
                    "live_mode_nonstandard_tws_port",
                    extra={"tws_port": self.tws_port, "expected_ports": [4001, 7496]},
                )
        else:
            if self.tws_port in (4001, 7496):
                log.warning(
                    "paper_mode_live_tws_port",
                    extra={"tws_port": self.tws_port, "expected_ports": [4002, 7497]},
                )


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

    contract_map: dict = {}
    raw_map = os.getenv("CONTRACT_MAP", "").strip()
    if raw_map:
        try:
            parsed_map = json.loads(raw_map)
            if isinstance(parsed_map, dict):
                for k, v in parsed_map.items():
                    if isinstance(v, dict):
                        contract_map[str(k).upper()] = v
                    else:
                        log.warning("contract_map_entry_not_object", extra={"key": k})
            else:
                log.warning("contract_map_not_object", extra={"type": type(parsed_map).__name__})
        except json.JSONDecodeError as e:
            log.warning("contract_map_invalid_json", extra={"error": str(e)})

    trading_mode = os.getenv("TRADING_MODE", "paper").strip().lower()

    expected_account = os.getenv("EXPECTED_IBKR_ACCOUNT", "").strip() or None
    allowed_symbols = [
        s.strip().upper()
        for s in os.getenv("ALLOWED_SYMBOLS", "").split(",")
        if s.strip()
    ]

    return Settings(
        trading_mode=trading_mode,
        live_trading_enabled=_env_bool("LIVE_TRADING_ENABLED", False),
        expected_ibkr_account=expected_account,
        allowed_symbols=allowed_symbols,
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
        disable_trail=_env_bool("DISABLE_TRAIL", False),
        max_position_size=_env_int("MAX_POSITION_SIZE", 1000),
        max_open_positions=_env_int("MAX_OPEN_POSITIONS", 10),
        max_daily_realized_loss=_env_float("MAX_DAILY_REALIZED_LOSS", 0.0),
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
        maintenance_window_2_enabled=_env_bool("MAINTENANCE_WINDOW_2_ENABLED", False),
        maintenance_window_2_start=os.getenv("MAINTENANCE_WINDOW_2_START", "17:00"),
        maintenance_window_2_end=os.getenv("MAINTENANCE_WINDOW_2_END", "18:00"),
        maintenance_weekend_enabled=_env_bool("MAINTENANCE_WEEKEND_ENABLED", False),
        maintenance_weekend_start_day=os.getenv("MAINTENANCE_WEEKEND_START_DAY", "friday"),
        maintenance_weekend_start_time=os.getenv("MAINTENANCE_WEEKEND_START_TIME", "16:00"),
        maintenance_weekend_end_day=os.getenv("MAINTENANCE_WEEKEND_END_DAY", "sunday"),
        maintenance_weekend_end_time=os.getenv("MAINTENANCE_WEEKEND_END_TIME", "17:00"),
        contract_map=contract_map,
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
