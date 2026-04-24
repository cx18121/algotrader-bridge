"""Configuration guardrail checks for paper/live split.

Run: python tests/config_guardrails_test.py
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import reset_settings_for_tests, settings  # noqa: E402


@contextmanager
def env(overrides: dict[str, str]):
    old = os.environ.copy()
    os.environ.clear()
    os.environ.update(old)
    os.environ.update(overrides)
    try:
        reset_settings_for_tests()
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)
        reset_settings_for_tests()


def expect_runtime_error(fragment: str, *, ibkr_mock: bool = False) -> None:
    try:
        settings().validate_runtime_guardrails(ibkr_mock=ibkr_mock)
    except RuntimeError as e:
        assert fragment in str(e), str(e)
        return
    raise AssertionError(f"expected RuntimeError containing: {fragment}")


def run() -> None:
    base = {
        "WEBHOOK_SECRET": "paper-secret-at-least-thirty-two-chars",
        "DASHBOARD_AUTH": "none",
    }

    with env(base):
        cfg = settings()
        assert cfg.trading_mode == "paper"
        cfg.validate_runtime_guardrails(ibkr_mock=True)

    with env(base | {"TRADING_MODE": "live"}):
        expect_runtime_error("LIVE_TRADING_ENABLED=true")

    with env(base | {
        "TRADING_MODE": "live",
        "LIVE_TRADING_ENABLED": "true",
    }):
        expect_runtime_error("EXPECTED_IBKR_ACCOUNT")

    with env(base | {
        "TRADING_MODE": "live",
        "LIVE_TRADING_ENABLED": "true",
        "EXPECTED_IBKR_ACCOUNT": "U1234567",
    }):
        expect_runtime_error("DASHBOARD_AUTH=none")

    with env({
        "WEBHOOK_SECRET": "live-secret-at-least-thirty-two-chars",
        "DASHBOARD_AUTH": "basic_auth",
        "DASHBOARD_USERNAME": "admin",
        "DASHBOARD_PASSWORD": "password",
        "TRADING_MODE": "live",
        "LIVE_TRADING_ENABLED": "true",
        "EXPECTED_IBKR_ACCOUNT": "U1234567",
        "ALLOWED_SYMBOLS": "mes, mnq",
        "MAX_DAILY_REALIZED_LOSS": "250",
    }):
        cfg = settings()
        assert cfg.trading_mode == "live"
        assert cfg.allowed_symbols == ["MES", "MNQ"]
        assert cfg.max_daily_realized_loss == 250.0
        cfg.validate_runtime_guardrails(ibkr_mock=False)

    print("config_guardrails_test: ok")


if __name__ == "__main__":
    run()
