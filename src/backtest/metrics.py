"""
Backtest metrics.

Computes standard trading strategy metrics from a sequence of trade records.
"""
import math
from dataclasses import dataclass


@dataclass
class BacktestMetrics:
    total_pnl: float
    n_fills: int
    n_markets: int
    pnl_per_fill: float
    win_rate: float              # fraction of markets where PnL > 0
    sharpe_ratio: float          # daily Sharpe (annualized)
    max_drawdown: float          # max peak-to-trough drawdown
    adverse_selection_rate: float
    fill_rate_per_hour: float
    total_fees: float
    net_pnl_after_fees: float


def compute_metrics(
    pnl_series: list[float],      # cumulative PnL at each event
    fills: list[dict],            # list of {pnl, market_id, is_adverse}
    duration_hours: float,
    total_fees: float,
) -> BacktestMetrics:
    if not fills:
        return BacktestMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    total_pnl = pnl_series[-1] if pnl_series else 0.0
    n_fills = len(fills)
    markets = {f["market_id"] for f in fills}
    n_markets = len(markets)

    # Win rate: per market
    market_pnl: dict[str, float] = {}
    for f in fills:
        market_pnl[f["market_id"]] = market_pnl.get(f["market_id"], 0.0) + f["pnl"]
    winning = sum(1 for v in market_pnl.values() if v > 0)
    win_rate = winning / n_markets if n_markets else 0.0

    # Sharpe from daily PnL (requires pnl_series bucketed by day)
    sharpe = _daily_sharpe(pnl_series)

    # Max drawdown
    max_dd = _max_drawdown(pnl_series)

    # Adverse selection
    resolved = [f for f in fills if "is_adverse" in f]
    adverse_rate = (
        sum(1 for f in resolved if f["is_adverse"]) / len(resolved)
        if resolved else 0.0
    )

    fill_rate = n_fills / duration_hours if duration_hours > 0 else 0.0
    net = total_pnl - total_fees

    return BacktestMetrics(
        total_pnl=round(total_pnl, 4),
        n_fills=n_fills,
        n_markets=n_markets,
        pnl_per_fill=round(total_pnl / n_fills, 6) if n_fills else 0.0,
        win_rate=round(win_rate, 4),
        sharpe_ratio=round(sharpe, 3),
        max_drawdown=round(max_dd, 4),
        adverse_selection_rate=round(adverse_rate, 4),
        fill_rate_per_hour=round(fill_rate, 2),
        total_fees=round(total_fees, 4),
        net_pnl_after_fees=round(net, 4),
    )


def _daily_sharpe(pnl_series: list[float], bucket_size: int = 100) -> float:
    """Approximate Sharpe from bucketed returns. Returns 0 if insufficient data."""
    if len(pnl_series) < bucket_size * 2:
        return 0.0
    returns = []
    for i in range(bucket_size, len(pnl_series), bucket_size):
        r = pnl_series[i] - pnl_series[i - bucket_size]
        returns.append(r)
    if len(returns) < 5:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return 0.0
    # Annualize: bucket = ~1 day if bucket_size ≈ fills/day
    return mean / std * math.sqrt(252)


def _max_drawdown(pnl_series: list[float]) -> float:
    if not pnl_series:
        return 0.0
    peak = pnl_series[0]
    max_dd = 0.0
    for v in pnl_series:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return max_dd
