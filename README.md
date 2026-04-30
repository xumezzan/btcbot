# btcbot

`btcbot` is a research-first trading bot for Polymarket crypto Up/Down markets.
It collects Binance spot data and Polymarket CLOB data, estimates fair value for
short-duration UP/DOWN markets, and can run in collection, backtest, dry-run, or
live modes.

The project is not ready for unrestricted live trading. The current goal is to
prove whether a durable edge exists before risking meaningful capital.

## What It Does

- Discovers active Polymarket BTC/ETH Up/Down markets.
- Streams Binance spot prices and Polymarket CLOB order books.
- Stores raw market data, market metadata, orders, fills, fair-value logs, and
  PnL state in Postgres.
- Estimates `P(UP wins)` with a volatility and momentum based fair-value model.
- Supports two strategy styles:
  - `signal`: buy only when model fair probability beats executable ask.
  - `market_making`: quote bid/ask around fair value.
- Runs in safe non-live modes before real execution.
- Applies basic risk limits and kill-switch checks.

## Modes

```bash
python3 main.py --mode collect
python3 main.py --mode backtest --start 2026-04-01 --end 2026-04-28 --symbol BTC
python3 main.py --mode dryrun
python3 main.py --mode live
```

Modes:

- `collect`: subscribe to data feeds and write raw data to Postgres.
- `backtest`: replay stored data and evaluate strategy behavior.
- `dryrun`: run live feeds and strategy logic without placing real orders.
- `live`: place real Polymarket orders. This mode requires explicit gates.

## Setup

Use Python 3.10+; Python 3.12 is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/secrets.env.example .env
```

Create the database schema:

```bash
psql "$DATABASE_URL" < schema.sql
```

Run tests:

```bash
python3 -m pytest -q
```

Note: invoking `pytest` directly may use an older system Python. Prefer
`python3 -m pytest`.

## Live Readiness

Before live trading, run the read-only readiness checks:

```bash
python3 scripts/check_live_readiness.py
```

Passing this script is necessary but not sufficient. The live gates are defined
in [LIVE_GATES.md](LIVE_GATES.md).

## Roadmap

The roadmap is intentionally evidence-driven. The next milestone is not simply
"turn live on"; it is to prove that the strategy has edge versus the market's
own implied probability.

See [ROADMAP.md](ROADMAP.md).

