// Mirrors the payloads written by src/report/publish.py. Kept hand-written
// rather than generated: a type that silently widens to `any` when the
// generator fails is worse than one that has to be edited alongside the
// publisher.

/** Non-finite numbers cross the JSON boundary as strings (DECISIONS D-012). */
export type MaybeInfinite = number | "Infinity" | "-Infinity" | null;

export interface Blocks {
  fundamental: number | null;
  supply: number | null;
  /** D-077. Absent (undefined) in files written before gem-v2. */
  momentum?: number | null;
  sector: number | null;
  events: number | null;
  attention: number | null;
  drawdown: number | null;
}

export type BlockKey = keyof Blocks;

export interface Layer3Summary {
  setup_detected: boolean;
  setup_type: string | null;
  invalidation_price: number | null;
  risk_flags: string[];
  depth_2pct_usd: number | null;
  funding_pctile: number | null;
}

export interface RankedRow {
  rank: number;
  asset: string;
  score: number;
  blocks: Blocks;
  flags: string[];
  sector: string;
  file: string;
  layer3: Layer3Summary | null;
  /** D-075: weight measured / weight live, 0-1. Absent before gem-v2. */
  coverage?: number | null;
  /** D-076. Absent before gem-v2. */
  score_version?: string | null;
}

export interface Funnel {
  universe: number;
  disqualified: number;
  survivors: number;
  ranked: number;
  reported: number;
  survival_rate: number;
}

export interface Latest {
  schema_version: number;
  run_date: string;
  generated_at_utc: string;
  funnel: Funnel;
  regime: {
    regime: string | null;
    fear_greed_value: number | null;
    fear_greed_label: string | null;
    btc_return_30d: number | null;
  } | null;
  dark_checks: string[];
  ranked: RankedRow[];
  report_top_n: number;
  /** D-076. Absent before gem-v2. */
  score_version?: string | null;
  /** D-075: the Layer 2 blocks measured for enough survivors to count this
   *  run. A block not listed is dark for everyone. Absent before gem-v2. */
  live_blocks?: string[] | null;
}

export interface CheckValue {
  passed: boolean;
  value: MaybeInfinite;
  value_display: string;
  threshold: number | null;
  reason: string;
  source_unavailable: boolean;
  description: string;
}

export interface RejectedAsset {
  asset: string;
  file: string;
  market_cap_usd: number | null;
  failed_checks: string[];
  checks: Record<string, Partial<CheckValue>>;
}

export interface Rejected {
  schema_version: number;
  run_date: string;
  generated_at_utc?: string;
  total_disqualified: number;
  ordering: string;
  groups: {
    check_id: string;
    count: number;
    description: string;
    assets: RejectedAsset[];
  }[];
}

export interface HorizonStats {
  horizon: string;
  entries_total: number;
  entries_with_returns: number;
  signal: GroupStats;
  control: GroupStats;
  edge_vs_control:
    | { available: false; reason: string }
    | {
        available: true;
        median_difference: number;
        mean_difference: number;
        signal_n: number;
        control_n: number;
      };
}

export interface GroupStats {
  n: number;
  raw?: Moments;
  vs_btc?: Moments;
  hit_rate_vs_btc?: number | null;
  max_adverse?: Moments;
  max_favourable?: Moments;
}

export interface Moments {
  mean: number | null;
  median: number | null;
  stdev?: number | null;
  min?: number | null;
  max?: number | null;
}

export interface JournalEntry {
  entry_id: string;
  run_date: string;
  base_asset: string;
  rank: number;
  total_score: number;
  is_control: boolean;
  price_at_signal: number;
  btc_price_at_signal: number;
  returns: Record<
    string,
    {
      return_raw: number | null;
      return_vs_btc: number | null;
      max_favourable: number | null;
      max_adverse: number | null;
    }
  >;
}

export interface Journal {
  schema_version: number;
  generated_at_utc: string;
  horizons: string[];
  statistics: Record<string, HorizonStats>;
  first_entry_date: string | null;
  coverage_note: string;
  entries: JournalEntry[];
}

export interface EventRow {
  event_id: string;
  base_asset: string;
  event_type: string;
  event_date_utc: string;
  recipient_type: string | null;
  magnitude_tokens: number | null;
  magnitude_usd: number | null;
  pct_of_circulating: number | null;
  description: string | null;
  source: string;
  confidence: string;
  first_seen_utc: string;
  is_survivor: boolean;
}

export interface Events {
  schema_version: number;
  run_date: string;
  generated_at_utc?: string;
  window_days: number;
  events: EventRow[];
  note: string;
}

