-- =============================================================================
-- WHY: the full data model. Applied idempotently on every startup.
--
-- Three properties matter more than anything else here:
--   1. fetched_at_utc on EVERY table. This is what makes point-in-time
--      backtesting possible. There is no way to add it retroactively.
--   2. Natural primary keys (date + asset), so a re-run of a collector
--      overwrites its own row instead of duplicating it. Collectors are
--      idempotent because the schema makes them idempotent.
--   3. journal_entry and forward_return are APPEND-ONLY, enforced by triggers
--      at the bottom of this file -- not by convention. Deleting a journal row
--      is the exact mechanism by which every signal channel looks profitable.
--
-- Compatibility note: this DDL must run on BOTH sqlite3 (local dev) and libSQL
-- (Turso, in CI). Do not use any SQLite feature libSQL lacks.
-- =============================================================================

PRAGMA foreign_keys=ON;

-- =============================================================================
-- UNIVERSE: point-in-time symbol list. Critical for backtesting.
-- One row per (symbol, day). Never delete rows: delisted symbols vanish from
-- exchangeInfo, and they are exactly the ones the screener would have flagged.
-- =============================================================================
CREATE TABLE IF NOT EXISTS universe_snapshot (
    snapshot_date   TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    base_asset      TEXT NOT NULL,
    quote_asset     TEXT NOT NULL,
    contract_type   TEXT,
    onboard_date    TEXT,
    status          TEXT,
    price_multiplier INTEGER DEFAULT 1,
    funding_interval_hours REAL,
    fetched_at_utc  TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, exchange, symbol)
);
CREATE INDEX IF NOT EXISTS idx_universe_asset_date
    ON universe_snapshot(base_asset, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_universe_date
    ON universe_snapshot(snapshot_date);

-- =============================================================================
-- DERIVATIVES: high-frequency snapshots (5 min on a host, hourly on Actions).
-- =============================================================================
CREATE TABLE IF NOT EXISTS derivatives_snapshot (
    ts_utc              TEXT NOT NULL,
    exchange            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    base_asset          TEXT,
    mark_price          REAL,
    index_price         REAL,
    open_interest_base  REAL,
    open_interest_usd   REAL,
    funding_rate        REAL,
    funding_interval_hours REAL,
    funding_apr         REAL,
    next_funding_ts     TEXT,
    premium             REAL,
    volume_24h_usd      REAL,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (ts_utc, exchange, symbol)
);
CREATE INDEX IF NOT EXISTS idx_deriv_symbol_ts ON derivatives_snapshot(symbol, ts_utc);
CREATE INDEX IF NOT EXISTS idx_deriv_asset_ts  ON derivatives_snapshot(base_asset, ts_utc);

-- =============================================================================
-- SPOT / MARKET: daily
-- =============================================================================
CREATE TABLE IF NOT EXISTS market_snapshot (
    snapshot_date       TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    coingecko_id        TEXT,
    price_usd           REAL,
    market_cap_usd      REAL,
    fdv_usd             REAL,
    circulating_supply  REAL,
    total_supply        REAL,
    max_supply          REAL,
    spot_volume_24h_usd REAL,
    ath_usd             REAL,
    ath_date            TEXT,
    pct_below_ath       REAL,
    price_change_24h_pct REAL,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, base_asset)
);
CREATE INDEX IF NOT EXISTS idx_market_asset_date ON market_snapshot(base_asset, snapshot_date);

-- =============================================================================
-- ORDER BOOK DEPTH: your real exit size, not the printed price.
-- =============================================================================
CREATE TABLE IF NOT EXISTS depth_snapshot (
    ts_utc          TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    market_type     TEXT NOT NULL,
    bid_depth_0p5   REAL,
    bid_depth_1p0   REAL,
    bid_depth_2p0   REAL,
    ask_depth_0p5   REAL,
    ask_depth_1p0   REAL,
    ask_depth_2p0   REAL,
    fetched_at_utc  TEXT NOT NULL,
    PRIMARY KEY (ts_utc, exchange, symbol, market_type)
);
CREATE INDEX IF NOT EXISTS idx_depth_symbol_ts ON depth_snapshot(symbol, ts_utc);

