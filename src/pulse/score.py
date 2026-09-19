"""
# WHY: ------------------------------------------------------------------------
# Feature frame -> Pulse score, 0-100, one row per survivor (PULSE_COLUMNS).
#
# CROSS-SECTIONAL, WITHIN THE HOUR. Every percentile is taken over this hour's
# SCORED set only (survivors not flagged thin_book / insufficient_history). An
# excluded asset never enters the ranking pool, so a dead perp cannot make a
# live one look strong. Same function as Layer 2 (cross_sectional_percentile).
#
# MISSING SCORES NOTHING (D-075). Unlike Layer 2's renormalisation, a live
# component an asset lacks contributes 0, not a redistribution of its weight;
# the same inside a component. A component (or sub-metric) is LIVE when it is
# measured for >= live_min_assets scored assets; one dark for (nearly)
# everyone drops out for everyone, so an outage of one source does not zero
# the whole universe. `coverage` = weight measured / weight live.
#
# D-074. OI enters only through oi_confirm (OI_QUADRANT_SCORE of the 24H
# quadrant: above neutral only for price up AND flow buying AND OI up) and the
# R1/R2 penalty multipliers. Funding enters only R1. No path lets OI alone, or
# funding at all, raise a score.
#
# Sub-weights (D-079): flow = flow_24h 1 + flow_4h flow_4h_relative_weight;
# momentum = vamom_24h 1 + vamom_7d 1; thrust = thrust_4h 1 + thrust_1h 0.5
# (the 1H spike is the noisier reading of the same thing); oi_confirm = the 24H
# quadrant only (the 4H quadrant is published as a feature).
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import PulseThresholds, get_config
from src.pulse import contract as C
from src.screening.layer2_score import cross_sectional_percentile

#: Within the thrust component: weight of the 1H thrust relative to the 4H.
THRUST_1H_RELATIVE_WEIGHT = 0.5

COMPONENTS: tuple[str, ...] = (
    "flow", "structure_4h", "momentum", "thrust", "oi_confirm", "structure_1h",
)


def _sub_metrics(scored: pd.DataFrame, cfg: PulseThresholds) -> dict[str, dict[str, tuple[pd.Series, float]]]:
    """component -> {sub-metric -> (0-100 series over the scored set, sub-weight)}."""

    def pct(col: str) -> pd.Series:
        return cross_sectional_percentile(scored[col])

    def mapped(col: str, table: dict[str, float]) -> pd.Series:
        return scored[col].map(lambda v: table.get(v, np.nan) if isinstance(v, str) else np.nan).astype(float)

    return {
        "flow": {"flow_24h": (pct("flow_24h"), 1.0),
                 "flow_4h": (pct("flow_4h"), cfg.flow_4h_relative_weight)},
        "momentum": {"vamom_24h": (pct("vamom_24h"), 1.0),
                     "vamom_7d": (pct("vamom_7d"), 1.0)},
        "thrust": {"thrust_4h": (pct("thrust_4h"), 1.0),
                   "thrust_1h": (pct("thrust_1h"), THRUST_1H_RELATIVE_WEIGHT)},
        "structure_4h": {"state_4h": (mapped("state_4h", C.STATE_SCORE), 1.0)},
        "structure_1h": {"state_1h": (mapped("state_1h", C.STATE_SCORE), 1.0)},
        "oi_confirm": {"oi_quadrant_24h": (mapped("oi_quadrant_24h", C.OI_QUADRANT_SCORE), 1.0)},
    }


def score_pulse(
    features: pd.DataFrame, gem_ranks: dict[str, int], cfg: PulseThresholds | None = None
) -> pd.DataFrame:
    """Score, rank and flag every asset in `features` (index base_asset).

    Excluded assets (EXCLUDING_FLAGS) keep a row with score and rank NaN. If no
    component is live (fewer than live_min_assets measured on every one), no
    asset has a score: nothing was measured widely enough to rank on.
    """
    cfg = cfg if cfg is not None else get_config().thresholds.pulse
    missing = [c for c in C.FEATURE_COLUMNS if c not in features.columns]
    if missing:
        raise ValueError(f"feature frame lacks columns {missing}")
    weights = cfg.weights.as_dict()
    gem_ranks = gem_ranks or {}

    flags = features["flags"].map(lambda v: list(v) if isinstance(v, (list, tuple)) else [])
    excluded = flags.map(lambda fl: any(f in C.EXCLUDING_FLAGS for f in fl))
    scored = features.loc[~excluded]
    idx = scored.index

    comp_value: dict[str, pd.Series] = {}
    comp_cov: dict[str, pd.Series] = {}
    live: dict[str, bool] = {}
    for comp, subs in _sub_metrics(scored, cfg).items():
        live_subs = {k: (s, w) for k, (s, w) in subs.items()
                     if int(s.notna().sum()) >= cfg.live_min_assets}
        live[comp] = bool(live_subs)
        if not live_subs:
            comp_value[comp] = pd.Series(np.nan, index=idx)
            comp_cov[comp] = pd.Series(0.0, index=idx)
            continue
        total_w = sum(w for _, w in live_subs.values())
        value_sum = sum(s.fillna(0.0) * w for s, w in live_subs.values())
        measured_w = sum(s.notna().astype(float) * w for s, w in live_subs.values())
        comp_value[comp] = (value_sum / total_w).where(measured_w > 0)
        comp_cov[comp] = measured_w / total_w

    live_weight = sum(weights[c] for c in COMPONENTS if live[c])
    if live_weight > 0:
        composite = sum(comp_value[c].fillna(0.0) * weights[c] for c in COMPONENTS if live[c]) / live_weight
        coverage = sum(comp_cov[c] * weights[c] for c in COMPONENTS if live[c]) / live_weight
    else:
        composite = pd.Series(np.nan, index=idx)
        coverage = pd.Series(0.0, index=idx)

    penalty = pd.Series(1.0, index=features.index)
    for asset, fl in flags.items():
        if C.FLAG_CROWDING in fl:
            penalty[asset] *= cfg.crowding_multiplier
        if C.FLAG_LEVERAGE_NO_MOVE in fl:
            penalty[asset] *= cfg.leverage_no_move_multiplier
    score = (composite * penalty.reindex(idx)).clip(0.0, 100.0)

    # Rank 1 = best; ties broken by asset name so the order is deterministic.
    order = sorted((a for a in idx if not pd.isna(score[a])), key=lambda a: (-score[a], a))
    rank = {a: float(i + 1) for i, a in enumerate(order)}

    rows = []
    for asset in features.index:
        fl = flags[asset]
        is_scored = asset in rank
        gem = gem_ranks.get(asset)
        gem = None if gem is None or pd.isna(gem) else int(gem)
        components = {
            c: (float(comp_value[c][asset])
                if not excluded[asset] and live[c] and not pd.isna(comp_value[c][asset]) else None)
            for c in COMPONENTS
        }
        s = float(score[asset]) if is_scored else np.nan
        state_4h = features.at[asset, "state_4h"]
        aligned = bool(
            is_scored
            and gem is not None
            and gem <= cfg.aligned_gem_rank_max
            and s >= cfg.aligned_min_score
            and state_4h in cfg.aligned_states
            and not any(f in C.RISK_FLAGS for f in fl)
        )
        quad = features.at[asset, "oi_quadrant_24h"]
        rows.append({
            "score": s,
            "rank": rank.get(asset, np.nan),
            "components": components,
            "coverage": float(coverage[asset]) if not excluded[asset] else np.nan,
            "state_4h": state_4h if isinstance(state_4h, str) else None,
            "state_1h": features.at[asset, "state_1h"] if isinstance(features.at[asset, "state_1h"], str) else None,
            "oi_quadrant": quad if isinstance(quad, str) else None,
            "flags": fl,
            "penalty": float(penalty[asset]) if not excluded[asset] else 1.0,
            "gem_rank": gem,
            "aligned": aligned,
        })
    out = pd.DataFrame(rows, index=features.index, columns=list(C.PULSE_COLUMNS))
    out.index.name = "base_asset"
    return out


__all__ = ["COMPONENTS", "THRUST_1H_RELATIVE_WEIGHT", "score_pulse"]
