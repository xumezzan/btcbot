"""
btcbot — Polymarket Bitcoin Up/Down Market-Making Bot

Usage:
    python main.py --mode collect      # Phase 0: collect data only
    python main.py --mode backtest     # Phase 4: replay historical data
    python main.py --mode dryrun       # Phase 5: live feed, no orders
    python main.py --mode live         # Phase 6+: real trading

All sensitive credentials must be in .env (see config/secrets.env.example).
Settings are in config/settings.yaml.
"""
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ──────────────────────────────────────────────────────────────
import colorlog

handler = colorlog.StreamHandler()
handler.setFormatter(colorlog.ColoredFormatter(
    "%(log_color)s%(asctime)s %(levelname)-8s%(reset)s %(message)s",
    datefmt="%H:%M:%S",
))
logging.basicConfig(handlers=[handler], level=logging.INFO)
log = logging.getLogger(__name__)


def load_config(path: str = "config/settings.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ── Collect mode ───────────────────────────────────────────────────────────────

async def run_collect(cfg: dict) -> None:
    """Phase 0: subscribe to feeds and write raw data to Postgres."""
    from src.db import create_pool, BufferedWriter
    from src.feeds.binance_ws import BinanceFeed, SpotTick
    from src.feeds.polymarket_clob import PolymarketCLOBFeed, CLOBTick, FillEvent
    from src.markets.discovery import MarketDiscovery

    pool = await create_pool()
    writer = BufferedWriter(pool, batch_size=cfg["database"]["batch_write_size"],
                            interval_ms=cfg["database"]["batch_write_interval_ms"])

    writer.register_table("spot_ticks",
        "INSERT INTO spot_ticks(symbol, ts_ms, received_ms, bid1, ask1, microprice, last_trade) "
        "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING")

    writer.register_table("clob_snapshots",
        "INSERT INTO clob_snapshots(market_id, token_id, ts_ms, received_ms, best_bid, best_ask, mid) "
        "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING")

    writer.register_table("clob_fills",
        "INSERT INTO clob_fills(market_id, token_id, ts_ms, received_ms, price, size, side, "
        "                       taker_order_id, maker_order_id) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING")

    writer.register_table("markets",
        "INSERT INTO markets(condition_id, question, symbol, start_price, up_token_id, down_token_id, "
        "                    start_time, end_time, duration_minutes, volume_24h, resolution_source, "
        "                    oracle_feed_id, active) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) "
        "ON CONFLICT(condition_id) DO UPDATE SET active=EXCLUDED.active, volume_24h=EXCLUDED.volume_24h")

    # Binance feed
    symbols = [a["binance_pair"] for a in cfg["assets"] if a["enabled"]]
    binance = BinanceFeed(symbols, cfg["binance"])

    async def on_spot_tick(tick: SpotTick) -> None:
        writer.enqueue("spot_ticks", (
            tick.symbol, tick.ts_ms, tick.received_ms,
            tick.bid1, tick.ask1, tick.microprice, tick.last_trade,
        ))

    binance.add_callback(on_spot_tick)

    # Polymarket CLOB feed
    clob_feed = PolymarketCLOBFeed(cfg["polymarket"])

    async def on_clob_tick(tick: CLOBTick) -> None:
        writer.enqueue("clob_snapshots", (
            tick.market_id, tick.token_id, tick.ts_ms, tick.received_ms,
            tick.best_bid, tick.best_ask, tick.mid,
        ))

    async def on_clob_fill(fill: FillEvent) -> None:
        writer.enqueue("clob_fills", (
            fill.market_id, fill.token_id, fill.ts_ms, fill.received_ms,
            fill.price, fill.size, fill.side,
            fill.taker_order_id, fill.maker_order_id,
        ))

    clob_feed.add_tick_callback(on_clob_tick)
    clob_feed.add_fill_callback(on_clob_fill)

    # Market discovery
    discovery = MarketDiscovery(
        cfg["polymarket"],
        allowed_durations=cfg["market_durations"],
        min_volume=cfg["polymarket"]["min_market_volume_24h"],
    )

    async def on_new_market(m) -> None:
        writer.enqueue("markets", (
            m.condition_id, m.question, m.symbol, m.start_price,
            m.up_token_id, m.down_token_id,
            m.start_time, m.end_time, m.duration_minutes,
            m.volume_24h, m.resolution_source, m.oracle_feed_id, m.active,
        ))
        clob_feed.subscribe_market(m.condition_id, m.up_token_id, m.down_token_id)
        await clob_feed.resubscribe()
        log.info("Subscribed to market: %s %s", m.symbol, m.question[:50])

    discovery.on_new_market(on_new_market)

    log.info("Starting data collection mode")
    await asyncio.gather(
        binance.run(),
        clob_feed.run(),
        discovery.run(),
        writer.run_flush_loop(),
    )


# ── Dry-run / live mode ────────────────────────────────────────────────────────

async def run_trading(cfg: dict, dry_run: bool) -> None:
    """Phase 5 (dry-run) and Phase 6+ (live)."""
    from src.db import create_pool, BufferedWriter
    from src.feeds.binance_ws import BinanceFeed, SpotTick
    from src.feeds.polymarket_clob import PolymarketCLOBFeed, CLOBTick, FillEvent
    from src.markets.discovery import MarketDiscovery, MarketInfo
    from src.markets.lifecycle import classify, seconds_to_expiry, MarketStage
    from src.fair_value.model import FairValueModel
    from src.fair_value.volatility import RealizedVolatility
    from src.quoting.quote_engine import QuoteEngine
    from src.quoting.inventory import InventoryManager
    from src.risk.risk_engine import RiskEngine
    from src.risk.adverse_selection import AdverseSelectionTracker
    from src.execution.clob_client import CLOBClient
    from src.signals.value_signal import MarketPrices, OutcomeQuote, ValueSignalEngine
    from src.dashboard import Dashboard

    mode = "dryrun" if dry_run else "live"
    strategy_mode = cfg.get("strategy", {}).get("mode", "market_making")
    log.info("Starting %s mode (%s strategy)", mode.upper(), strategy_mode)

    pool = await create_pool()
    clob = CLOBClient(cfg["execution"], dry_run=dry_run)

    # Per-symbol models
    vol_calcs: dict[str, RealizedVolatility] = {}
    models: dict[str, FairValueModel] = {}
    for asset in cfg["assets"]:
        if asset["enabled"]:
            sym = asset["symbol"]
            vol_calcs[sym] = RealizedVolatility(
                window_minutes=cfg["fair_value"]["vol_window_minutes"],
                min_periods=cfg["fair_value"]["vol_min_periods"],
            )
            models[sym] = FairValueModel(cfg["fair_value"])

    quote_engine = QuoteEngine(cfg["quoting"])
    signal_engine = ValueSignalEngine(cfg.get("signal", {}))
    inventory = InventoryManager()
    risk = RiskEngine(cfg["risk"])
    adverse = AdverseSelectionTracker()
    dashboard = Dashboard()
    dashboard.update(mode=mode)

    active_markets: dict[str, MarketInfo] = {}
    current_spot: dict[str, float] = {}
    market_prices: dict[str, dict[str, OutcomeQuote]] = {}
    allow_taker = bool(cfg.get("signal", {}).get("allow_taker", False))
    warned_taker_disabled: set[str] = set()

    # Per-market active order IDs: {market_id: {side: order_id}}
    active_orders: dict[str, dict] = {}

    async def record_order(order, token_side: str | None) -> None:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO orders(order_id, market_id, token_id, side, token_side, price, size, status, dry_run, ts_ms, updated_ms) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
                    "ON CONFLICT(order_id) DO UPDATE SET status=EXCLUDED.status, updated_ms=EXCLUDED.updated_ms",
                    order.order_id,
                    order.market_id,
                    order.token_id,
                    order.side,
                    token_side,
                    order.price,
                    order.size,
                    order.status,
                    dry_run,
                    order.ts_ms,
                    int(time.time() * 1000),
                )
        except Exception:
            log.exception("Failed to record order %s", getattr(order, "order_id", ""))

    async def record_user_fill(fill, token_side: str, is_adverse: bool | None) -> None:
        fill_id = getattr(fill, "fill_id", "") or f"{fill.order_id}:{fill.ts_ms}:{fill.token_id}:{fill.size}"
        order_id = getattr(fill, "order_id", "") or None
        try:
            async with pool.acquire() as conn:
                db_order_id = None
                if order_id:
                    exists = await conn.fetchval("SELECT 1 FROM orders WHERE order_id=$1", order_id)
                    db_order_id = order_id if exists else None
                await conn.execute(
                    "INSERT INTO fills(fill_id, order_id, market_id, token_id, token_side, side, price, size, fee, ts_ms, is_adverse) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
                    "ON CONFLICT(fill_id) DO NOTHING",
                    fill_id,
                    db_order_id,
                    fill.market_id,
                    fill.token_id,
                    token_side,
                    fill.side,
                    fill.price,
                    fill.size,
                    fill.fee,
                    fill.ts_ms,
                    is_adverse,
                )
                if db_order_id:
                    await conn.execute(
                        "UPDATE orders SET status='FILLED', updated_ms=$1 WHERE order_id=$2",
                        int(time.time() * 1000),
                        db_order_id,
                    )
        except Exception:
            log.exception("Failed to record fill %s", fill_id)

    async def on_spot_tick(tick: SpotTick) -> None:
        if not risk.is_alive():
            return
        sym = tick.symbol.replace("USDT", "")
        current_spot[sym] = tick.microprice
        vc = vol_calcs.get(sym)
        if vc:
            vc.update(tick.microprice, tick.ts_ms)
        m = models.get(sym)
        if m:
            m.update_price_history(tick.microprice, tick.ts_ms / 1000)
        dashboard.update(spot={**dashboard._state["spot"], sym: tick.microprice})

        # Check stale orders for all active markets with this symbol
        for mkt in list(active_markets.values()):
            if mkt.symbol != sym:
                continue
            vol = vc.get() if vc else 0.0
            if quote_engine.is_stale(mkt.condition_id, tick.microprice, vol):
                cancelled = await clob.cancel_all(mkt.condition_id)
                if cancelled:
                    log.debug("Preemptive cancel: %d orders for %s", cancelled, mkt.condition_id[:12])
                active_orders.pop(mkt.condition_id, None)

        # Refresh quotes for active markets
        await _refresh_quotes(sym)

    async def _refresh_quotes(sym: str) -> None:
        if not risk.is_alive():
            return
        spot = current_spot.get(sym, 0.0)
        if spot <= 0:
            return
        vc = vol_calcs.get(sym)
        model = models.get(sym)
        if not vc or not model or not vc.has_enough_data():
            return
        vol = vc.get()

        for mkt in list(active_markets.values()):
            if mkt.symbol != sym:
                continue
            stage = classify(mkt)
            tte = seconds_to_expiry(mkt)

            if stage in (MarketStage.EXPIRED, MarketStage.DANGER):
                await clob.cancel_all(mkt.condition_id)
                active_orders.pop(mkt.condition_id, None)
                continue

            fair = model.predict(spot, mkt.start_price, vol, tte)
            inv_skew = inventory.inventory_skew(mkt.condition_id, sym)

            # Exposure check
            mkt_pos = inventory.get_or_create(mkt.condition_id, sym)
            if not risk.check_market_exposure(mkt_pos.gross_exposure_usdc):
                continue
            if not risk.check_total_exposure(inventory.total_exposure()):
                continue

            quotes = quote_engine.compute(
                market_id=mkt.condition_id,
                fair_up=fair.fair_up,
                vol_annual=vol,
                time_to_expiry_s=tte,
                stage=stage,
                inventory_skew=inv_skew,
                spot=spot,
                avg_vol=vol,
            )

            if not quotes.should_quote:
                continue

            size = risk.clamp_order_size(cfg["risk"]["max_order_size_usdc"])

            if strategy_mode == "signal":
                prices_by_side = market_prices.get(mkt.condition_id, {})
                prices = MarketPrices(
                    up=prices_by_side.get("UP", OutcomeQuote()),
                    down=prices_by_side.get("DOWN", OutcomeQuote()),
                )
                decision = signal_engine.decide(
                    market_id=mkt.condition_id,
                    up_token_id=mkt.up_token_id,
                    down_token_id=mkt.down_token_id,
                    fair_up=fair.fair_up,
                    prices=prices,
                    time_to_expiry_s=tte,
                    default_size_usdc=size,
                )

                if decision.action == "SKIP":
                    log.debug("Signal skip %s: %s", mkt.condition_id[:12], decision.reason)
                    continue

                if not dry_run and not allow_taker:
                    if mkt.condition_id not in warned_taker_disabled:
                        log.warning(
                            "Signal found %s edge=%.4f price=%.4f, but live taker execution is disabled",
                            decision.action,
                            decision.edge,
                            decision.price,
                        )
                        warned_taker_disabled.add(mkt.condition_id)
                    continue

                existing = active_orders.get(mkt.condition_id, {})
                if existing:
                    await clob.cancel_all(mkt.condition_id)
                    active_orders.pop(mkt.condition_id, None)

                order = await clob.post_order(
                    decision.token_id,
                    mkt.condition_id,
                    "BUY",
                    decision.price,
                    decision.size_usdc,
                )
                if order:
                    active_orders[mkt.condition_id] = {"signal_buy": order.order_id}
                    signal_engine.mark_order_sent(mkt.condition_id)
                    await record_order(order, decision.side)
                    log.info(
                        "Signal %s %s price=%.4f fair=%.4f edge=%.4f size=$%.2f",
                        decision.action,
                        mkt.condition_id[:12],
                        decision.price,
                        decision.fair_probability,
                        decision.edge,
                        decision.size_usdc,
                    )
                continue

            # Cancel old orders and post new quotes
            existing = active_orders.get(mkt.condition_id, {})
            if existing:
                await clob.cancel_all(mkt.condition_id)
                active_orders.pop(mkt.condition_id, None)

            new_orders = {}
            # UP bid
            o = await clob.post_order(mkt.up_token_id, mkt.condition_id, "BUY", quotes.up_bid, size)
            if o:
                new_orders["up_bid"] = o.order_id
            # UP ask
            o = await clob.post_order(mkt.up_token_id, mkt.condition_id, "SELL", quotes.up_ask, size)
            if o:
                new_orders["up_ask"] = o.order_id
            # DOWN bid
            o = await clob.post_order(mkt.down_token_id, mkt.condition_id, "BUY", quotes.down_bid, size)
            if o:
                new_orders["down_bid"] = o.order_id
            # DOWN ask
            o = await clob.post_order(mkt.down_token_id, mkt.condition_id, "SELL", quotes.down_ask, size)
            if o:
                new_orders["down_ask"] = o.order_id

            if new_orders:
                active_orders[mkt.condition_id] = new_orders

    async def process_user_fill(fill) -> None:
        """Handle an authenticated user fill from the CLOB API."""
        if not risk.is_alive():
            return
        mkt = active_markets.get(fill.market_id)
        if not mkt:
            return

        # Determine which side was filled
        side = "UP" if fill.token_id == mkt.up_token_id else "DOWN"
        is_buy = fill.side.upper() == "BUY"

        fill_id = getattr(fill, "fill_id", "") or getattr(fill, "order_id", "")
        adverse.record_fill(
            fill_id=fill_id,
            market_id=fill.market_id,
            token_side=side,
            price=fill.price,
            size=fill.size,
            ts_ms=fill.ts_ms,
        )
        is_adverse = None
        inventory.record_fill(fill.market_id, mkt.symbol, side, is_buy, fill.size, fill.price)

        adv_rate = adverse.adverse_selection_rate()
        risk.check_adverse_selection(adv_rate)
        risk.check_net_delta(inventory.cross_asset_delta())
        await record_user_fill(fill, side, is_adverse)

        dashboard.update(
            fills_today=dashboard._state["fills_today"] + 1,
            adverse_rate=adv_rate,
        )
        log.info("Fill: %s %s token=%s price=%.4f size=%.2f", side, "BUY" if is_buy else "SELL",
                 fill.token_id[:12], fill.price, fill.size)

    async def poll_user_fills() -> None:
        """Poll authenticated user fills; public market trades are not our fills."""
        seen_fill_ids: set[str] = set()
        last_poll_ms = int(time.time() * 1000) - 60_000
        while True:
            if risk.is_alive():
                fills = await clob.get_user_fills(after_ts_ms=last_poll_ms)
                now_ms = int(time.time() * 1000)
                for fill in fills:
                    fill_key = fill.fill_id or f"{fill.order_id}:{fill.ts_ms}:{fill.token_id}:{fill.size}"
                    if fill_key in seen_fill_ids:
                        continue
                    seen_fill_ids.add(fill_key)
                    await process_user_fill(fill)
                last_poll_ms = now_ms - 5_000
            await asyncio.sleep(5)

    async def on_clob_tick(tick: CLOBTick) -> None:
        mkt = active_markets.get(tick.market_id)
        if not mkt:
            return

        side = "UP" if tick.token_id == mkt.up_token_id else "DOWN"
        ask_size = tick.asks[0].size if tick.asks else 0.0
        market_prices.setdefault(tick.market_id, {})[side] = OutcomeQuote(
            bid=tick.best_bid,
            ask=tick.best_ask,
            ask_size=ask_size,
        )

        if strategy_mode == "signal":
            await _refresh_quotes(mkt.symbol)

    async def on_new_market(mkt: MarketInfo) -> None:
        active_markets[mkt.condition_id] = mkt
        clob_feed.subscribe_market(mkt.condition_id, mkt.up_token_id, mkt.down_token_id)
        await clob_feed.resubscribe()

    async def on_expired_market(mkt: MarketInfo) -> None:
        await clob.cancel_all(mkt.condition_id)
        active_orders.pop(mkt.condition_id, None)
        active_markets.pop(mkt.condition_id, None)
        # Redeem settled position to recycle USDC
        asyncio.create_task(clob.redeem_position(mkt.condition_id))

    # Wire feeds
    symbols = [a["binance_pair"] for a in cfg["assets"] if a["enabled"]]
    binance = BinanceFeed(symbols, cfg["binance"])
    binance.add_callback(on_spot_tick)

    clob_feed = PolymarketCLOBFeed(cfg["polymarket"])
    clob_feed.add_tick_callback(on_clob_tick)

    discovery = MarketDiscovery(
        cfg["polymarket"],
        allowed_durations=cfg["market_durations"],
        min_volume=cfg["polymarket"]["min_market_volume_24h"],
    )
    discovery.on_new_market(on_new_market)
    discovery.on_expired_market(on_expired_market)

    await asyncio.gather(
        binance.run(),
        clob_feed.run(),
        discovery.run(),
        poll_user_fills(),
    )