export interface Health {
  schema_version: number;
  run_date: string;
  generated_at_utc: string;
  collectors: Record<
    string,
    { success: number; partial: number; failed: number; completion_rate: number | null }
  >;
  last_runs: {
    collector_name: string;
    last_run: string;
    status: string;
    rows_written: number | null;
    error_message: string | null;
  }[];
  recent_failures: {
    collector_name: string;
    started_at_utc: string;
    status: string;
    error_message: string | null;
  }[];
  /** `row_count` is a cumulative WRITE tally, not a row census (D-069): an
   *  upsert that overwrites a row increments it again. Rendered as "Writes". */
  tables: { table_name: string; row_count: number; last_write_utc: string | null }[];
  coverage: Record<
    string,
    {
      assets: number;
      rows_present: number;
      of_universe: number | null;
      // The snapshot date these counts describe -- the same
      // latest-at-or-before-today row the screen used. null when the table
      // holds nothing at all, which is a different state from a stale one.
      as_of: string | null;
      age_days: number | null;
    }
  >;
  blocks: Record<
    string,
    {
      weight: number | null;
      scored: number;
      of_ranked: number;
      distinct_values: number;
      informative: boolean;
    }
  >;
  news_lag_seconds: { n: number; p50: number | null; p95: number | null };
  trigger_lag_seconds: { n: number; p50: number | null; p95: number | null };
  trigger_lag_recent: { workflow: string; actual_start_utc: string; lag_seconds: number }[];
  turso_usage: { available: boolean; reason: string };
}

export interface AssetDetail {
  schema_version: number;
  asset: string;
  run_date: string;
  sector: string;
  verdict: { passed: boolean; failed_checks: string[]; dark_checks: string[] };
  checks: Record<string, CheckValue>;
  layer2: {
    total_score: number;
    rank: number | null;
    universe_size: number;
    blocks: Blocks;
    percentiles: Record<string, number | null>;
    /** D-075. Absent before gem-v2. */
    coverage?: number | null;
    /** D-076. Absent before gem-v2. */
    score_version?: string | null;
  } | null;
  layer3: Record<string, unknown> | null;
  market: MarketSnapshot | null;
  prices: { columns: string[]; rows: (string | number | null)[][] };
  events: EventRow[];
  news_context: {
    news_id: string;
    published_at_utc: string;
    title: string;
    url: string | null;
    source_name: string | null;
    sentiment_label: string | null;
    sentiment_score: number | null;
    event_type_guess: string | null;
  }[];
}

/** market_snapshot as the asset file carries it. `ath_date` is a date string. */
export interface MarketSnapshot {
  price_usd: number | null;
  market_cap_usd: number | null;
  fdv_usd: number | null;
  spot_volume_24h_usd: number | null;
  circulating_supply: number | null;
  total_supply: number | null;
  max_supply: number | null;
  ath_usd: number | null;
  ath_date: string | null;
  pct_below_ath: number | null;
  price_change_24h_pct: number | null;
}

/** manifest.json. `asset_files` is the only way from a ticker to its file:
 *  sanitising a ticker is not reversible (DECISIONS D-012). */
export interface Manifest {
  schema_version: number;
  run_date: string;
  generated_at_utc: string;
  assets: number;
  asset_files: Record<string, string>;
  files: { name: string; bytes: number }[];
  total_bytes: number;
}

export interface History {
  schema_version: number;
  generated_at_utc?: string;
  window_days: number;
  days: {
    run_date: string;
    universe: number;
    survivors: number;
    survival_rate: number | null;
  }[];
}

// -- pulse.json -------------------------------------------------------------------
// Mirrors PULSE_JSON_EXAMPLE in src/pulse/contract.py (schema_version 1). The
// hourly clock: baked by build-site from pulse_result, never committed.

/** F5/F6 structure states (contract.STATES). */
export type PulseState = "bull_break" | "bull_trend" | "neutral" | "bear_trend" | "bear_break";

/** F4 OI x price x flow quadrant (contract.OI_QUADRANTS). A conditioner, never a
 *  signal on its own (D-074). */
export type OiQuadrant =
  | "confirm_long"
  | "short_covering"
  | "new_shorts"
  | "long_liquidation"
  | "leverage_build"
  | "neutral";

export interface PulseAsset {
  asset: string;
  score: number | null;
  rank: number | null;
  /** Rank at the previous scored hour, or null. */
  prev_rank: number | null;
  /** Score change vs the previous hour, or null. */
  delta: number | null;
  gem_rank: number | null;
  aligned: boolean;
  /** Typed wide so an unknown future value renders as text, not a crash. */
  state_4h: PulseState | string | null;
  bars_since_4h?: number | null;
  state_1h: PulseState | string | null;
  oi_quadrant: OiQuadrant | string | null;
  /** RISK_FLAGS: crowding, leverage_no_move. */
  flags: string[];
  /** Weight measured / weight live, 0-1 (D-075). */
  coverage: number | null;
  components?: Record<string, number | null>;
  features?: Record<string, number | null>;
  /** Last 48 closed 1H closes. */
  spark_1h?: (number | null)[];
  /** Per-bar taker flow in [-1, 1], same 48 bars as spark_1h. */
  flow_1h?: (number | null)[];
  /** Last 42 closed 4H closes (7 days). */
  spark_4h?: (number | null)[];
  file?: string | null;
}

export interface PulseRiser {
  asset: string;
  delta: number;
  rank: number | null;
  prev_rank: number | null;
}

export interface Pulse {
  schema_version: number;
  /** "unavailable" when no pulse_result was written in the last 3h. */
  status: "ok" | "unavailable" | string;
  as_of_utc: string | null;
  generated_at_utc: string | null;
  score_version: string | null;
  universe_size: number | null;
  scored: number | null;
  aligned: string[];
  risers: PulseRiser[];
  assets: PulseAsset[];
  /** EXCLUDING_FLAGS: thin_book, insufficient_history. Never hidden. */
  excluded: { asset: string; reason: string }[];
}