-- =============================================================================
-- FUNDAMENTALS: daily, from DefiLlama / Artemis
-- =============================================================================
CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
    snapshot_date       TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    protocol_slug       TEXT,
    tvl_usd             REAL,
    fees_24h_usd        REAL,
    fees_7d_usd         REAL,
    fees_30d_usd        REAL,
    revenue_24h_usd     REAL,
    revenue_7d_usd      REAL,
    revenue_30d_usd     REAL,
    revenue_prev_30d_usd REAL,
    revenue_annualised  REAL,
    active_addresses_24h INTEGER,
    has_fundamentals    INTEGER NOT NULL,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, base_asset)
);
CREATE INDEX IF NOT EXISTS idx_fund_asset_date
    ON fundamentals_snapshot(base_asset, snapshot_date);

-- =============================================================================
-- SUPPLY / HOLDERS
-- =============================================================================
CREATE TABLE IF NOT EXISTS holder_snapshot (
    snapshot_date       TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    chain               TEXT,
    contract_address    TEXT,
    top10_share         REAL,
    top50_share         REAL,
    holder_count        INTEGER,
    excluded_addresses  TEXT,
    data_quality        TEXT,
    -- measured | native_coin | unsupported_chain | fetch_failed. native_coin
    -- is NOT a gap: a chain's own coin has no token contract to read holders
    -- from, so the check does not apply. See DECISIONS.md D-035.
    applicability       TEXT,
    top10_share_raw     REAL,               -- before exclusions, kept for audit
    top1_share          REAL,               -- single-whale risk, after exclusions
    excluded_share      REAL,               -- share of supply removed by exclusions
    holders_json        TEXT,               -- top holders with name + exclusion reason
    source              TEXT,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, base_asset)
);
CREATE INDEX IF NOT EXISTS idx_holder_asset_date
    ON holder_snapshot(base_asset, snapshot_date);

-- =============================================================================
-- SUPPLY METRICS: emissions + burns, feeding the L2 supply block.
-- =============================================================================
CREATE TABLE IF NOT EXISTS supply_metrics (
    snapshot_date       TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    emissions_annual    REAL,
    emissions_prev_annual REAL,
    emissions_trajectory TEXT,
    cumulative_burned   REAL,
    burned_pct_of_total REAL,
    staked_ratio        REAL,
    staked_is_team_controlled INTEGER,
    source              TEXT,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, base_asset)
);
CREATE INDEX IF NOT EXISTS idx_supply_asset_date
    ON supply_metrics(base_asset, snapshot_date);

-- =============================================================================
-- LIQUIDATIONS. NOTE: all totals are FLOORS, not measurements. Feeds are
-- throttled at source to roughly one print per second per symbol, so a high
-- ratio computed from these is high-confidence and a low one is not.
-- =============================================================================
CREATE TABLE IF NOT EXISTS liquidation_snapshot (
    snapshot_date       TEXT NOT NULL,
    exchange            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    base_asset          TEXT,
    liq_long_usd_24h    REAL,
    liq_short_usd_24h   REAL,
    liq_total_usd_24h   REAL,
    is_floor            INTEGER NOT NULL DEFAULT 1,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, exchange, symbol)
);
CREATE INDEX IF NOT EXISTS idx_liq_asset_date
    ON liquidation_snapshot(base_asset, snapshot_date);

