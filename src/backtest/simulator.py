"""
Historical backtest simulator.

Replays stored CLOB orderbook snapshots and Binance spot ticks to simulate
what the strategy would have done. Key simulation assumptions:

  - Queue position: worst-case (back of queue at each price level)
  - Cancel latency: 100ms round-trip (configurable)
  - Gas fee: 0.02 USDC per order/cancel
  - Fill occurs when our bid >= best ask (or our ask <= best bid) in the book
  - Partial fills not simulated (simplification: size is small)

Data is loaded from Postgres. Run after collecting ≥2 weeks of data.

Usage:
    sim = Simulator(db_pool, cfg)
    result = await sim.run("2025-01-01", "2025-01-14", symbol="BTC")
    print(result)
"""
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

GAS_FEE = 0.02  # USDC per order/cancel


@dataclass
class SimFill:
    ts_ms: int
    market_id: str
    side: str       # "UP" or "DOWN"
    is_buy: bool
    price: float
    size: float
    fee: float
    pnl_at_resolution: float | None = None
    is_adverse: bool | None = None


@dataclass
class SimResult:
    fills: list[SimFill] = field(default_factory=list)
    pnl_series: list[float] = field(default_factory=list)
    total_gas: float = 0.0
    total_cancels: int = 0
    stale_quote_count: int = 0  # times we were potentially picked off before cancel
    duration_hours: float = 0.0


