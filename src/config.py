"""
# WHY: ------------------------------------------------------------------------
# This module is the ONE place the program learns any number or any secret.
#
# Two rules from the spec drive its whole design:
#   1. "Never hardcode a number in a module" (spec 0.1.4). Every threshold lives
#      in config/thresholds.yaml and arrives here.
#   2. "Fail loudly on data problems, silently on nothing" (spec 0.1.5). Config
#      is validated AT IMPORT TIME. A missing threshold crashes the program on
#      startup, not at 3am inside a collector, and not by silently screening
#      with a default that someone forgot to set.
#
# Secrets are read from the environment (.env locally, GitHub Actions secrets in
# CI) and are NEVER read from YAML, because YAML is committed and the repo is
# public. Every secret is optional: the system must run with an empty .env,
# logging a WARN and marking the affected block unavailable rather than crashing.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = the parent of the directory holding this file (src/ -> repo root).
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"


# ==============================================================================
# Secrets. All optional by design.
# ==============================================================================
class Secrets(BaseSettings):
    """Secrets from the environment. Every field optional -- see module docstring."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    coingecko_api_key: str | None = None
    coinalyze_api_key: str | None = None
    lunarcrush_api_key: str | None = None
    cryptopanic_auth_token: str | None = None
    turso_database_url: str | None = None
    turso_auth_token: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    healthcheck_daily: str | None = None
    healthcheck_hourly: str | None = None
    healthcheck_journal: str | None = None
    healthcheck_screen: str | None = None
    # Used by the workflows; declared so `doctor` lists them when unset (D-065).
    healthcheck_supply: str | None = None
    healthcheck_site: str | None = None

    @field_validator("*", mode="before")
    @classmethod
    def _empty_string_is_none(cls, v: Any) -> Any:
        # An unset key in .env reads as "" -- treat that as absent, so that
        # `if cfg.secrets.coingecko_api_key:` behaves the way a reader expects.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    def missing(self) -> list[str]:
        """Names of unset secrets. Used for a single startup WARN, never to fail."""
        return [k for k, v in self.model_dump().items() if v is None]


# ==============================================================================
# Thresholds -- config/thresholds.yaml. Validated strictly.
# ==============================================================================
class Layer1Thresholds(BaseModel):
    model_config = {"extra": "forbid"}

    oi_to_mcap_fail: float
    oi_to_mcap_warn: float
    perp_to_spot_vol_fail: float
    perp_to_spot_vol_warn: float
    min_mcap_usd: float
    min_contract_age_days: int
    top10_holder_share_fail: float
    min_circulating_ratio: float
    unlock_lookahead_days: int
    unlock_pct_circulating_fail: float
    unlock_fail_recipient_types: list[str]
    mcap_to_liquidation_ratio_fail: float
    mcap_to_liq_trigger_move_pct: float
    orphan_perp_fails: bool
    require_mcap_data: bool
    min_source_coverage: float
    holder_snapshot_max_age_days: int

    @model_validator(mode="after")
    def _sanity(self) -> "Layer1Thresholds":
        if not 0 < self.top10_holder_share_fail <= 1:
            raise ValueError("top10_holder_share_fail must be a share in (0, 1]")
        if not 0 < self.min_circulating_ratio <= 1:
            raise ValueError("min_circulating_ratio must be a share in (0, 1]")
        if not 0 < self.unlock_pct_circulating_fail <= 1:
            raise ValueError("unlock_pct_circulating_fail must be a share in (0, 1]")
        if self.oi_to_mcap_warn > self.oi_to_mcap_fail:
            raise ValueError("oi_to_mcap_warn must not exceed oi_to_mcap_fail")
        if self.perp_to_spot_vol_warn > self.perp_to_spot_vol_fail:
            raise ValueError("perp_to_spot_vol_warn must not exceed perp_to_spot_vol_fail")
        return self