-- =============================================================================
-- EVENT CALENDAR (Tier-1 news). The highest-value table here.
-- =============================================================================
CREATE TABLE IF NOT EXISTS scheduled_event (
    event_id            TEXT PRIMARY KEY,
    base_asset          TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    event_date_utc      TEXT NOT NULL,
    recipient_type      TEXT,
    magnitude_tokens    REAL,
    magnitude_usd       REAL,
    pct_of_circulating  REAL,
    description         TEXT,
    source              TEXT NOT NULL,
    confidence          TEXT NOT NULL,
    first_seen_utc      TEXT NOT NULL,
    fetched_at_utc      TEXT NOT NULL,
    recipient_category  TEXT,               -- the source's raw label, e.g. DefiLlama 'insiders'
    recipient_label     TEXT,               -- e.g. 'Core Contributors'
    source_ref          TEXT,               -- the source's own id, e.g. a DefiLlama slug
    -- Set when a later refresh no longer lists a FUTURE event. Never deleted:
    -- a backtest on a date before this must still see what was then known.
    retracted_utc       TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_asset_date
    ON scheduled_event(base_asset, event_date_utc);
CREATE INDEX IF NOT EXISTS idx_event_first_seen
    ON scheduled_event(first_seen_utc);

-- event_type enum:
--   unlock_cliff, unlock_linear, emissions_change, burn, mainnet, upgrade,
--   governance_vote, listing, delisting, monitoring_tag_add,
--   monitoring_tag_remove, conference, earnings_report

-- =============================================================================
-- ASSET CONTRACTS: which token contract, on which chain, holder data is read
-- from -- or why there is none. Resolved from CoinGecko platforms, cached.
-- =============================================================================
CREATE TABLE IF NOT EXISTS asset_contract (
    coingecko_id        TEXT PRIMARY KEY,
    -- measurable | native_coin | unsupported_chain
    applicability       TEXT NOT NULL,
    platform            TEXT,               -- CoinGecko platform key of the chosen contract
    goplus_chain        TEXT,               -- GoPlus chain id, or 'solana' / 'tron'
    contract_address    TEXT,
    -- single_platform | asset_platform_id | coins_list_first_platform |
    -- native_coin_id | no_platforms | no_supported_platform
    resolved_via        TEXT NOT NULL,
    origin_platform     TEXT,               -- CoinGecko asset_platform_id, once confirmed
    platforms_json      TEXT,               -- every platform -> address, for reverse lookup
    fetched_at_utc      TEXT NOT NULL
);

-- Contract names from a block explorer. Names do not change, so this is a
-- cache that saves re-asking for the same pool or escrow every refresh.
CREATE TABLE IF NOT EXISTS address_label (
    chain               TEXT NOT NULL,
    address             TEXT NOT NULL,
    name                TEXT,
    implementation_name TEXT,
    is_contract         INTEGER,
    source              TEXT NOT NULL,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (chain, address)
);

-- DefiLlama emissions protocols and the CoinGecko id each one maps to.
CREATE TABLE IF NOT EXISTS emission_protocol (
    slug                TEXT PRIMARY KEY,
    gecko_id            TEXT,
    name                TEXT,
    token               TEXT,
    unlock_event_count  INTEGER,
    last_fetched_utc    TEXT NOT NULL,
    fetched_at_utc      TEXT NOT NULL
);

-- =============================================================================
-- ATTENTION (Tier-2 news)
-- =============================================================================
CREATE TABLE IF NOT EXISTS attention_snapshot (
    snapshot_date       TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    social_volume       REAL,
    social_volume_z     REAL,
    social_dominance    REAL,
    social_engagement   REAL,
    sentiment_score     REAL,
    galaxy_score        REAL,
    alt_rank            INTEGER,
    google_trends       REAL,
    source              TEXT NOT NULL,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, base_asset, source)
);
CREATE INDEX IF NOT EXISTS idx_attention_asset_date
    ON attention_snapshot(base_asset, snapshot_date);

CREATE TABLE IF NOT EXISTS market_regime (
    snapshot_date       TEXT PRIMARY KEY,
    fear_greed_value    INTEGER,
    fear_greed_label    TEXT,
    btc_return_30d      REAL,
    regime              TEXT,
    fetched_at_utc      TEXT NOT NULL
);

