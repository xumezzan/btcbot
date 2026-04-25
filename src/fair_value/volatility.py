"""
Realized volatility calculator from rolling spot returns.

Maintains a ring buffer of (timestamp, price) pairs and computes
annualized realized volatility on demand.
"""
import math
import time
from collections import deque


class RealizedVolatility:
    """
    Rolling realized volatility from log returns over a configurable window.

    Uses 5-minute sampled prices to avoid microstructure noise.
    Returns annualized vol (e.g. 0.80 = 80% p.a.).
    """

    SAMPLE_INTERVAL_MS = 300_000   # 5-minute samples

    def __init__(self, window_minutes: int = 60, min_periods: int = 12):
        self._window_ms = window_minutes * 60 * 1000
        self._min_periods = min_periods
        # Stores (bucket_ts_ms, sampled_price)
        self._samples: deque[tuple[int, float]] = deque()
        self._last_sample_ts: int = 0
        self._last_price: float = 0.0

    def update(self, price: float, ts_ms: int | None = None) -> None:
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)

        self._last_price = price

        # Sample at 5-minute intervals
        bucket = (ts_ms // self.SAMPLE_INTERVAL_MS) * self.SAMPLE_INTERVAL_MS
        if bucket > self._last_sample_ts:
            self._last_sample_ts = bucket
            self._samples.append((bucket, price))

        # Trim samples outside the window
        cutoff = ts_ms - self._window_ms
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def get(self) -> float:
        """Return annualized realized vol. Returns 0.0 if insufficient data."""
        if len(self._samples) < self._min_periods:
            return 0.0

        prices = [p for _, p in self._samples]
        log_returns = [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
            if prices[i - 1] > 0
        ]
        if len(log_returns) < 2:
            return 0.0

        n = len(log_returns)
        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
        std_per_sample = math.sqrt(variance)

        # Annualize: samples are 5-minute, so 105,120 samples/year
        # = 365 * 24 * 12 = 105,120
        samples_per_year = 365 * 24 * (60 // (self.SAMPLE_INTERVAL_MS // 60_000))
        return std_per_sample * math.sqrt(samples_per_year)

    def get_last_price(self) -> float:
        return self._last_price

    def has_enough_data(self) -> bool:
        return len(self._samples) >= self._min_periods
