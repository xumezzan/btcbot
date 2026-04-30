# Live Trading Gates

Live trading is allowed only after these gates are satisfied. Passing the
readiness script is required, but it is not enough by itself.

## Gate 0: Local Readiness

Commands:

```bash
python3 -m pytest -q
python3 scripts/check_live_readiness.py
```

Requirements:

- Tests pass.
- `.env` contains valid Polymarket credentials.
- Postgres is reachable.
- Required schema tables exist.
- CLOB read-only auth works.
- Starter risk limits are not exceeded.
- `signal.allow_taker` remains `false`.

## Gate 1: Data Quality

Requirements:

- At least 2 weeks of usable data; 4 weeks preferred.
- Binance and CLOB feed gaps are quantified.
- Market discovery catches expected BTC/ETH Up/Down markets.
- UP/DOWN token mapping is verified.
- Spread, volume, and depth distributions are available.

Fail conditions:

- Frequent stale feeds.
- Missing active markets.
- Incorrect token-side mapping.
- Unexplained gaps in CLOB snapshots.

## Gate 2: Edge Versus Market Baseline

Requirements:

- Model Brier score is better than market-implied Brier score out of sample.
- Calibration is not materially worse than market calibration.
- Positive edge buckets remain positive after estimated fees and adverse
  selection.
- Results are stable across different date ranges.

Fail conditions:

- Model only beats market in sample.
- Model is worse than market-implied probability.
- Positive PnL depends on one narrow period or one lucky regime.

## Gate 3: Break-Even Edge

Requirements:

- Break-even edge calculation is documented.
- `signal.min_edge` is greater than break-even edge plus safety margin.
- Maker spread settings are justified by measured costs.

Fail conditions:

- `min_edge` is chosen by intuition.
- Fees, adverse selection, latency, or queue position are ignored.

## Gate 4: Long Dry-Run

Requirements:

- Dry-run has run for 3-4 weeks.
- Candidate signals and hypothetical quotes are logged.
- Latency is measured.
- Competitive depth is measured.
- Hypothetical PnL is positive after realistic costs.

Fail conditions:

- Dry-run period is shorter than 3 weeks.
- PnL is positive only before fees.
- Latency is unknown.
- Fill assumptions are unrealistic.

## Gate 5: Reconciliation

Requirements:

- Startup can recover open orders.
- User fills are fetched and deduplicated.
- Inventory can be rebuilt from exchange state.
- Settlements and redeemed collateral are tracked.
- Local PnL matches reconstructed exchange state.

Fail conditions:

- Restart loses order or position state.
- Filled orders remain locally open.
- PnL cannot be explained from fills and settlements.

## Gate 6: Maker-Only Live Pilot

Requirements:

- One asset.
- One duration.
- Small order size.
- Strict max exposure.
- Daily review.
- `signal.allow_taker: false`.
- No taker entry.

Stop conditions:

- Daily loss limit hit.
- Adverse selection exceeds threshold.
- Reconciliation mismatch.
- Unexpected API behavior.
- Feed latency or stale data becomes material.

## Taker Entry Rule

Taker entry must remain disabled until all of the following are true:

- At least 1000 recorded maker fills exist.
- Maker PnL is positive after fees.
- Adverse selection is measured and controlled.
- Reconciliation is reliable.
- The expected value of taker entry is separately proven against market-implied
  probability and spread cost.

Until then:

```yaml
signal:
  allow_taker: false
```

