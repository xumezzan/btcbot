"""
Fair value model for Polymarket Up/Down prediction markets.

Uses a normal distribution model (equivalent to Bachelier/GBM) to estimate
P(price at expiry > strike). Applies a momentum adjustment based on recent
spot direction.

IMPORTANT: The model is calibrated against actual resolutions. Run
calibration.py regularly to check for systematic bias and update
the bias_correction parameter.
"""
import math
import time
from dataclasses import dataclass, field
from scipy.stats import norm


@dataclass
class FairValue:
    fair_up: float       # P(UP wins) in [0, 1]
    fair_down: float     # = 1 - fair_up (not accounting for fees)
    spot: float
    strike: float
    vol_annual: float
    time_to_expiry_s: float
    momentum_adj: float  # raw momentum adjustment applied
    bias_correction: float


class FairValueModel:
    """
    P(UP) = N(d) where:
      d = (log(spot/strike) + momentum_adj) / (vol * sqrt(T))
      N = standard normal CDF
      T = time to expiry in years

    With zero time to expiry, the model snaps to 1.0 or 0.0.

    The momentum_adj shifts the expected final price based on recent trend.
    The bias_correction is an additive adjustment calibrated from historical data.
    """

    SECONDS_PER_YEAR = 365 * 24 * 3600

    def __init__(self, cfg: dict):
        self._momentum_window_s = cfg.get("momentum_window_seconds", 300)
        self._momentum_weight = cfg.get("momentum_weight", 0.3)
        self._bias_correction: float = 0.0  # updated by calibration pipeline
        self._default_vol = 0.80  # fallback annual vol if no data

        # Rolling price history for momentum: list of (ts_s, price)
        self._price_history: list[tuple[float, float]] = []

    def update_price_history(self, price: float, ts_s: float | None = None) -> None:
        if ts_s is None:
            ts_s = time.time()
        self._price_history.append((ts_s, price))
        cutoff = ts_s - self._momentum_window_s
        self._price_history = [(t, p) for t, p in self._price_history if t >= cutoff]

    def _momentum_adjustment(self) -> float:
        """
        Returns an additive log-return adjustment.
        If spot has been trending up, this makes UP slightly more likely.
        """
        if len(self._price_history) < 2:
            return 0.0
        first_price = self._price_history[0][1]
        last_price = self._price_history[-1][1]
        if first_price <= 0:
            return 0.0
        return math.log(last_price / first_price) * self._momentum_weight

    def predict(
        self,
        spot: float,
        strike: float,
        vol_annual: float,
        time_to_expiry_s: float,
    ) -> FairValue:
        if vol_annual <= 0:
            vol_annual = self._default_vol

        # Snap to boundary at expiry
        if time_to_expiry_s <= 0:
            fair_up = 1.0 if spot > strike else 0.0
            return FairValue(
                fair_up=fair_up,
                fair_down=1.0 - fair_up,
                spot=spot,
                strike=strike,
                vol_annual=vol_annual,
                time_to_expiry_s=0.0,
                momentum_adj=0.0,
                bias_correction=self._bias_correction,
            )

        T = time_to_expiry_s / self.SECONDS_PER_YEAR
        if strike <= 0 or spot <= 0:
            return FairValue(
                fair_up=0.5, fair_down=0.5,
                spot=spot, strike=strike,
                vol_annual=vol_annual,
                time_to_expiry_s=time_to_expiry_s,
                momentum_adj=0.0,
                bias_correction=self._bias_correction,
            )

        mom = self._momentum_adjustment()
        # d = (log(S/K) + momentum_adj) / (sigma * sqrt(T))
        # For GBM: add drift = -0.5 * vol^2 * T to numerator (risk-neutral)
        # We omit risk-free rate (Polymarket is in USDC, negligible)
        drift = -0.5 * vol_annual ** 2 * T
        d = (math.log(spot / strike) + drift + mom) / (vol_annual * math.sqrt(T))
        fair_up = float(norm.cdf(d))

        # Apply calibration bias correction (additive, clamp to [0.01, 0.99])
        fair_up = max(0.01, min(0.99, fair_up + self._bias_correction))

        return FairValue(
            fair_up=fair_up,
            fair_down=1.0 - fair_up,
            spot=spot,
            strike=strike,
            vol_annual=vol_annual,
            time_to_expiry_s=time_to_expiry_s,
            momentum_adj=mom,
            bias_correction=self._bias_correction,
        )

    def set_bias_correction(self, correction: float) -> None:
        self._bias_correction = correction