class Layer2Weights(BaseModel):
    model_config = {"extra": "forbid"}

    fundamental: float
    supply: float
    sector: float
    events: float
    attention: float
    drawdown: float

    @model_validator(mode="after")
    def _all_positive(self) -> "Layer2Weights":
        # Spec acceptance criterion: every weight must be positive.
        for name, value in self.model_dump().items():
            if value <= 0:
                raise ValueError(f"layer2 weight '{name}' must be positive, got {value}")
        return self

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class Layer2Thresholds(BaseModel):
    model_config = {"extra": "forbid"}

    attention_zscore_gate: float
    attention_zscore_window_days: int
    unlock_overhang_cleared_bonus: float
    redundancy_corr_threshold: float
    redundancy_min_days: int
    # Net issuance in the supply block (D-072).
    supply_growth_window_days: int
    supply_smoothing_days: int
    emissions_trajectory_tolerance: float


class Layer3Thresholds(BaseModel):
    model_config = {"extra": "forbid"}

    funding_negative_extreme_pctile: float
    oi_change_extreme_pctile: float
    min_depth_2pct_usd: float
    trendline_min_touches: int
    trendline_tolerance_pct: float
    swing_window_bars: int
    max_bars_since_break: int


class CollectorThresholds(BaseModel):
    model_config = {"extra": "forbid"}

    derivatives_interval_minutes: int
    market_interval_hours: int
    fundamentals_interval_hours: int
    events_interval_hours: int
    attention_interval_hours: int
    news_interval_minutes: int
    max_data_age_hours: float
    consecutive_failures_before_alert: int


class JournalThresholds(BaseModel):
    model_config = {"extra": "forbid"}

    horizons: list[str]
    top_n_to_journal: int
    report_top_n: int

    @model_validator(mode="after")
    def _journal_at_least_reports(self) -> "JournalThresholds":
        if self.top_n_to_journal < self.report_top_n:
            raise ValueError(
                "top_n_to_journal must be >= report_top_n: journal more than you report"
            )
        return self


class BacktestThresholds(BaseModel):
    model_config = {"extra": "forbid"}

    taker_fee_bps: float
    slippage_model: str
    regime_btc_up_pct: float
    regime_btc_down_pct: float
    holdout_fraction: float


class Thresholds(BaseModel):
    model_config = {"extra": "forbid"}

    layer1: Layer1Thresholds
    layer2_weights: Layer2Weights
    layer2: Layer2Thresholds
    layer3: Layer3Thresholds
    collectors: CollectorThresholds
    journal: JournalThresholds
    backtest: BacktestThresholds


# ==============================================================================
# Settings -- config/settings.yaml.
# ==============================================================================
class ProjectSettings(BaseModel):
    name: str
    site_url: str = ""


class DatabaseSettings(BaseModel):
    local_path: str


class UniverseSettings(BaseModel):
    exchanges: list[str]
    quote_asset: str
    coingecko_pages: int
    coingecko_page_pause_seconds: float
    # Which CoinGecko plan COINGECKO_API_KEY belongs to. The two kinds of key look
    # alike and each works only on its own host (D-054).
    coingecko_plan: Literal["demo", "pro"] = "demo"


class HttpSettings(BaseModel):
    timeout_seconds: float
    max_attempts: int
    backoff_initial_seconds: float
    backoff_max_seconds: float
    user_agent: str
    openinterest_concurrency: int


class LoggingSettings(BaseModel):
    level: str
    json_file: str
    console_human_readable: bool


class ReportingSettings(BaseModel):
    output_dir: str
    public_json_dir: str
    history_window_days: int


# Top-holder concentration (L1_HOLDER_CONC). See DECISIONS.md D-035 and D-036.
class HolderSettings(BaseModel):
    model_config = {"extra": "forbid"}

    max_tokens_per_run: int
    max_name_lookups_per_run: int
    # GoPlus returns rate limiting as HTTP 200 with code 4029: how many times to
    # retry, and the first wait in seconds (doubling each retry).
    goplus_rate_limit_retries: int
    goplus_rate_limit_backoff_seconds: float
    # CoinGecko platform key -> GoPlus chain id, for chains with no EVM chain id.
    non_evm_goplus_ids: dict[str, str]
    # CoinGecko platform key -> Blockscout host, for contract-name lookups.
    blockscout_hosts: dict[str, str]
    # Lower-case substrings of a contract's (implementation) name that mark its
    # balance as not-concentration: pools, escrows, stakes, bridges, vesting.
    exclude_contract_name_patterns: list[str]
    excluded_addresses_file: str


