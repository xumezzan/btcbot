-- btcbot database schema
-- Run once to set up tables:  psql $DATABASE_URL < schema.sql

-- Binance spot ticks (L2 microprice)
CREATE TABLE IF NOT EXISTS spot_ticks (
    id           BIGSERIAL PRIMARY KEY,
    symbol       TEXT NOT NULL,           -- e.g. 'BTCUSDT'
    ts_ms        BIGINT NOT NULL,         -- exchange event time
    received_ms  BIGINT NOT NULL,         -- local receive time
    bid1         DOUBLE PRECISION,
    ask1         DOUBLE PRECISION,
    microprice   DOUBLE PRECISION,        -- volume-weighted mid
    last_trade   DOUBLE PRECISION,
    UNIQUE(symbol, ts_ms)
);
CREATE INDEX IF NOT EXISTS idx_spot_ticks_symbol_ts ON spot_ticks(symbol, ts_ms);

-- Polymarket CLOB order book snapshots
CREATE TABLE IF NOT EXISTS clob_snapshots (
    id           BIGSERIAL PRIMARY KEY,
    market_id    TEXT NOT NULL,           -- condition_id
    token_id     TEXT NOT NULL,           -- up_token_id
    ts_ms        BIGINT NOT NULL,
    received_ms  BIGINT NOT NULL,
    best_bid     DOUBLE PRECISION,
    best_ask     DOUBLE PRECISION,
    mid          DOUBLE PRECISION,
    UNIQUE(token_id, ts_ms)
);
CREATE INDEX IF NOT EXISTS idx_clob_snap_market_ts ON clob_snapshots(market_id, ts_ms);

-- Polymarket CLOB market fills (public trade tape)
CREATE TABLE IF NOT EXISTS clob_fills (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    ts_ms           BIGINT NOT NULL,
    received_ms     BIGINT NOT NULL,
    price           DOUBLE PRECISION,
    size            DOUBLE PRECISION,
    side            TEXT,                 -- 'BUY' or 'SELL'
    taker_order_id  TEXT,
    maker_order_id  TEXT,
    UNIQUE(taker_order_id, maker_order_id, ts_ms)
);
CREATE INDEX IF NOT EXISTS idx_clob_fills_market_ts ON clob_fills(market_id, ts_ms);

-- Polymarket markets metadata
CREATE TABLE IF NOT EXISTS markets (
    condition_id        TEXT PRIMARY KEY,
    question            TEXT,
    symbol              TEXT NOT NULL,    -- 'BTC', 'ETH', etc.
    start_price         DOUBLE PRECISION,
    up_token_id         TEXT,
    down_token_id       TEXT,
    start_time          BIGINT,           -- unix ms
    end_time            BIGINT,           -- unix ms
    duration_minutes    INT,
    volume_24h          DOUBLE PRECISION,
    resolution_source   TEXT,             -- 'pyth', 'chainlink', etc.
    oracle_feed_id      TEXT,
    active              BOOLEAN DEFAULT true,
    resolved            BOOLEAN DEFAULT false,
    up_won              BOOLEAN,          -- NULL until resolved
    settlement_price    DOUBLE PRECISION,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_markets_symbol ON markets(symbol);
CREATE INDEX IF NOT EXISTS idx_markets_active ON markets(active, end_time);

-- Our own orders (dry-run and live)
CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    market_id    TEXT NOT NULL,
    token_id     TEXT NOT NULL,
    side         TEXT NOT NULL,           -- 'BUY' or 'SELL'
    token_side   TEXT,                    -- 'UP' or 'DOWN'
    price        DOUBLE PRECISION,
    size         DOUBLE PRECISION,
    status       TEXT DEFAULT 'OPEN',     -- 'OPEN', 'FILLED', 'CANCELLED'
    dry_run      BOOLEAN DEFAULT true,
    ts_ms        BIGINT NOT NULL,
    updated_ms   BIGINT
);
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id, status);

-- Our own fills (subset of orders that were filled)
CREATE TABLE IF NOT EXISTS fills (
    fill_id      TEXT PRIMARY KEY,
    order_id     TEXT REFERENCES orders(order_id),
    market_id    TEXT NOT NULL,
    token_id     TEXT NOT NULL,
    token_side   TEXT,                    -- 'UP' or 'DOWN'
    side         TEXT,                    -- 'BUY' or 'SELL'
    price        DOUBLE PRECISION,
    size         DOUBLE PRECISION,
    fee          DOUBLE PRECISION,        -- gas cost estimate
    ts_ms        BIGINT NOT NULL,
    resolved     BOOLEAN DEFAULT false,
    up_won       BOOLEAN,
    is_adverse   BOOLEAN,
    pnl          DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_fills_market ON fills(market_id);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts_ms);

-- Fair value predictions (for calibration)
CREATE TABLE IF NOT EXISTS fair_value_log (
    id           BIGSERIAL PRIMARY KEY,
    market_id    TEXT NOT NULL,
    ts_ms        BIGINT NOT NULL,
    spot         DOUBLE PRECISION,
    strike       DOUBLE PRECISION,
    vol_annual   DOUBLE PRECISION,
    tte_seconds  DOUBLE PRECISION,
    fair_up      DOUBLE PRECISION,
    fair_down    DOUBLE PRECISION,
    momentum_adj DOUBLE PRECISION,
    resolved     BOOLEAN DEFAULT false,
    actual_up    BOOLEAN                  -- NULL until market resolves
);
CREATE INDEX IF NOT EXISTS idx_fvl_market_ts ON fair_value_log(market_id, ts_ms);

-- PnL log (time series for charting)
CREATE TABLE IF NOT EXISTS pnl_log (
    id           BIGSERIAL PRIMARY KEY,
    ts_ms        BIGINT NOT NULL,
    realized_pnl DOUBLE PRECISION,
    daily_pnl    DOUBLE PRECISION,
    total_fills  INT,
    mode         TEXT
);
CREATE INDEX IF NOT EXISTS idx_pnl_ts ON pnl_log(ts_ms);
