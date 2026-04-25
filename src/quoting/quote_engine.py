"""
Quote engine.

Computes bid/ask prices for UP and DOWN tokens around the fair value.
Spread is a function of:
  - base spread (config)
  - realized volatility (wider when uncertain)
  - time-to-expiry (wider as expiry approaches)
  - inventory skew (asymmetric to rebalance position)
  - market lifecycle stage (NEW = wider, DANGER = don't quote)

Also implements preemptive cancel logic: if spot moves more than
cancel_threshold_bps since the last quote, all quotes are stale.
"""
import logging
import math
import time
from dataclasses import dataclass

from src.markets.lifecycle import MarketStage

log = logging.getLogger(__name__)


@dataclass
class Quotes:
    market_id: str
    ts_ms: int
    fair_up: float
    fair_down: float
    # UP token
    up_bid: float
    up_ask: float
    # DOWN token
    down_bid: float
    down_ask: float
    half_spread: float
    should_quote: bool
    reason: str          # why not quoting (empty if should_quote=True)


class QuoteEngine:
    def __init__(self, cfg: dict):
        self._base_half = cfg.get("base_half_spread", 0.020)
        self._vol_scale = cfg.get("vol_spread_scale", 2.0)
        self._min_half = cfg.get("min_half_spread", 0.015)
        self._max_half = cfg.get("max_half_spread", 0.100)
        self._inv_skew_per_unit = cfg.get("inventory_skew_per_unit", 0.005)
        self._stop_s = cfg.get("stop_quoting_seconds", 60)
        self._widen_s = cfg.get("widen_near_expiry_seconds", 120)
        self._cancel_bps = cfg.get("spot_move_threshold_bps", 15)
        self._cancel_vol_spike = cfg.get("cancel_on_vol_spike", True)
        self._cancel_vol_mult = cfg.get("vol_spike_multiplier", 2.0)

        # Per-market state for preemptive cancel
        self._last_spot_at_quote: dict[str, float] = {}
        self._baseline_vol: dict[str, float] = {}

    def compute(
        self,
        market_id: str,
        fair_up: float,
        vol_annual: float,
        time_to_expiry_s: float,
        stage: MarketStage,
        inventory_skew: float,   # [-1, 1], positive = over-long UP
        spot: float,
        avg_vol: float,          # long-run average vol for spike detection
    ) -> Quotes:
        now_ms = int(time.time() * 1000)
        fair_down = 1.0 - fair_up

        # Hard stops
        if stage == MarketStage.EXPIRED:
            return Quotes(market_id, now_ms, fair_up, fair_down,
                          0, 0, 0, 0, 0, False, "expired")
        if stage == MarketStage.DANGER or time_to_expiry_s <= self._stop_s:
            return Quotes(market_id, now_ms, fair_up, fair_down,
                          0, 0, 0, 0, 0, False, f"danger zone ({time_to_expiry_s:.0f}s to expiry)")

        # Base half-spread from vol
        vol_contribution = vol_annual * self._vol_scale * math.sqrt(
            max(1, time_to_expiry_s) / (365 * 24 * 3600)
        )
        half_spread = self._base_half + vol_contribution

        # Widen near expiry
        if stage == MarketStage.NEAR_EXPIRY or time_to_expiry_s <= self._widen_s:
            taper = 1.0 - (time_to_expiry_s - self._stop_s) / (self._widen_s - self._stop_s)
            half_spread *= 1.0 + 2.0 * taper  # up to 3x wider near expiry

        # Widen for new markets (less information)
        if stage == MarketStage.NEW:
            half_spread *= 1.5

        # Clamp spread
        half_spread = max(self._min_half, min(self._max_half, half_spread))

        # Inventory skew: shift quotes asymmetrically to rebalance
        # Over-long UP → raise UP ask (harder for takers to hit), lower DOWN ask (attract DOWN)
        up_skew = inventory_skew * self._inv_skew_per_unit * abs(inventory_skew)
        down_skew = -up_skew

        up_bid = max(0.01, fair_up - half_spread + up_skew)
        up_ask = min(0.99, fair_up + half_spread + up_skew)
        down_bid = max(0.01, fair_down - half_spread + down_skew)
        down_ask = min(0.99, fair_down + half_spread + down_skew)

        # Sanity: bid < ask
        if up_bid >= up_ask:
            up_bid = fair_up - self._min_half
            up_ask = fair_up + self._min_half
        if down_bid >= down_ask:
            down_bid = fair_down - self._min_half
            down_ask = fair_down + self._min_half

        # Record spot for preemptive cancel tracking
        self._last_spot_at_quote[market_id] = spot
        if avg_vol > 0:
            self._baseline_vol[market_id] = avg_vol

        return Quotes(
            market_id=market_id,
            ts_ms=now_ms,
            fair_up=fair_up,
            fair_down=fair_down,
            up_bid=round(up_bid, 4),
            up_ask=round(up_ask, 4),
            down_bid=round(down_bid, 4),
            down_ask=round(down_ask, 4),
            half_spread=round(half_spread, 4),
            should_quote=True,
            reason="",
        )

    def is_stale(self, market_id: str, current_spot: float, current_vol: float) -> bool:
        """
        Returns True if existing quotes should be cancelled immediately.
        Triggered by large spot move or vol spike since last quote.
        """
        last_spot = self._last_spot_at_quote.get(market_id)
        if last_spot and last_spot > 0:
            move_bps = abs(current_spot - last_spot) / last_spot * 10_000
            if move_bps > self._cancel_bps:
                log.debug("Stale quotes for %s: spot moved %.1f bps", market_id, move_bps)
                return True

        if self._cancel_vol_spike:
            baseline_vol = self._baseline_vol.get(market_id, 0.0)
            if baseline_vol > 0 and current_vol > baseline_vol * self._cancel_vol_mult:
                log.debug("Stale quotes for %s: vol spike %.2f vs baseline %.2f",
                          market_id, current_vol, baseline_vol)
                return True

        return False
