"""
Market lifecycle classifier.

A market transitions through stages based on elapsed time and time-to-expiry.
The strategy applies different quoting behaviour at each stage.
"""
import time
from enum import Enum, auto

from src.markets.discovery import MarketInfo


class MarketStage(Enum):
    NEW = auto()         # First 20% of duration — uncertain, quote with very wide spreads
    ACTIVE = auto()      # 20%–80% of duration — normal market-making zone
    NEAR_EXPIRY = auto() # Last 20% (but > 60s) — widen spreads significantly
    DANGER = auto()      # Last 60 seconds — stop quoting (filled = adversely selected)
    EXPIRED = auto()     # Past end_time


def classify(market: MarketInfo, now_ms: int | None = None) -> MarketStage:
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    if now_ms >= market.end_time:
        return MarketStage.EXPIRED

    duration_ms = market.end_time - market.start_time
    if duration_ms <= 0:
        return MarketStage.EXPIRED

    elapsed_ms = now_ms - market.start_time
    remaining_ms = market.end_time - now_ms
    elapsed_frac = elapsed_ms / duration_ms

    if remaining_ms <= 60_000:
        return MarketStage.DANGER
    if remaining_ms <= 120_000:  # 2 minutes
        return MarketStage.NEAR_EXPIRY
    if elapsed_frac < 0.20:
        return MarketStage.NEW
    if elapsed_frac > 0.80:
        return MarketStage.NEAR_EXPIRY
    return MarketStage.ACTIVE


def seconds_to_expiry(market: MarketInfo, now_ms: int | None = None) -> float:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return max(0.0, (market.end_time - now_ms) / 1000.0)


def elapsed_fraction(market: MarketInfo, now_ms: int | None = None) -> float:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    duration_ms = market.end_time - market.start_time
    if duration_ms <= 0:
        return 1.0
    return min(1.0, max(0.0, (now_ms - market.start_time) / duration_ms))
