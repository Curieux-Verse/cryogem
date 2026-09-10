// Mirrors the payloads written by src/report/publish.py. Kept hand-written
// rather than generated: a type that silently widens to `any` when the
// generator fails is worse than one that has to be edited alongside the
// publisher.

/** Non-finite numbers cross the JSON boundary as strings (DECISIONS D-012). */
export type MaybeInfinite = number | "Infinity" | "-Infinity" | null;

export interface Blocks {
  fundamental: number | null;
  supply: number | null;
  sector: number | null;
  events: number | null;
  attention: number | null;
  drawdown: number | null;
}

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
  tables: { table_name: string; row_count: number; last_write_utc: string | null }[];
  coverage: Record<
    string,
    { assets: number; rows_present: number; of_universe: number | null }
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
  } | null;
  layer3: Record<string, unknown> | null;
  market: Record<string, number | null> | null;
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

export interface History {
  schema_version: number;
  window_days: number;
  days: {
    run_date: string;
    universe: number;
    survivors: number;
    survival_rate: number | null;
  }[];
}