# Circulating-supply history for net issuance. See DECISIONS.md D-072.
class SupplyHistorySettings(BaseModel):
    model_config = {"extra": "forbid"}

    max_fetches_per_run: int
    # Re-backfill an asset after this many days, healing any gap in the series.
    refresh_days: int
    history_days: int


class ContractSettings(BaseModel):
    model_config = {"extra": "forbid"}

    max_origin_lookups_per_run: int


# Unlock calendar from DefiLlama's public datasets host. See DECISIONS.md D-037.
class UnlockSettings(BaseModel):
    model_config = {"extra": "forbid"}

    max_protocol_fetches_per_run: int
    # Re-fetch a protocol that maps to a screened asset after this many days.
    refresh_days: int
    # Re-check a protocol that maps to nothing we screen after this many days.
    remap_days: int
    # Past events older than this are not stored; they cannot move L1 or L2.
    history_days: int
    # DefiLlama category -> our recipient_type. null = stored unlabelled.
    category_map: dict[str, str | None]


class Settings(BaseModel):
    model_config = {"extra": "forbid"}

    project: ProjectSettings
    database: DatabaseSettings
    universe: UniverseSettings
    http: HttpSettings
    endpoints: dict[str, str]
    rate_limits: dict[str, int]
    # Sources whose calls are spread evenly (one every 60/rate seconds) instead
    # of allowed to burst up to the per-minute budget at once.
    rate_limit_spacing: list[str] = []
    logging: LoggingSettings
    reporting: ReportingSettings
    holders: HolderSettings
    contracts: ContractSettings
    unlocks: UnlockSettings
    supply_history: SupplyHistorySettings


# ==============================================================================
# Sectors -- config/sectors.yaml.
# ==============================================================================
class SectorMap(BaseModel):
    model_config = {"extra": "forbid"}

    sectors: dict[str, list[str]]
    min_members_for_index: int
    benchmark_asset: str

    _reverse: dict[str, str] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _build_reverse_and_check_dupes(self) -> "SectorMap":
        reverse: dict[str, str] = {}
        for sector, tickers in self.sectors.items():
            for ticker in tickers:
                t = str(ticker).upper()
                if t in reverse:
                    raise ValueError(
                        f"ticker {t} appears in two sectors: {reverse[t]} and {sector}"
                    )
                reverse[t] = sector
        self._reverse = reverse
        return self

    def sector_of(self, base_asset: str) -> str:
        """Sector for an asset, or 'unclassified'.

        'unclassified' is deliberate: an unmapped asset scores None on the sector
        block and its weight is renormalised across the others. Never guess a
        sector just to avoid a null -- a null is honest, a guess is not.
        """
        return self._reverse.get(str(base_asset).upper(), "unclassified")

    def members(self, sector: str) -> list[str]:
        return [t.upper() for t in self.sectors.get(sector, [])]


# ==============================================================================
# Loading
# ==============================================================================
class Config(BaseModel):
    """The fully-validated configuration. Build it once via `get_config()`."""

    model_config = {"arbitrary_types_allowed": True}

    settings: Settings
    thresholds: Thresholds
    sectors: SectorMap
    secrets: Secrets
    repo_root: Path

    def path(self, relative: str) -> Path:
        """Resolve a repo-relative path (every config path is repo-relative)."""
        return self.repo_root / relative


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Required config file missing: {path}. "
            "The system refuses to start with incomplete configuration."
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not parse to a mapping (got {type(data).__name__})")
    return data


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Load and validate all configuration. Cached -- call it freely.

    Raises on first use if anything is missing or invalid. That is intentional:
    a screener running on a half-loaded threshold file is worse than one that
    refuses to run at all.
    """
    settings = Settings(**_read_yaml(CONFIG_DIR / "settings.yaml"))
    thresholds = Thresholds(**_read_yaml(CONFIG_DIR / "thresholds.yaml"))
    sectors = SectorMap(**_read_yaml(CONFIG_DIR / "sectors.yaml"))
    secrets = Secrets()
    return Config(
        settings=settings,
        thresholds=thresholds,
        sectors=sectors,
        secrets=secrets,
        repo_root=REPO_ROOT,
    )


__all__ = ["Config", "get_config", "REPO_ROOT", "CONFIG_DIR"]
