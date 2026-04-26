from __future__ import annotations

"""
Value-betting signal engine for Up/Down markets.

The engine compares the model's fair probability with the executable CLOB ask.
It only emits a BUY signal when the model edge is large enough after all
configured safety filters. Otherwise it returns SKIP with a reason.
"""
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class OutcomeQuote:
    bid: float = 0.0
    ask: float = 0.0
    ask_size: float = 0.0


@dataclass(frozen=True)
class MarketPrices:
    up: OutcomeQuote
    down: OutcomeQuote


@dataclass(frozen=True)
class SignalDecision:
    action: str          # BUY_UP | BUY_DOWN | SKIP
    side: str | None     # UP | DOWN | None
    token_id: str
    price: float
    size_usdc: float
    edge: float
    fair_probability: float
    market_probability: float
    reason: str


class ValueSignalEngine:
    """Selects directional buys when fair value exceeds market price enough."""

    def __init__(self, cfg: dict):
        self._enabled = cfg.get("enabled", True)
        self._min_edge = float(cfg.get("min_edge", 0.04))
        self._min_probability = float(cfg.get("min_probability", 0.55))
        self._max_entry_price = float(cfg.get("max_entry_price", 0.95))
        self._min_seconds_to_expiry = float(cfg.get("min_seconds_to_expiry", 60))
        self._max_seconds_to_expiry = float(cfg.get("max_seconds_to_expiry", 15 * 60))
        self._min_ask_size_usdc = float(cfg.get("min_ask_size_usdc", 1.0))
        self._order_size_usdc = float(cfg.get("order_size_usdc", 0.0))
        self._trade_up = "UP" in cfg.get("trade_sides", ["UP", "DOWN"])
        self._trade_down = "DOWN" in cfg.get("trade_sides", ["UP", "DOWN"])
        self._cooldown_s = float(cfg.get("cooldown_seconds", 20))
        self._one_position_per_market = cfg.get("one_position_per_market", True)
        self._last_signal_ts: dict[str, float] = {}
        self._traded_markets: set[str] = set()

    @property
    def min_edge(self) -> float:
        return self._min_edge

    def mark_order_sent(self, market_id: str) -> None:
        now = time.time()
        self._last_signal_ts[market_id] = now
        if self._one_position_per_market:
            self._traded_markets.add(market_id)

    def decide(
        self,
        *,
        market_id: str,
        up_token_id: str,
        down_token_id: str,
        fair_up: float,
        prices: MarketPrices | None,
        time_to_expiry_s: float,
        default_size_usdc: float,
    ) -> SignalDecision:
        if not self._enabled:
            return self._skip("disabled")

        if self._one_position_per_market and market_id in self._traded_markets:
            return self._skip("already traded market")

        now = time.time()
        last_signal = self._last_signal_ts.get(market_id, 0.0)
        if now - last_signal < self._cooldown_s:
            return self._skip("cooldown")

        if time_to_expiry_s < self._min_seconds_to_expiry:
            return self._skip("too close to expiry")
        if time_to_expiry_s > self._max_seconds_to_expiry:
            return self._skip("too far from expiry")
        if prices is None:
            return self._skip("missing clob prices")

        size = self._order_size_usdc or default_size_usdc
        candidates: list[SignalDecision] = []

        if self._trade_up:
            candidates.append(self._candidate(
                action="BUY_UP",
                side="UP",
                token_id=up_token_id,
                fair_probability=fair_up,
                quote=prices.up,
                size_usdc=size,
            ))

        if self._trade_down:
            candidates.append(self._candidate(
                action="BUY_DOWN",
                side="DOWN",
                token_id=down_token_id,
                fair_probability=1.0 - fair_up,
                quote=prices.down,
                size_usdc=size,
            ))

        candidates = [c for c in candidates if c.action != "SKIP"]
        if not candidates:
            return self._skip("no executable candidates")

        best = max(candidates, key=lambda c: c.edge)
        if best.edge < self._min_edge:
            return self._skip(f"edge {best.edge:.4f} below min {self._min_edge:.4f}")
        return best

    def _candidate(
        self,
        *,
        action: str,
        side: str,
        token_id: str,
        fair_probability: float,
        quote: OutcomeQuote,
        size_usdc: float,
    ) -> SignalDecision:
        ask = quote.ask
        if ask <= 0:
            return self._skip(f"missing {side} ask")
        if quote.ask_size < self._min_ask_size_usdc:
            return self._skip(f"{side} ask size too small")
        if ask > self._max_entry_price:
            return self._skip(f"{side} ask above max entry")
        if fair_probability < self._min_probability:
            return self._skip(f"{side} fair probability too low")

        edge = fair_probability - ask
        return SignalDecision(
            action=action,
            side=side,
            token_id=token_id,
            price=round(ask, 4),
            size_usdc=size_usdc,
            edge=edge,
            fair_probability=fair_probability,
            market_probability=ask,
            reason="edge",
        )

    @staticmethod
    def _skip(reason: str) -> SignalDecision:
        return SignalDecision(
            action="SKIP",
            side=None,
            token_id="",
            price=0.0,
            size_usdc=0.0,
            edge=0.0,
            fair_probability=0.0,
            market_probability=0.0,
            reason=reason,
        )
