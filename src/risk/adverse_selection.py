"""
Adverse selection tracker.

For every fill we receive, we record:
  - the side (UP or DOWN)
  - the market's resolution outcome once it settles

Then we compute what fraction of fills resolved against us.
If >50% of fills are adversely selected, we're being systematically
picked off by informed traders and the edge is negative.
"""
import logging
import time
from dataclasses import dataclass, field
from collections import deque

log = logging.getLogger(__name__)

# We only evaluate fills where resolution is known within this window
EVAL_WINDOW_HOURS = 24


@dataclass
class FillRecord:
    fill_id: str
    market_id: str
    token_side: str      # "UP" or "DOWN"
    price: float
    size: float
    ts_ms: int
    resolved: bool = False
    up_won: bool | None = None   # set after resolution
    is_adverse: bool | None = None


class AdverseSelectionTracker:
    """
    Tracks fill quality over a rolling window.
    A fill is "adverse" if we bought UP and DOWN won (or vice versa).
    """

    def __init__(self, max_window: int = 2000):
        self._fills: deque[FillRecord] = deque(maxlen=max_window)
        self._fill_index: dict[str, FillRecord] = {}

    def record_fill(
        self,
        fill_id: str,
        market_id: str,
        token_side: str,
        price: float,
        size: float,
        ts_ms: int | None = None,
    ) -> None:
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        rec = FillRecord(
            fill_id=fill_id,
            market_id=market_id,
            token_side=token_side,
            price=price,
            size=size,
            ts_ms=ts_ms,
        )
        self._fills.append(rec)
        self._fill_index[fill_id] = rec

    def record_resolution(self, market_id: str, up_won: bool) -> None:
        """Mark all fills for this market as resolved."""
        resolved_count = 0
        adverse_count = 0
        for rec in self._fills:
            if rec.market_id == market_id and not rec.resolved:
                rec.resolved = True
                rec.up_won = up_won
                # Adverse: we bought UP but DOWN won, or bought DOWN but UP won
                rec.is_adverse = (rec.token_side == "UP") != up_won
                resolved_count += 1
                if rec.is_adverse:
                    adverse_count += 1
        if resolved_count:
            log.debug("Market %s resolved (up_won=%s): %d fills, %d adverse",
                      market_id, up_won, resolved_count, adverse_count)

    def adverse_selection_rate(self, min_samples: int = 30) -> float | None:
        """
        Returns adverse selection rate over resolved fills.
        Returns None if fewer than min_samples resolved fills are available.
        """
        resolved = [r for r in self._fills if r.resolved]
        if len(resolved) < min_samples:
            return None
        adverse = sum(1 for r in resolved if r.is_adverse)
        return adverse / len(resolved)

    def adverse_selection_rate_by_market(self) -> dict[str, float]:
        """Per-market adverse selection rates (only markets with ≥5 resolved fills)."""
        by_market: dict[str, list[bool]] = {}
        for rec in self._fills:
            if rec.resolved and rec.is_adverse is not None:
                by_market.setdefault(rec.market_id, []).append(rec.is_adverse)
        return {
            mid: sum(v) / len(v)
            for mid, v in by_market.items()
            if len(v) >= 5
        }

    def recent_fill_count(self, window_seconds: int = 3600) -> int:
        cutoff = int(time.time() * 1000) - window_seconds * 1000
        return sum(1 for r in self._fills if r.ts_ms >= cutoff)