-- =============================================================================
-- NEWS (Tier-3). Research/labelling only. NOT a trigger source.
-- Nothing in this table may reach layer1_kill.py or layer2_score.py.
-- =============================================================================
CREATE TABLE IF NOT EXISTS news_item (
    news_id             TEXT PRIMARY KEY,
    published_at_utc    TEXT NOT NULL,
    fetched_at_utc      TEXT NOT NULL,
    lag_seconds         REAL,
    title               TEXT NOT NULL,
    url                 TEXT,
    source_name         TEXT,
    assets              TEXT,
    sentiment_label     TEXT,
    sentiment_score     REAL,
    event_type_guess    TEXT,
    raw                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_item(published_at_utc);

-- =============================================================================
-- SCREENING OUTPUT
-- =============================================================================
CREATE TABLE IF NOT EXISTS layer1_result (
    run_date        TEXT NOT NULL,
    base_asset      TEXT NOT NULL,
    passed          INTEGER NOT NULL,
    failed_checks   TEXT,
    check_values    TEXT,
    fetched_at_utc  TEXT NOT NULL,
    PRIMARY KEY (run_date, base_asset)
);
CREATE INDEX IF NOT EXISTS idx_l1_run_passed ON layer1_result(run_date, passed);
CREATE INDEX IF NOT EXISTS idx_l1_asset      ON layer1_result(base_asset, run_date);

CREATE TABLE IF NOT EXISTS layer2_result (
    run_date            TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    total_score         REAL NOT NULL,
    score_fundamental   REAL,
    score_supply        REAL,
    score_sector        REAL,
    score_drawdown      REAL,
    score_events        REAL,
    score_attention     REAL,
    rank                INTEGER,
    universe_size       INTEGER NOT NULL,
    percentiles         TEXT,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (run_date, base_asset)
);
CREATE INDEX IF NOT EXISTS idx_l2_run_rank ON layer2_result(run_date, rank);
CREATE INDEX IF NOT EXISTS idx_l2_asset    ON layer2_result(base_asset, run_date);

CREATE TABLE IF NOT EXISTS layer3_result (
    run_date            TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    setup_detected      INTEGER NOT NULL DEFAULT 0,
    setup_type          TEXT,
    invalidation_price  REAL,
    trendline_slope     REAL,
    trendline_touches   INTEGER,
    break_confirmed     INTEGER,
    retest_confirmed    INTEGER,
    fvg_count           INTEGER,
    order_block_count   INTEGER,
    risk_flags          TEXT,
    depth_2pct_usd      REAL,
    funding_pctile      REAL,
    oi_change_pctile    REAL,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (run_date, base_asset)
);

-- =============================================================================
-- JOURNAL: the only source of truth. APPEND-ONLY. Enforced by trigger below.
-- =============================================================================
CREATE TABLE IF NOT EXISTS journal_entry (
    entry_id            TEXT PRIMARY KEY,
    run_date            TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    rank                INTEGER NOT NULL,
    total_score         REAL NOT NULL,
    price_at_signal     REAL NOT NULL,
    btc_price_at_signal REAL NOT NULL,
    is_control          INTEGER NOT NULL DEFAULT 0,
    layer1_values       TEXT,
    layer2_values       TEXT,
    layer3_values       TEXT,
    news_context        TEXT,
    events_context      TEXT,
    created_at_utc      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_run     ON journal_entry(run_date);
CREATE INDEX IF NOT EXISTS idx_journal_asset   ON journal_entry(base_asset, run_date);
CREATE INDEX IF NOT EXISTS idx_journal_control ON journal_entry(is_control, run_date);

CREATE TABLE IF NOT EXISTS forward_return (
    entry_id        TEXT NOT NULL,
    horizon         TEXT NOT NULL,
    entry_price     REAL,
    price_source    TEXT,
    price_at_horizon REAL,
    return_raw      REAL,
    return_vs_btc   REAL,
    max_favourable  REAL,
    max_adverse     REAL,
    computed_at_utc TEXT NOT NULL,
    PRIMARY KEY (entry_id, horizon),
    FOREIGN KEY (entry_id) REFERENCES journal_entry(entry_id)
);
CREATE INDEX IF NOT EXISTS idx_fwd_horizon ON forward_return(horizon);

-- =============================================================================
-- PRICE HISTORY: daily closes, needed to compute forward returns at horizons.
-- =============================================================================
CREATE TABLE IF NOT EXISTS price_daily (
    snapshot_date   TEXT NOT NULL,
    base_asset      TEXT NOT NULL,
    open_usd        REAL,
    high_usd        REAL,
    low_usd         REAL,
    close_usd       REAL NOT NULL,
    volume_usd      REAL,
    source          TEXT NOT NULL,
    fetched_at_utc  TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, base_asset)
);
CREATE INDEX IF NOT EXISTS idx_price_asset_date ON price_daily(base_asset, snapshot_date);

-- =============================================================================
-- OPS
-- =============================================================================
CREATE TABLE IF NOT EXISTS collector_run (
    run_id          TEXT PRIMARY KEY,
    collector_name  TEXT NOT NULL,
    started_at_utc  TEXT NOT NULL,
    ended_at_utc    TEXT,
    status          TEXT NOT NULL,
    rows_written    INTEGER,
    error_message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_name_started ON collector_run(collector_name, started_at_utc);

-- Maintained counts. Turso meters ROW READS: never SELECT COUNT(*) over a
-- time-series table to answer "how many rows do we have?". Read this instead.
CREATE TABLE IF NOT EXISTS table_stats (
    table_name      TEXT PRIMARY KEY,
    row_count       INTEGER NOT NULL DEFAULT 0,
    last_write_utc  TEXT
);

-- Trigger-lag record: intended fire time vs the workflow real start.
-- Surfaced on the Health page. If this reads in tens of minutes, an
-- on-schedule trigger is still live somewhere in the repo.
-- BACKTEST RUNS: the holdout audit log.
-- "Test once on the final third" is a promise no code can keep by asking
-- politely, so every holdout run is recorded here and the harness reports the
-- count back. A second run is not blocked -- there are legitimate reasons to
-- re-run after a bug fix -- but it is labelled, so a fourth-attempt result can
-- never be presented as an out-of-sample one.
--
-- run_id is a RANDOM uuid, not a deterministic hash. The first version keyed
-- it on (window, timestamp-to-the-second): two runs inside the same second
-- produced the same id, the upsert replaced the row, and the counter stayed at
-- one. An audit log whose rows can overwrite each other is not an audit log.
CREATE TABLE IF NOT EXISTS backtest_run (
    run_id          TEXT PRIMARY KEY,
    split           TEXT NOT NULL,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    horizon         TEXT NOT NULL,
    trades          INTEGER,
    median_vs_btc   REAL,
    mean_vs_btc     REAL,
    cost_drag       REAL,
    ran_at_utc      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_backtest_run_split ON backtest_run(split, ran_at_utc);

CREATE TABLE IF NOT EXISTS trigger_lag (
    run_id              TEXT PRIMARY KEY,
    workflow            TEXT NOT NULL,
    scheduled_at_utc    TEXT,
    actual_start_utc    TEXT NOT NULL,
    lag_seconds         REAL,
    fetched_at_utc      TEXT NOT NULL
);

-- =============================================================================
-- APPEND-ONLY ENFORCEMENT (spec 11 acceptance criterion, and guardrail 18.5).
-- The journal is the only component that produces truth. A deleted row is how
-- every signal channel comes to look profitable. Enforced here, in the database,
-- rather than left to the discipline of whoever writes the next query.
-- =============================================================================
CREATE TRIGGER IF NOT EXISTS journal_entry_no_delete
BEFORE DELETE ON journal_entry
BEGIN
    SELECT RAISE(ABORT, 'journal_entry is append-only: deletion is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS journal_entry_no_update
BEFORE UPDATE ON journal_entry
BEGIN
    SELECT RAISE(ABORT, 'journal_entry is append-only: mutation is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS forward_return_no_delete
BEFORE DELETE ON forward_return
BEGIN
    SELECT RAISE(ABORT, 'forward_return is append-only: deletion is forbidden');
END;

-- =============================================================================
-- SPOT MARKETS on the same venue as the perp. Kept separate from
-- market_snapshot (CoinGecko) because L1 check 4 compares perp volume against
-- SPOT VOLUME ON THE SAME EXCHANGE, and mixing sources there would compare a
-- Binance perp against global spot volume -- a different, much weaker test.
-- Absence of a row here is itself the signal: an orphan perp.
-- =============================================================================
CREATE TABLE IF NOT EXISTS spot_snapshot (
    snapshot_date       TEXT NOT NULL,
    exchange            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    base_asset          TEXT NOT NULL,
    price_usd           REAL,
    volume_24h_usd      REAL,
    price_change_24h_pct REAL,
    fetched_at_utc      TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, exchange, symbol)
);
CREATE INDEX IF NOT EXISTS idx_spot_asset_date
    ON spot_snapshot(base_asset, snapshot_date);
