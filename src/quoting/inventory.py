"""
Inventory manager.

Tracks net UP/DOWN exposure per market and across all markets.
Computes net delta (directional BTC/ETH exposure) and provides
inventory-skew signals to the quote engine.

Cross-asset note: BTC and ETH are ~0.85 correlated. This manager
combines their net deltas into a single cross-asset delta view.
"""
from dataclasses import dataclass, field


# BTC-ETH correlation for cross-asset delta (empirical, ~0.85)
BTC_ETH_CORR = 0.85


@dataclass
class MarketPosition:
    market_id: str
    symbol: str          # BTC / ETH etc.
    up_shares: float = 0.0    # shares of UP token held (positive = long UP)
    down_shares: float = 0.0  # shares of DOWN token held
    avg_up_price: float = 0.0
    avg_down_price: float = 0.0
    realized_pnl: float = 0.0

    @property
    def net_delta_usdc(self) -> float:
        """Net directional exposure: long UP = positive delta."""
        return self.up_shares - self.down_shares

    @property
    def gross_exposure_usdc(self) -> float:
        return abs(self.up_shares * self.avg_up_price) + abs(self.down_shares * self.avg_down_price)


class InventoryManager:
    def __init__(self):
        self._positions: dict[str, MarketPosition] = {}

    def get_or_create(self, market_id: str, symbol: str) -> MarketPosition:
        if market_id not in self._positions:
            self._positions[market_id] = MarketPosition(market_id=market_id, symbol=symbol)
        return self._positions[market_id]

    def record_fill(
        self,
        market_id: str,
        symbol: str,
        side: str,        # "UP" or "DOWN"
        is_buy: bool,
        shares: float,
        price: float,
    ) -> None:
        pos = self.get_or_create(market_id, symbol)
        if side == "UP":
            if is_buy:
                total_cost = pos.up_shares * pos.avg_up_price + shares * price
                pos.up_shares += shares
                pos.avg_up_price = total_cost / pos.up_shares if pos.up_shares > 0 else price
            else:
                proceeds = shares * price
                cost_basis = shares * pos.avg_up_price
                pos.realized_pnl += proceeds - cost_basis
                pos.up_shares = max(0.0, pos.up_shares - shares)
        else:  # DOWN
            if is_buy:
                total_cost = pos.down_shares * pos.avg_down_price + shares * price
                pos.down_shares += shares
                pos.avg_down_price = total_cost / pos.down_shares if pos.down_shares > 0 else price
            else:
                proceeds = shares * price
                cost_basis = shares * pos.avg_down_price
                pos.realized_pnl += proceeds - cost_basis
                pos.down_shares = max(0.0, pos.down_shares - shares)

    def record_resolution(self, market_id: str, up_won: bool) -> float:
        """Mark market as resolved, compute settlement PnL. Returns PnL."""
        pos = self._positions.get(market_id)
        if not pos:
            return 0.0
        if up_won:
            settlement = pos.up_shares * 1.0 - pos.up_shares * pos.avg_up_price
            settlement += pos.down_shares * 0.0 - pos.down_shares * pos.avg_down_price
        else:
            settlement = pos.down_shares * 1.0 - pos.down_shares * pos.avg_down_price
            settlement += pos.up_shares * 0.0 - pos.up_shares * pos.avg_up_price
        pos.realized_pnl += settlement
        pos.up_shares = 0.0
        pos.down_shares = 0.0
        return pos.realized_pnl

    def inventory_skew(self, market_id: str, symbol: str) -> float:
        """
        Returns skew in [-1, 1].
        Positive = over-long UP (should widen UP ask, tighten DOWN ask).
        Negative = over-long DOWN (reverse).
        """
        pos = self._positions.get(market_id)
        if not pos:
            return 0.0
        gross = pos.gross_exposure_usdc
        if gross == 0:
            return 0.0
        return pos.net_delta_usdc / gross

    def net_delta_per_symbol(self) -> dict[str, float]:
        """Sum of net delta (UP - DOWN) in USDC per asset symbol."""
        totals: dict[str, float] = {}
        for pos in self._positions.values():
            totals[pos.symbol] = totals.get(pos.symbol, 0.0) + pos.net_delta_usdc
        return totals

    def cross_asset_delta(self) -> float:
        """
        Combined BTC-equivalent delta accounting for BTC/ETH correlation.
        Used for portfolio-level risk monitoring.
        """
        deltas = self.net_delta_per_symbol()
        btc = deltas.get("BTC", 0.0)
        eth = deltas.get("ETH", 0.0) * BTC_ETH_CORR
        # Other assets at face value
        others = sum(v for k, v in deltas.items() if k not in ("BTC", "ETH"))
        return btc + eth + others

    def total_exposure(self) -> float:
        return sum(p.gross_exposure_usdc for p in self._positions.values())

    def all_positions(self) -> list[MarketPosition]:
        return list(self._positions.values())
