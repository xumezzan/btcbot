# btcbot Roadmap

This roadmap is designed to prevent self-deception. The bot should reach live
trading only after data shows that it has edge against the market, not just
against historical outcomes after parameter fitting.

## Phase 0: Operational Base

Goal: make the project reproducible and safe to operate.

- Keep setup instructions current in `README.md`.
- Use Python 3.10+ and run tests with `python3 -m pytest -q`.
- Keep secrets only in `.env`.
- Keep real-money trading blocked unless `I_ACCEPT_REAL_MONEY_RISK=yes` is set.
- Keep `signal.allow_taker: false` until maker-only evidence is strong.
- Maintain `ROADMAP.md` and `LIVE_GATES.md` as the source of truth for progress.

Exit criteria:

- Fresh checkout can install dependencies, create schema, and run tests.
- `python3 -m pytest -q` passes.
- Live trading requirements are documented and explicit.

## Phase 1: Data Collection

Goal: build a reliable dataset before tuning strategy behavior.

- Run `collect` mode for 2-4 weeks.
- Store Binance spot ticks, Polymarket CLOB snapshots, public fills, and market
  metadata.
- Track data quality:
  - missing feed intervals;
  - stale Binance ticks;
  - stale CLOB books;
  - market discovery misses;
  - bad token mapping;
  - spread and depth distributions.

Exit criteria:

- At least 2 weeks of usable data, ideally 4.
- Data quality report shows no major feed gaps or market mapping issues.
- Market duration, oracle, and token-side parsing are verified.

## Phase 1.5: Edge Hypothesis

Goal: write the hypothesis before backtesting.

Current hypothesis:

> The bot can earn money because its fair-value model reacts to Binance spot
> movement and short-term volatility faster or more accurately than the
> Polymarket CLOB mid-price in short-duration crypto Up/Down markets. The
> hypothesis is supported only if the model predicts market resolutions better
> than market-implied probability out of sample, and if maker quotes produce
> positive PnL after fees, spread cost, queue effects, and adverse selection.

What would disprove it:

- Model Brier score is not better than market-implied Brier score.
- Apparent edge disappears out of sample.
- Positive gross edge becomes negative after fees and adverse selection.
- Fill quality is poor because the bot only gets filled when the market moves
  against it.
- Latency is too high to quote before better participants update.

Exit criteria:

- The hypothesis is written before parameter tuning.
- The expected evidence and disconfirming evidence are both explicit.

## Phase 2: Model Validation

Goal: prove whether the model has predictive edge versus the market baseline.

Required comparisons:

- Model fair probability vs actual resolution.
- Market-implied probability vs actual resolution.
- Model Brier score vs market Brier score.
- Calibration bins for both model and market.
- Edge bucket analysis:
  - 0-2 cents;
  - 2-4 cents;
  - 4-6 cents;
  - 6-8 cents;
  - 8+ cents.
- Out-of-sample evaluation by date range.
- Separate analysis by market regime:
  - trend;
  - ranging;
  - volatility expansion;
  - near expiry.

Main gate:

> The model must beat market-implied probability out of sample. If it does not,
> there is no validated edge.

Exit criteria:

- Market baseline metrics exist.
- Model metrics beat market baseline out of sample.
- Winning edge buckets remain positive after estimated fees and adverse
  selection.

## Phase 2.5: Break-Even Edge

Goal: choose `min_edge` from economics, not intuition.

Break-even edge should include:

```text
break_even_edge =
    fees
  + expected adverse selection cost
  + spread/slippage cost
  + cancel/reject/failure cost
  + latency cost
  + safety margin
```

Required outputs:

- A documented break-even edge estimate.
- A recommended `signal.min_edge`.
- A recommended maker half-spread floor.
- Sensitivity table for pessimistic, base, and optimistic assumptions.

Exit criteria:

- `signal.min_edge` is justified by measured costs.
- Maker spread parameters are justified by measured fill quality and adverse
  selection.

## Phase 3: Long Dry-Run

Goal: observe live behavior without real execution.

Minimum duration: 3-4 weeks.

Dry-run must log:

- every candidate signal;
- every skipped signal and reason;
- every hypothetical quote;
- best bid/ask and book depth at decision time;
- expected fill price;
- estimated queue position;
- latency from Binance tick to quote decision;
- hypothetical PnL after fees and adverse selection assumptions.

Exit criteria:

- Dry-run covers multiple crypto regimes.
- Expected PnL remains positive after realistic costs.
- Latency and queue position are measured, not guessed.
- No unresolved data, execution, or reconciliation gaps remain.

## Phase 4: Competitive, Latency, and Reconciliation Work

Goal: make the bot operationally honest.

Competitive analysis:

- Inspect depth at best levels when the bot wants to quote.
- Estimate queue sizes ahead of the bot.
- Track whether fills happen only after adverse spot moves.

Latency budget:

- Measure Binance receive timestamp.
- Measure fair-value compute time.
- Measure decision time.
- Measure CLOB order post/ack time.
- Target end-to-end quote reaction below 500ms, with lower preferred.

Reconciliation:

- On startup, fetch open orders.
- Fetch user fills.
- Rebuild inventory.
- Compare local state to CLOB state.
- Track settlement and redeemed collateral.

Exit criteria:

- Startup state can be reconstructed safely.
- Latency report exists.
- Competitive depth report exists.
- PnL and inventory reconcile with exchange state.

## Phase 5: Maker-Only Live Pilot

Goal: test real execution with minimal capital and maker-only behavior.

Rules:

- Keep `signal.allow_taker: false`.
- Do not enable taker entry.
- Use one asset and one duration first.
- Use small order size and strict exposure caps.
- Review results daily.

Taker rule:

> Taker entry stays disabled until the project has at least 1000 recorded
> maker fills with positive PnL after fees, adverse selection, and settlement
> effects.

Exit criteria:

- Maker fills are positive after all costs.
- Adverse selection is below the configured threshold.
- Reconciliation matches exchange state.
- No operational incidents occur.
- Results match the honest expectations document closely enough to justify
  continuing.

## Phase 6: Scale Only After Evidence

Goal: increase scope only when the previous phases show durable edge.

Possible expansions:

- More assets.
- More durations.
- Larger order sizes.
- More advanced volatility model.
- Adaptive sizing.
- Separate maker and directional signal profiles.

Scale is allowed only if:

- model beats market baseline out of sample;
- maker-only live pilot is profitable after fees;
- latency is acceptable;
- reconciliation is reliable;
- adverse selection is controlled.

## Honest Expectations

Before Phase 5, write a concrete expectation:

```text
I expect to earn $X per month with bankroll $Y,
maximum drawdown $Z,
maximum daily loss $A,
about N fills per week,
and net ROI after fees of at least B%.
```

After the live pilot, compare reality against this document. Do not replace the
original expectation after seeing results.