# ── Backtest mode ─────────────────────────────────────────────────────────────

async def run_backtest(cfg: dict, start: str, end: str, symbol: str) -> None:
    from src.db import create_pool
    from src.backtest.simulator import Simulator
    from src.backtest.metrics import compute_metrics

    pool = await create_pool()
    sim = Simulator(pool, cfg)
    result = await sim.run(start, end, symbol=symbol)

    fills_dicts = [
        {"market_id": f.market_id, "pnl": f.pnl_at_resolution or 0.0,
         "is_adverse": f.is_adverse}
        for f in result.fills
    ]
    metrics = compute_metrics(
        pnl_series=result.pnl_series,
        fills=fills_dicts,
        duration_hours=result.duration_hours,
        total_fees=result.total_gas,
    )

    print("\n" + "="*60)
    print(f"BACKTEST RESULTS: {symbol} {start} → {end}")
    print("="*60)
    print(f"  Total PnL:        ${metrics.total_pnl:+.4f}")
    print(f"  Net (after fees): ${metrics.net_pnl_after_fees:+.4f}")
    print(f"  Total Fees/Gas:   ${metrics.total_fees:.4f}")
    print(f"  Fills:            {metrics.n_fills}")
    print(f"  Markets:          {metrics.n_markets}")
    print(f"  PnL/fill:         ${metrics.pnl_per_fill:+.6f}")
    print(f"  Win rate:         {metrics.win_rate:.1%}")
    print(f"  Sharpe:           {metrics.sharpe_ratio:.3f}")
    print(f"  Max drawdown:     ${metrics.max_drawdown:.4f}")
    print(f"  Adverse sel.:     {metrics.adverse_selection_rate:.1%}")
    print(f"  Fill rate:        {metrics.fill_rate_per_hour:.1f}/hr")
    print("="*60)

    # Gate checks
    print("\nGate checks:")
    print(f"  [{'✓' if metrics.net_pnl_after_fees > 0 else '✗'}] Positive net PnL after fees")
    print(f"  [{'✓' if metrics.adverse_selection_rate < 0.45 else '✗'}] Adverse selection < 45%")
    print(f"  [{'✓' if metrics.max_drawdown < 30 else '✗'}] Max drawdown < $30")
    print(f"  [{'✓' if metrics.n_fills >= 50 else '✗'}] At least 50 fills (enough data)")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.command()
