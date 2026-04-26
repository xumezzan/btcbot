#!/usr/bin/env python3
"""
Read-only live readiness checks.

This script validates local configuration and Polymarket CLOB authentication
without placing, cancelling, or redeeming anything.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv


PRIVATE_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _ok(name: str, detail: str = "") -> None:
    print(f"[OK] {name}{': ' + detail if detail else ''}")


def _fail(name: str, detail: str) -> None:
    print(f"[FAIL] {name}: {detail}")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def check_env() -> bool:
    load_dotenv(Path(".env"))
    passed = True

    private_key = _env("POLY_PRIVATE_KEY")
    if PRIVATE_KEY_RE.fullmatch(private_key):
        _ok("POLY_PRIVATE_KEY", "valid format")
    else:
        _fail("POLY_PRIVATE_KEY", "must be 0x followed by 64 hex characters")
        passed = False

    for key in ("POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE"):
        value = _env(key)
        if value and value != "...":
            _ok(key, "filled")
        else:
            _fail(key, "empty or placeholder")
            passed = False

    if _env("I_ACCEPT_REAL_MONEY_RISK").lower() == "yes":
        _ok("I_ACCEPT_REAL_MONEY_RISK", "yes")
    else:
        _ok("I_ACCEPT_REAL_MONEY_RISK", "not enabled")

    return passed


def check_config(path: str) -> bool:
    with open(path) as f:
        cfg = yaml.safe_load(f)

    passed = True
    strategy_mode = cfg.get("strategy", {}).get("mode")
    if strategy_mode == "signal":
        _ok("strategy.mode", "signal")
    else:
        _fail("strategy.mode", f"expected signal, got {strategy_mode!r}")
        passed = False

    signal = cfg.get("signal", {})
    risk = cfg.get("risk", {})
    checks = [
        ("signal.order_size_usdc", float(signal.get("order_size_usdc", 0)), 2.0),
        ("risk.max_order_size_usdc", float(risk.get("max_order_size_usdc", 0)), 2.0),
        ("risk.max_total_exposure_usdc", float(risk.get("max_total_exposure_usdc", 0)), 10.0),
        ("risk.max_daily_loss_usdc", float(risk.get("max_daily_loss_usdc", 0)), 7.0),
    ]
    for name, actual, expected in checks:
        if actual <= expected:
            _ok(name, str(actual))
        else:
            _fail(name, f"{actual} exceeds starter limit {expected}")
            passed = False

    if signal.get("allow_taker") is False:
        _ok("signal.allow_taker", "false")
    else:
        _fail("signal.allow_taker", "must stay false until live gates pass")
        passed = False

    return passed


def check_clob_auth() -> bool:
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError as exc:
        _fail("py-clob-client", str(exc))
        return False

    client = ClobClient(
        "https://clob.polymarket.com",
        key=_env("POLY_PRIVATE_KEY"),
        chain_id=137,
        creds=ApiCreds(
            api_key=_env("POLY_API_KEY"),
            api_secret=_env("POLY_API_SECRET"),
            api_passphrase=_env("POLY_API_PASSPHRASE"),
        ),
    )

    passed = True
    try:
        server_time = client.get_server_time()
        _ok("CLOB server time", "reachable" if server_time else "empty response")
    except Exception as exc:
        _fail("CLOB server time", f"{type(exc).__name__}: {str(exc)[:200]}")
        passed = False

    try:
        orders = client.get_orders()
        count = len(orders) if isinstance(orders, list) else "unknown"
        _ok("CLOB auth get_orders", f"{count} open/order records returned")
    except Exception as exc:
        _fail("CLOB auth get_orders", f"{type(exc).__name__}: {str(exc)[:200]}")
        passed = False

    try:
        trades = client.get_trades()
        count = len(trades) if isinstance(trades, list) else "unknown"
        _ok("CLOB auth get_trades", f"{count} user trades returned")
    except Exception as exc:
        _fail("CLOB auth get_trades", f"{type(exc).__name__}: {str(exc)[:200]}")
        passed = False

    try:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        balance_info = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        balance = float(balance_info.get("balance", 0)) if isinstance(balance_info, dict) else 0.0
        if balance > 0:
            _ok("CLOB collateral balance", str(balance))
        else:
            _ok("CLOB collateral balance", "0 (fund before live)")
    except Exception as exc:
        _fail("CLOB balance/allowance", f"{type(exc).__name__}: {str(exc)[:200]}")
        passed = False

    return passed


async def check_database() -> bool:
    try:
        import asyncpg
    except ImportError as exc:
        _fail("asyncpg", str(exc))
        return False

    database_url = _env("DATABASE_URL")
    if not database_url:
        _fail("DATABASE_URL", "empty")
        return False

    try:
        conn = await asyncpg.connect(database_url, timeout=3)
    except Exception as exc:
        _fail("Postgres connect", f"{type(exc).__name__}: {str(exc)[:200]}")
        return False

    try:
        rows = await conn.fetch(
            "select tablename from pg_tables where schemaname='public' order by tablename"
        )
    finally:
        await conn.close()

    tables = {row["tablename"] for row in rows}
    required = {
        "spot_ticks",
        "clob_snapshots",
        "clob_fills",
        "markets",
        "orders",
        "fills",
        "pnl_log",
        "fair_value_log",
    }
    missing = sorted(required - tables)
    if missing:
        _fail("Postgres schema", f"missing tables: {', '.join(missing)}")
        return False

    _ok("Postgres schema", f"{len(required)} required tables present")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/settings.yaml")
    args = parser.parse_args()

    all_passed = True
    all_passed = check_env() and all_passed
    all_passed = check_config(args.config) and all_passed
    all_passed = asyncio.run(check_database()) and all_passed
    all_passed = check_clob_auth() and all_passed

    if all_passed:
        print("\nReadiness checks passed. Live trading is still blocked until final gates pass.")
        return 0

    print("\nReadiness checks failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