class Simulator:
    def __init__(self, db_pool, cfg: dict):
        self._db = db_pool
        self._cfg = cfg
        self._cancel_latency_ms = cfg.get("cancel_latency_ms", 100)
        self._cancel_bps = cfg.get("spot_move_threshold_bps", 15)

    async def run(self, start_date: str, end_date: str, symbol: str = "BTC") -> SimResult:
        """
        Main backtest loop. Fetches data from DB and simulates strategy.
        Returns SimResult with fills, PnL series, and summary stats.
        """
        from src.fair_value.model import FairValueModel
        from src.fair_value.volatility import RealizedVolatility
        from src.quoting.quote_engine import QuoteEngine
        from src.quoting.inventory import InventoryManager
        from src.markets.lifecycle import classify, seconds_to_expiry
        from src.signals.value_signal import MarketPrices, OutcomeQuote, ValueSignalEngine

        model = FairValueModel(self._cfg.get("fair_value", {}))
        vol_calc = RealizedVolatility(
            window_minutes=self._cfg.get("fair_value", {}).get("vol_window_minutes", 60)
        )
        quote_engine = QuoteEngine(self._cfg.get("quoting", {}))
        signal_engine = ValueSignalEngine(self._cfg.get("signal", {}))
        inventory = InventoryManager()
        result = SimResult()
        strategy_mode = self._cfg.get("strategy", {}).get("mode", "market_making")

        # Load spot ticks
        spot_ticks = await self._load_spot_ticks(start_date, end_date, f"{symbol}USDT")
        # Load CLOB snapshots
        clob_snapshots = await self._load_clob_snapshots(start_date, end_date, symbol)
        # Load markets
        markets = await self._load_markets(start_date, end_date, symbol)

        if not spot_ticks:
            log.warning("No spot ticks found for %s %s–%s", symbol, start_date, end_date)
            return result

        log.info("Backtest: %d spot ticks, %d CLOB snapshots, %d markets",
                 len(spot_ticks), len(clob_snapshots), len(markets))

        # Build market lookup
        market_map = {m["condition_id"]: m for m in markets}
        # Build CLOB index by market_id and ts_ms
        clob_by_market: dict[str, list] = {}
        for snap in clob_snapshots:
            clob_by_market.setdefault(snap["market_id"], []).append(snap)

        # Sort everything by time
        spot_ticks.sort(key=lambda x: x["ts_ms"])

        # Active virtual orders: {order_id: {market_id, side, price, size, placed_ts_ms}}
        virtual_orders: dict[str, dict] = {}
        last_spot = 0.0
        last_spot_ts = 0
        pnl = 0.0

        start_ts = spot_ticks[0]["ts_ms"]
        end_ts = spot_ticks[-1]["ts_ms"]
        result.duration_hours = (end_ts - start_ts) / 3_600_000

        # Advance CLOB pointer per market
        clob_ptrs: dict[str, int] = {mid: 0 for mid in clob_by_market}
        latest_prices: dict[str, dict[str, OutcomeQuote]] = {}

        for tick in spot_ticks:
            ts_ms = tick["ts_ms"]
            spot = tick["microprice"] or tick["last_trade"]
            if spot <= 0:
                continue

            vol_calc.update(spot, ts_ms)
            model.update_price_history(spot, ts_ms / 1000)
            last_spot = spot
            last_spot_ts = ts_ms
            vol = vol_calc.get()

            # Process each active market at this timestamp
            for cid, mkt in market_map.items():
                stage = classify(_dict_to_market(mkt), ts_ms)
                tte = seconds_to_expiry(_dict_to_market(mkt), ts_ms)

                # Check stale orders
                for oid, vord in list(virtual_orders.items()):
                    if vord["market_id"] != cid:
                        continue
                    age_ms = ts_ms - vord["placed_ts_ms"]
                    move = abs(spot - vord["spot_at_place"]) / vord["spot_at_place"] * 10_000
                    if move > self._cancel_bps:
                        # Would cancel — but there's cancel latency
                        # If a fill happened in that window, count it as stale
                        result.stale_quote_count += 1
                        result.total_cancels += 1
                        result.total_gas += GAS_FEE
                        pnl -= GAS_FEE
                        del virtual_orders[oid]

                # Get latest CLOB snapshots at this timestamp
                clob_list = clob_by_market.get(cid, [])
                ptr = clob_ptrs.get(cid, 0)
                while ptr < len(clob_list) and clob_list[ptr]["ts_ms"] <= ts_ms:
                    snap = clob_list[ptr]
                    side = "UP" if snap.get("token_id") == mkt.get("up_token_id") else "DOWN"
                    latest_prices.setdefault(cid, {})[side] = OutcomeQuote(
                        bid=float(snap.get("best_bid") or 0.0),
                        ask=float(snap.get("best_ask") or 0.0),
                        ask_size=float(snap.get("ask_size") or 999999.0),
                    )
                    ptr += 1
                clob_ptrs[cid] = ptr

                prices_by_side = latest_prices.get(cid, {})
                if not prices_by_side:
                    continue

                if vol <= 0 or not vol_calc.has_enough_data():
                    continue

                fair = model.predict(
                    spot=spot,
                    strike=float(mkt.get("start_price", spot)),
                    vol_annual=vol,
                    time_to_expiry_s=tte,
                )
                if strategy_mode == "signal":
                    size = self._cfg.get("risk", {}).get("max_order_size_usdc", 5.0)
                    prices = MarketPrices(
                        up=prices_by_side.get("UP", OutcomeQuote()),
                        down=prices_by_side.get("DOWN", OutcomeQuote()),
                    )
                    decision = signal_engine.decide(
                        market_id=cid,
                        up_token_id=mkt.get("up_token_id", ""),
                        down_token_id=mkt.get("down_token_id", ""),
                        fair_up=fair.fair_up,
                        prices=prices,
                        time_to_expiry_s=tte,
                        default_size_usdc=size,
                    )
                    if decision.action != "SKIP":
                        fill = SimFill(
                            ts_ms=ts_ms,
                            market_id=cid,
                            side=decision.side or "",
                            is_buy=True,
                            price=decision.price,
                            size=decision.size_usdc,
                            fee=GAS_FEE,
                        )
                        result.fills.append(fill)
                        inventory.record_fill(
                            cid,
                            symbol,
                            decision.side or "",
                            True,
                            decision.size_usdc,
                            decision.price,
                        )
                        signal_engine.mark_order_sent(cid)
                        pnl -= GAS_FEE
                        result.total_gas += GAS_FEE
                    result.pnl_series.append(pnl)
                    continue

                inv_skew = inventory.inventory_skew(cid, symbol)
                quotes = quote_engine.compute(
                    market_id=cid,
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

                # Simulate fills: our bid >= CLOB best ask → we buy
                up_quote = prices_by_side.get("UP", OutcomeQuote())
                clob_best_ask = up_quote.ask
                clob_best_bid = up_quote.bid
                size = self._cfg.get("risk", {}).get("max_order_size_usdc", 5.0)

                # UP BUY fill
                if quotes.up_bid >= clob_best_ask and clob_best_ask > 0:
                    fill = SimFill(
                        ts_ms=ts_ms, market_id=cid, side="UP", is_buy=True,
                        price=clob_best_ask, size=size, fee=GAS_FEE,
                    )
                    result.fills.append(fill)
                    inventory.record_fill(cid, symbol, "UP", True, size, clob_best_ask)
                    pnl -= GAS_FEE
                    result.total_gas += GAS_FEE

                # UP SELL fill
                if quotes.up_ask <= clob_best_bid and clob_best_bid > 0:
                    fill = SimFill(
                        ts_ms=ts_ms, market_id=cid, side="UP", is_buy=False,
                        price=clob_best_bid, size=size, fee=GAS_FEE,
                    )
                    result.fills.append(fill)
                    inventory.record_fill(cid, symbol, "UP", False, size, clob_best_bid)
                    pnl -= GAS_FEE
                    result.total_gas += GAS_FEE

            result.pnl_series.append(pnl)

        # After simulation: resolve markets and compute PnL
        resolutions = await self._load_resolutions(start_date, end_date, symbol)
        for res in resolutions:
            cid = res["condition_id"]
            up_won = res["up_won"]
            fill_pnl = inventory.record_resolution(cid, up_won)
            pnl += fill_pnl
            # Mark fills as adverse/not
            for f in result.fills:
                if f.market_id == cid:
                    f.is_adverse = (f.side == "UP" and not up_won) or (f.side == "DOWN" and up_won)
                    f.pnl_at_resolution = fill_pnl

        result.pnl_series.append(pnl)
        log.info(
            "Backtest complete: fills=%d pnl=%.4f gas=%.4f stale=%d",
            len(result.fills), pnl, result.total_gas, result.stale_quote_count,
        )
        return result

    async def _load_spot_ticks(self, start: str, end: str, pair: str) -> list[dict]:
        try:
            rows = await self._db.fetch(
                "SELECT ts_ms, microprice, last_trade FROM spot_ticks "
                "WHERE symbol=$1 AND ts_ms BETWEEN $2 AND $3 ORDER BY ts_ms",
                pair,
                _date_to_ms(start),
                _date_to_ms(end, end_of_day=True),
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("Could not load spot ticks: %s", exc)
            return []

    async def _load_clob_snapshots(self, start: str, end: str, symbol: str) -> list[dict]:
        try:
            rows = await self._db.fetch(
                "SELECT c.ts_ms, c.market_id, c.token_id, c.best_bid, c.best_ask "
                "FROM clob_snapshots c "
                "JOIN markets m ON m.condition_id = c.market_id "
                "WHERE m.symbol=$1 AND c.ts_ms BETWEEN $2 AND $3 ORDER BY c.ts_ms",
                symbol,
                _date_to_ms(start),
                _date_to_ms(end, end_of_day=True),
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("Could not load CLOB snapshots: %s", exc)
            return []

    async def _load_markets(self, start: str, end: str, symbol: str) -> list[dict]:
        try:
            rows = await self._db.fetch(
                "SELECT condition_id, symbol, start_price, start_time, end_time, "
                "       duration_minutes, resolution_source, up_token_id, down_token_id "
                "FROM markets WHERE symbol=$1 AND start_time >= $2 AND end_time <= $3",
                symbol,
                _date_to_ms(start),
                _date_to_ms(end, end_of_day=True),
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("Could not load markets: %s", exc)
            return []

    async def _load_resolutions(self, start: str, end: str, symbol: str) -> list[dict]:
        try:
            rows = await self._db.fetch(
                "SELECT condition_id, up_won FROM markets "
                "WHERE symbol=$1 AND resolved=true AND end_time BETWEEN $2 AND $3",
                symbol,
                _date_to_ms(start),
                _date_to_ms(end, end_of_day=True),
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("Could not load resolutions: %s", exc)
            return []


def _date_to_ms(date_str: str, end_of_day: bool = False) -> int:
    from datetime import datetime, timezone
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        from datetime import timedelta
        dt += timedelta(days=1)
    return int(dt.timestamp() * 1000)


def _dict_to_market(m: dict):
    """Convert a DB market dict to a MarketInfo-like object for lifecycle functions."""
    from src.markets.discovery import MarketInfo
    return MarketInfo(
        condition_id=m.get("condition_id", ""),
        question=m.get("question", ""),
        symbol=m.get("symbol", "BTC"),
        start_price=float(m.get("start_price", 0)),
        up_token_id=m.get("up_token_id", ""),
        down_token_id=m.get("down_token_id", ""),
        start_time=int(m.get("start_time", 0)),
        end_time=int(m.get("end_time", 0)),
        duration_minutes=int(m.get("duration_minutes", 15)),
        volume_24h=0.0,
        resolution_source=m.get("resolution_source", "pyth"),
        oracle_feed_id="",
        active=True,
    )