@click.option("--mode", default=None,
              type=click.Choice(["collect", "dryrun", "live", "backtest"]),
              help="Override mode from settings.yaml")
@click.option("--config", default="config/settings.yaml", help="Config file path")
@click.option("--start", default="2025-01-01", help="Backtest start date (YYYY-MM-DD)")
@click.option("--end", default="2025-01-14", help="Backtest end date (YYYY-MM-DD)")
@click.option("--symbol", default="BTC", help="Asset symbol for backtest")
def main(mode, config, start, end, symbol):
    cfg = load_config(config)
    active_mode = mode or cfg.get("mode", "collect")

    log.info("btcbot starting in mode: %s", active_mode.upper())

    if active_mode == "collect":
        asyncio.run(run_collect(cfg))
    elif active_mode == "dryrun":
        asyncio.run(run_trading(cfg, dry_run=True))
    elif active_mode == "live":
        # Extra confirmation for live mode
        if not os.environ.get("POLY_PRIVATE_KEY"):
            log.critical("POLY_PRIVATE_KEY not set. Cannot start live trading.")
            sys.exit(1)
        if os.environ.get("I_ACCEPT_REAL_MONEY_RISK", "").strip().lower() != "yes":
            log.critical("I_ACCEPT_REAL_MONEY_RISK=yes is required for live trading.")
            sys.exit(1)
        confirm = input(
            "\n⚠  You are starting LIVE trading mode. "
            "Real money will be at risk.\nType 'yes' to continue: "
        )
        if confirm.strip().lower() != "yes":
            log.info("Aborted.")
            sys.exit(0)
        cfg["execution"]["dry_run"] = False
        asyncio.run(run_trading(cfg, dry_run=False))
    elif active_mode == "backtest":
        asyncio.run(run_backtest(cfg, start, end, symbol))
    else:
        log.error("Unknown mode: %s", active_mode)
        sys.exit(1)


if __name__ == "__main__":
    main()
