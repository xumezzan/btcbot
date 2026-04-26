#!/usr/bin/env python3
"""
Create or derive Polymarket CLOB API credentials from POLY_PRIVATE_KEY.

This script reads .env, signs a local authenticated request through the
py-clob-client SDK, and prints only the API credentials that should be copied
back into .env. It never prints the private key.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv


HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _optional_int(name: str) -> int | None:
    value = _env(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got: {value!r}")


def main() -> int:
    env_path = Path(".env")
    if not env_path.exists():
        print("Missing .env. Create it first: cp config/secrets.env.example .env", file=sys.stderr)
        return 1

    load_dotenv(env_path)

    private_key = _env("POLY_PRIVATE_KEY")
    if not private_key or private_key in {"0x...", "..."}:
        print("POLY_PRIVATE_KEY is still empty/placeholder in .env", file=sys.stderr)
        return 1
    if not PRIVATE_KEY_RE.match(private_key):
        print("POLY_PRIVATE_KEY must be 0x followed by 64 hex characters.", file=sys.stderr)
        return 1

    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print(
            "py-clob-client is not installed in this Python environment.\n"
            "Run one of:\n"
            "  .venv/bin/python scripts/create_polymarket_creds.py\n"
            "  pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    signature_type = _optional_int("POLY_SIGNATURE_TYPE")
    funder = _env("POLY_FUNDER") or None

    kwargs = {
        "host": HOST,
        "chain_id": CHAIN_ID,
        "key": private_key,
    }
    if signature_type is not None:
        kwargs["signature_type"] = signature_type
    if funder:
        kwargs["funder"] = funder

    client = ClobClient(**kwargs)
    creds = client.create_or_derive_api_creds()
    if creds is None:
        print("Polymarket did not return API credentials.", file=sys.stderr)
        return 1

    print("\nCopy these into .env:\n")
    print(f"POLY_API_KEY={creds.api_key}")
    print(f"POLY_API_SECRET={creds.api_secret}")
    print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
    print("\nKeep I_ACCEPT_REAL_MONEY_RISK=no until all live gates pass.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
