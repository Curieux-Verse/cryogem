"""
Pulse signals: resample_4h, compute_features (F1-F6, R1-R3), structure_state,
score_pulse (D-074, D-075, D-079). Synthetic, deterministic, no network.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.config import get_config
from src.pulse import contract as C
from src.pulse.features import compute_features, oi_quadrant, resample_4h
from src.pulse.score import COMPONENTS, score_pulse
from src.pulse.structure import min_structure_bars, structure_state

AS_OF = pd.Timestamp("2026-09-19 14:00", tz="UTC")
H = pd.Timedelta(hours=1)


@pytest.fixture(scope="module")
def cfg():
    return get_config().thresholds.pulse


# ==============================================================================
# Builders
# ==============================================================================
def bars_from_close(close, qv=None, buy_share=0.5, end=AS_OF - H) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    n = len(close)
    idx = pd.date_range(end=end, periods=n, freq="h")
    opn = np.r_[close[0], close[:-1]]
    qv = np.full(n, 2e6) if qv is None else np.asarray(qv, dtype=float)
    share = np.broadcast_to(np.asarray(buy_share, dtype=float), (n,))
    return pd.DataFrame(
        {
            "open": opn,
            "high": np.maximum(opn, close) * 1.001,
            "low": np.minimum(opn, close) * 0.999,
            "close": close,
            "quote_volume": qv,
            "taker_buy_quote": qv * share,
            "trades": np.full(n, 100.0),
        },
        index=idx,
    )


def random_bars(drift=0.0, buy_share=0.5, n=400, seed=0, noise=0.004):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(drift + noise * rng.standard_normal(n)))
    qv = 2e6 * (1 + 0.3 * rng.random(n))
    return bars_from_close(close, qv=qv, buy_share=buy_share)


def oi_frame(values, end=AS_OF) -> pd.DataFrame:
    values = np.asarray(values, dtype=float)
    idx = pd.date_range(end=end, periods=len(values), freq="h")
    return pd.DataFrame({"oi_contracts": values, "oi_usd": values * 100}, index=idx)


def oi_rising_at_end(n=400, rise=0.10, seed=0) -> pd.DataFrame:
    """Flat-ish OI history, then +rise over the last 24 hours."""
    rng = np.random.default_rng(seed)
    v = 1e6 * (1 + 0.001 * rng.standard_normal(n))
    v[-24:] = v[-25] * np.linspace(1 + rise / 24, 1 + rise, 24)
    return oi_frame(v)


def window(bars: dict, oi=None, funding=None, survivors=None) -> C.MarketWindow:
    return C.MarketWindow(
        as_of=AS_OF.to_pydatetime(),
        bars=bars,
        oi=oi or {},
        funding=funding or {},
        survivors=list(survivors if survivors is not None else bars),
    )


def feature_row(**overrides) -> dict:
    row = {c: np.nan for c in C.FEATURE_COLUMNS}
    row.update(
        n_bars_1h=400.0, last_close=1.0, quote_volume_24h=5e7,
        ret_1h=0.0, ret_4h=0.0, ret_24h=0.0, ret_7d=0.0,
        flow_4h=0.0, flow_24h=0.0, thrust_1h=0.0, thrust_4h=0.0,
        vamom_24h=0.0, vamom_7d=0.0,
        oi_quadrant_4h=C.OI_NEUTRAL, oi_quadrant_24h=C.OI_NEUTRAL,
        state_4h=C.NEUTRAL, state_1h=C.NEUTRAL, flags=[],
    )
    row.update(overrides)
    return row


def feature_frame(rows: dict[str, dict]) -> pd.DataFrame:
    df = pd.DataFrame(list(rows.values()), index=pd.Index(list(rows), name="base_asset"),
                      columns=list(C.FEATURE_COLUMNS))
    return df


def universe(n=6, **target) -> pd.DataFrame:
    """n assets spread evenly on every percentile feature; asset T gets `target`."""
    rows = {}
    for i in range(n):
        x = (i - n / 2) / n
        rows[f"A{i}"] = feature_row(flow_4h=x, flow_24h=x, thrust_1h=x, thrust_4h=x,
                                    vamom_24h=x, vamom_7d=x)
    rows["T"] = feature_row(**target)
    return feature_frame(rows)


# ==============================================================================
# resample_4h
# ==============================================================================
def test_resample_4h_aligns_to_utc_and_drops_incomplete_buckets():
    # 01:00 .. 13:00 on the day: 00-04 lacks 00:00, 12-16 is still forming.
    idx = pd.date_range("2026-09-19 01:00", "2026-09-19 13:00", freq="h", tz="UTC")
    n = len(idx)
    df = pd.DataFrame({
        "open": np.arange(n, dtype=float), "high": np.arange(n) + 10.0,
        "low": np.arange(n) - 10.0, "close": np.arange(n) + 0.5,
        "quote_volume": np.ones(n), "taker_buy_quote": np.full(n, 0.25),
        "trades": np.full(n, 2.0),
    }, index=idx)
    out = resample_4h(df)
    assert list(out.index) == [pd.Timestamp("2026-09-19 04:00", tz="UTC"),
                               pd.Timestamp("2026-09-19 08:00", tz="UTC")]
    assert list(out.columns) == list(df.columns)
    first = out.iloc[0]  # 1H bars 04,05,06,07 = positions 3..6
    assert first["open"] == 3.0 and first["close"] == 6.5
    assert first["high"] == 16.0 and first["low"] == -7.0
    assert first["quote_volume"] == 4.0 and first["taker_buy_quote"] == 1.0 and first["trades"] == 8.0


def test_resample_4h_drops_a_bucket_with_a_hole():
    df = bars_from_close(np.linspace(100, 110, 24), end=pd.Timestamp("2026-09-19 23:00", tz="UTC"))
    holed = df.drop(pd.Timestamp("2026-09-19 09:00", tz="UTC"))
    out = resample_4h(holed)
    assert pd.Timestamp("2026-09-19 08:00", tz="UTC") not in out.index
    assert len(out) == 5


def test_resample_4h_empty():
    out = resample_4h(bars_from_close([1.0]).iloc[:0])
    assert out.empty


# ==============================================================================
# Look-ahead
# ==============================================================================
def test_bars_oi_and_funding_after_as_of_change_nothing(cfg):
    base = random_bars(0.001, 0.55, seed=3)
    oi = oi_rising_at_end()
    fund = pd.Series(np.linspace(1e-4, 3e-4, 60),
                     index=pd.date_range(end=AS_OF, periods=60, freq="8h"))
    clean = compute_features(window({"X": base}, {"X": oi}, {"X": fund}), cfg)

    future_close = np.full(30, base["close"].iloc[-1] * 3)
    future = bars_from_close(future_close, qv=np.full(30, 9e9), buy_share=1.0,
                             end=AS_OF + 30 * H)  # includes the still-open 14:00 bar
    polluted_bars = pd.concat([base, future])
    polluted_oi = pd.concat([oi, oi_frame(np.full(10, 9e9), end=AS_OF + 10 * H).iloc[1:]])
    polluted_f = pd.concat([fund, pd.Series([0.05], index=[AS_OF + 3 * H])])
    dirty = compute_features(window({"X": polluted_bars}, {"X": polluted_oi}, {"X": polluted_f}), cfg)
    pd.testing.assert_frame_equal(clean, dirty)


# ==============================================================================
# Structure (F5 / F6)
# ==============================================================================
def _line_frame(kind: str, break_at: int | None, hold: bool = True, n: int = 120) -> pd.DataFrame:
    """Four exact touches of a line at bars 20/40/60/80; optional close-break."""
    i = np.arange(n, dtype=float)
    if kind == "resistance":
        line = 124 - 0.2 * i
        high = np.where(np.isin(i, [20, 40, 60, 80]), line, line - 3)
        close = high - 1
        low = close - 1
        if break_at is not None:
            for b in range(break_at, n):
                close[b] = line[b] * 1.05 + 0.1 * (b - break_at)
                high[b], low[b] = close[b] + 0.2, close[b] - 0.2
            if not hold:
                close[-1] = line[-1] * 0.97
                high[-1], low[-1] = close[-1] + 0.2, close[-1] - 0.2
    else:
        line = 76 + 0.2 * i
        low = np.where(np.isin(i, [20, 40, 60, 80]), line, line + 3)
        close = low + 1
        high = close + 1
        if break_at is not None:
            for b in range(break_at, n):
                close[b] = line[b] * 0.95 - 0.1 * (b - break_at)
                high[b], low[b] = close[b] + 0.2, close[b] - 0.2
    idx = pd.date_range(end=AS_OF - 4 * H, periods=n, freq="4h")
    return pd.DataFrame({"high": high, "low": low, "close": close}, index=idx)


def test_bull_break_exact_bar_and_invalidation(cfg):
    state, since, inval = structure_state(_line_frame("resistance", 115), cfg.structure)
    assert state == C.BULL_BREAK
    assert since == 4  # broke on bar 115, newest bar is 119
    assert inval == pytest.approx(124 - 0.2 * 119)


def test_bull_break_on_the_newest_bar_is_zero_bars_old(cfg):
    state, since, _ = structure_state(_line_frame("resistance", 119), cfg.structure)
    assert (state, since) == (C.BULL_BREAK, 0)


def test_bear_break_exact_bar_and_invalidation(cfg):
    state, since, inval = structure_state(_line_frame("support", 113), cfg.structure)
    assert state == C.BEAR_BREAK
    assert since == 6
    assert inval == pytest.approx(76 + 0.2 * 119)


def test_stale_break_is_not_an_event(cfg):
    state, _, inval = structure_state(_line_frame("resistance", 105), cfg.structure)
    assert state != C.BULL_BREAK and inval is None


def test_failed_break_is_not_an_event(cfg):
    state, _, inval = structure_state(_line_frame("resistance", 115, hold=False), cfg.structure)
    assert state != C.BULL_BREAK and inval is None


def test_no_break_without_a_close_through_the_line(cfg):
    state, _, inval = structure_state(_line_frame("resistance", None), cfg.structure)
    assert state in (C.BEAR_TREND, C.NEUTRAL) and inval is None


def _trend(slope: float, n: int = 150) -> pd.DataFrame:
    i = np.arange(n, dtype=float)
    close = 100 + slope * i + 0.3 * np.sin(i)  # smooth wiggle: no clean 3-touch line
    return pd.DataFrame({"high": close + 0.2, "low": close - 0.2, "close": close},
                        index=pd.date_range(end=AS_OF - H, periods=n, freq="h"))


def test_ema_states(cfg):
    assert structure_state(_trend(0.5), cfg.structure)[0] == C.BULL_TREND
    assert structure_state(_trend(-0.5), cfg.structure)[0] == C.BEAR_TREND
    flat = _trend(0.0)
    flat["close"] = 100 + np.where(np.arange(150) % 2, 0.1, -0.1)
    flat["high"], flat["low"] = flat["close"] + 0.2, flat["close"] - 0.2
    assert structure_state(flat, cfg.structure) == (C.NEUTRAL, None, None)


def test_bull_trend_counts_bars_since_the_cross(cfg):
    i = np.arange(150, dtype=float)
    close = np.where(i < 100, 200 - i, 100 + 3 * (i - 100))  # falls, then turns up at 100
    frame = pd.DataFrame({"high": close + 0.1, "low": close - 0.1, "close": close},
                         index=pd.date_range(end=AS_OF - H, periods=150, freq="h"))
    state, since, _ = structure_state(frame, cfg.structure)
    assert state == C.BULL_TREND
    fast = pd.Series(close).ewm(span=cfg.structure.ema_fast, adjust=False).mean().to_numpy()
    slow = pd.Series(close).ewm(span=cfg.structure.ema_slow, adjust=False).mean().to_numpy()
    above = fast > slow
    first_above = max(p for p in range(1, 150) if above[p] and not above[p - 1])
    assert since == 149 - first_above


def test_structure_refuses_short_history(cfg):
    short = _trend(0.5, n=min_structure_bars(cfg.structure) - 1)
    with pytest.raises(ValueError):
        structure_state(short, cfg.structure)


# ==============================================================================
# Features F1-F3
# ==============================================================================
def test_flow_thrust_and_returns_exact(cfg):
    n = 300
    close = np.full(n, 100.0)
    close[-25] = 100.0
    close[-1] = 110.0  # last bar: up 10%
    qv = np.full(n, 1e6)
    qv[-1] = 1e6 * math.e
    f = compute_features(window({"X": bars_from_close(close, qv=qv, buy_share=0.6)}), cfg).loc["X"]
    assert f["flow_24h"] == pytest.approx(0.2) and f["flow_4h"] == pytest.approx(0.2)
    assert f["thrust_1h"] == pytest.approx(1.0)
    assert f["ret_1h"] == pytest.approx(0.10) and f["ret_24h"] == pytest.approx(0.10)
    assert f["n_bars_1h"] == n and f["last_close"] == 110.0
    assert f["quote_volume_24h"] == pytest.approx(23e6 + 1e6 * math.e)


def test_down_bar_thrust_is_negative(cfg):
    close = np.full(300, 100.0)
    close[-1] = 95.0
    qv = np.full(300, 1e6)
    qv[-1] = 1e6 * math.e ** 2
    f = compute_features(window({"X": bars_from_close(close, qv=qv)}), cfg).loc["X"]
    assert f["thrust_1h"] == pytest.approx(-2.0)


def test_vamom_is_return_over_realised_volatility(cfg):
    bars = random_bars(0.002, seed=5)
    bars["taker_buy_quote"] = bars["quote_volume"] * (0.5 + 0.05 * np.sin(np.arange(len(bars)) / 5))
    f = compute_features(window({"X": bars}), cfg).loc["X"]
    lr = np.log(bars["close"]).diff().iloc[-24:]
    expected = (bars["close"].iloc[-1] / bars["close"].iloc[-25] - 1) / (lr.std(ddof=1) * math.sqrt(24))
    assert f["vamom_24h"] == pytest.approx(expected)
    assert np.isfinite(f["vamom_7d"]) and np.isfinite(f["vamom_24h_z"]) and np.isfinite(f["flow_24h_z"])


def test_flow_z_sees_an_unusual_day(cfg):
    share = np.full(400, 0.5) + 0.02 * np.sin(np.arange(400))
    share[-24:] = 0.8
    f = compute_features(window({"X": random_bars(seed=2).assign(
        taker_buy_quote=lambda d: d["quote_volume"] * share)}), cfg).loc["X"]
    assert f["flow_24h_z"] > 3


def test_a_stale_series_measures_nothing_current(cfg):
    bars = random_bars(seed=1).iloc[:-3]  # stopped printing three hours ago
    f = compute_features(window({"X": bars}), cfg).loc["X"]
    assert np.isnan(f["ret_1h"]) and np.isnan(f["ret_24h"]) and np.isnan(f["thrust_1h"])


def test_feature_frame_shape(cfg):
    f = compute_features(window({"X": random_bars()}, survivors=["X", "MISSING"]), cfg)
    assert list(f.columns) == list(C.FEATURE_COLUMNS)
    assert list(f.index) == ["X", "MISSING"]
    assert set(f.loc["MISSING", "flags"]) == {C.FLAG_THIN_BOOK, C.FLAG_INSUFFICIENT}
    assert f.loc["MISSING", "state_4h"] is None


# ==============================================================================
# F4 OI quadrants
# ==============================================================================
@pytest.mark.parametrize(
    "ret, oi, flow, pctile, expected",
    [
        (0.05, 0.05, 0.1, 50, C.OI_CONFIRM_LONG),
        (0.05, 0.05, -0.1, 50, C.OI_NEUTRAL),       # up + OI up + selling
        (0.05, -0.05, -0.1, 50, C.OI_SHORT_COVERING),
        (0.05, -0.05, 0.1, 50, C.OI_SHORT_COVERING),
        (-0.05, 0.05, -0.1, 50, C.OI_NEW_SHORTS),
        (-0.05, 0.05, 0.1, 50, C.OI_NEUTRAL),
        (-0.05, -0.05, 0.1, 50, C.OI_LONG_LIQUIDATION),
        (0.001, 0.08, 0.1, 99, C.OI_LEVERAGE_BUILD),  # flat price, extreme OI build
        (0.001, 0.08, 0.1, 50, C.OI_NEUTRAL),         # flat price, ordinary OI rise
        (0.05, 0.005, 0.1, 99, C.OI_NEUTRAL),         # OI flat
        (np.nan, 0.05, 0.1, 50, None),
        (0.05, np.nan, 0.1, 50, None),
    ],
)
def test_oi_quadrant_table(cfg, ret, oi, flow, pctile, expected):
    assert oi_quadrant(ret, oi, flow, 0.02, pctile, cfg) == expected


def test_confirm_long_end_to_end_uses_contracts(cfg):
    bars = random_bars(0.003, 0.6, seed=4, noise=0.002)
    oi = oi_rising_at_end(rise=0.10)
    oi["oi_usd"] = 1.0  # notional is context only; changing it must change nothing
    f = compute_features(window({"X": bars}, {"X": oi}), cfg).loc["X"]
    assert f["oi_chg_24h"] == pytest.approx(0.10, rel=1e-3)
    assert f["oi_quadrant_24h"] == C.OI_CONFIRM_LONG
    assert f["oi24_own_pctile"] == 100.0


def test_oi_older_than_one_hour_is_not_now(cfg):
    oi = oi_rising_at_end().iloc[:-2]  # newest reading two hours old
    f = compute_features(window({"X": random_bars()}, {"X": oi}), cfg).loc["X"]
    assert np.isnan(f["oi_chg_24h"]) and f["oi_quadrant_24h"] is None


# ==============================================================================
# Risk flags R1-R3
# ==============================================================================
def _flat_price_bars(n=400):
    close = 100 + np.where(np.arange(n) % 2, 0.5, -0.5)  # ret_24h exactly 0
    return bars_from_close(close, qv=np.full(n, 2e6))


def test_r2_leverage_no_move(cfg):
    f = compute_features(window({"X": _flat_price_bars()}, {"X": oi_rising_at_end()}), cfg).loc["X"]
    assert C.FLAG_LEVERAGE_NO_MOVE in f["flags"]
    assert f["oi_quadrant_24h"] == C.OI_LEVERAGE_BUILD
    assert C.FLAG_CROWDING not in f["flags"]  # no funding supplied


def test_r1_crowding_needs_funding_and_oi_extremes(cfg):
    fund = pd.Series(np.r_[np.full(40, 1e-4), 1e-3], index=pd.date_range(end=AS_OF, periods=41, freq="8h"))
    bars = random_bars(0.003, 0.6, seed=4, noise=0.002)
    f = compute_features(window({"X": bars}, {"X": oi_rising_at_end()}, {"X": fund}), cfg).loc["X"]
    assert C.FLAG_CROWDING in f["flags"] and f["funding_own_pctile"] == 100.0
    # Same funding, ordinary OI: no flag.
    calm = oi_frame(1e6 * (1 + 0.001 * np.random.default_rng(0).standard_normal(400)))
    f = compute_features(window({"X": bars}, {"X": calm}, {"X": fund}), cfg).loc["X"]
    assert C.FLAG_CROWDING not in f["flags"]
    # Extreme but NEGATIVE funding is not long crowding.
    neg = pd.Series(np.r_[np.full(40, -1e-3), -1e-4], index=fund.index)
    f = compute_features(window({"X": bars}, {"X": oi_rising_at_end()}, {"X": neg}), cfg).loc["X"]
    assert C.FLAG_CROWDING not in f["flags"]


def test_r3_thin_book_and_insufficient_history(cfg):
    thin = random_bars(seed=1).assign(quote_volume=1e3, taker_buy_quote=5e2)
    short = random_bars(seed=1, n=cfg.min_bars_1h - 1)
    f = compute_features(window({"THIN": thin, "SHORT": short}), cfg)
    assert f.loc["THIN", "flags"] == [C.FLAG_THIN_BOOK]
    assert f.loc["SHORT", "flags"] == [C.FLAG_INSUFFICIENT]


# ==============================================================================
# Score
# ==============================================================================
def test_score_output_shape(cfg):
    out = score_pulse(universe(), {"T": 3}, cfg)
    assert list(out.columns) == list(C.PULSE_COLUMNS)
    assert set(out.loc["T", "components"]) == set(COMPONENTS) == set(cfg.weights.as_dict())
    assert sorted(out["rank"].dropna()) == list(range(1, len(out) + 1))


def test_uptrend_with_buying_outranks_downtrend_with_selling(cfg):
    bars = {f"N{i}": random_bars(0.0, 0.5, seed=10 + i) for i in range(5)}
    bars["UP"] = random_bars(0.003, 0.6, seed=1)
    bars["DOWN"] = random_bars(-0.003, 0.4, seed=2)
    out = score_pulse(compute_features(window(bars), cfg), {}, cfg)
    assert out.loc["UP", "rank"] == 1
    assert out.loc["DOWN", "rank"] == len(bars)
    assert out.loc["UP", "state_4h"] == C.BULL_TREND
    # No OI anywhere: oi_confirm is dark for everyone and drops out (D-075).
    assert out.loc["UP", "components"]["oi_confirm"] is None
    assert out.loc["UP", "coverage"] == pytest.approx(1.0)


def test_missing_live_component_scores_zero_not_renormalised(cfg):
    frame = universe(state_4h=C.BULL_TREND, flow_4h=0.4, flow_24h=0.4)
    full = score_pulse(frame, {}, cfg).loc["T"]
    frame.loc["T", "state_4h"] = None
    gap = score_pulse(frame, {}, cfg).loc["T"]
    w = cfg.weights.as_dict()
    assert gap["components"]["structure_4h"] is None
    assert gap["coverage"] == pytest.approx(1 - w["structure_4h"] / sum(w.values()))
    assert full["score"] - gap["score"] == pytest.approx(75.0 * w["structure_4h"] / sum(w.values()))


def test_component_dark_for_most_drops_out_for_everyone(cfg):
    frame = universe()
    frame["oi_quadrant_24h"] = None
    frame.loc[["A0", "A1"], "oi_quadrant_24h"] = C.OI_CONFIRM_LONG  # 2 < live_min_assets
    out = score_pulse(frame, {}, cfg)
    assert out.loc["A0", "components"]["oi_confirm"] is None
    assert (out["coverage"] == 1.0).all()
    base = frame.copy()
    base["oi_quadrant_24h"] = None
    pd.testing.assert_series_equal(out["score"], score_pulse(base, {}, cfg)["score"])


def test_excluded_assets_never_enter_the_ranking_pool(cfg):
    frame = universe()
    before = score_pulse(frame, {}, cfg)["score"]
    extra = frame.copy()
    extra.loc["DEAD"] = pd.Series(feature_row(flow_24h=9.0, flow_4h=9.0, flags=[C.FLAG_THIN_BOOK]))
    after = score_pulse(extra, {}, cfg)
    pd.testing.assert_series_equal(after["score"].drop("DEAD"), before)
    dead = after.loc["DEAD"]
    assert np.isnan(dead["score"]) and np.isnan(dead["rank"]) and not dead["aligned"]
    assert dead["flags"] == [C.FLAG_THIN_BOOK]


def test_penalties_multiply(cfg):
    frame = universe()
    clean = score_pulse(frame, {}, cfg).loc["A5", "score"]
    frame.at["A5", "flags"] = [C.FLAG_CROWDING, C.FLAG_LEVERAGE_NO_MOVE]
    hit = score_pulse(frame, {}, cfg).loc["A5"]
    mult = cfg.crowding_multiplier * cfg.leverage_no_move_multiplier
    assert hit["penalty"] == pytest.approx(mult)
    assert hit["score"] == pytest.approx(clean * mult)


def test_no_live_component_means_no_score(cfg):
    frame = feature_frame({f"A{i}": feature_row() for i in range(cfg.live_min_assets - 1)})
    out = score_pulse(frame, {}, cfg)
    assert out["score"].isna().all() and out["rank"].isna().all()


def _strong(**kw):
    x = dict(flow_4h=0.5, flow_24h=0.5, thrust_1h=0.5, thrust_4h=0.5, vamom_24h=0.5, vamom_7d=0.5,
             state_4h=C.BULL_BREAK, state_1h=C.BULL_TREND, oi_quadrant_24h=C.OI_CONFIRM_LONG)
    x.update(kw)
    return x


def test_aligned_logic(cfg):
    out = score_pulse(universe(**_strong()), {"T": 3}, cfg).loc["T"]
    assert out["score"] >= cfg.aligned_min_score and out["aligned"]
    assert not score_pulse(universe(**_strong()), {"T": cfg.aligned_gem_rank_max + 1}, cfg).loc["T", "aligned"]
    assert not score_pulse(universe(**_strong()), {}, cfg).loc["T", "aligned"]
    assert not score_pulse(universe(**_strong(state_4h=C.NEUTRAL)), {"T": 3}, cfg).loc["T", "aligned"]
    assert not score_pulse(universe(**_strong(flags=[C.FLAG_CROWDING])), {"T": 3}, cfg).loc["T", "aligned"]
    weak = _strong(flow_4h=-1, flow_24h=-1, thrust_1h=-1, thrust_4h=-1, vamom_24h=-1, vamom_7d=-1)
    low = score_pulse(universe(**weak), {"T": 3}, cfg).loc["T"]
    assert low["score"] < cfg.aligned_min_score and not low["aligned"]


# ==============================================================================
# D-074 guards (property style: many seeded scenarios, end to end)
# ==============================================================================
def _scenario_universe(seed: int, cfg):
    """Six random assets with OI; their features are computed once (features are per-asset)."""
    rng = np.random.default_rng(seed)
    bars = {f"N{i}": random_bars(rng.normal(0, 0.002), rng.uniform(0.45, 0.55), seed=seed * 10 + i)
            for i in range(6)}
    oi = {a: oi_frame(1e6 * (1 + 0.01 * np.cumsum(rng.standard_normal(400)) / 20)) for a in bars}
    return rng, compute_features(window(bars, oi), cfg)


def _score_target(others, cfg, bars, oi=None, funding=None) -> float:
    t = compute_features(window({"T": bars}, {"T": oi} if oi is not None else None,
                                {"T": funding} if funding is not None else None), cfg)
    return score_pulse(pd.concat([others, t]), {}, cfg).loc["T", "score"]


@pytest.mark.parametrize("seed", range(16))
def test_oi_alone_never_raises_a_score(cfg, seed):
    """Holding price and flow fixed, more OI never beats flat OI unless price AND flow are up."""
    rng, others = _scenario_universe(seed, cfg)
    target = random_bars(rng.normal(0, 0.003), rng.uniform(0.4, 0.6), seed=seed + 999)
    t = compute_features(window({"T": target}), cfg).loc["T"]
    if t["ret_24h"] > 0 and t["flow_24h"] > 0:
        pytest.skip("price AND flow already positive: OI may confirm here by design")
    base = _score_target(others, cfg, target, oi_frame(np.full(400, 1e6)))
    for rise in (0.005, 0.02, 0.1, 0.5):
        s = _score_target(others, cfg, target, oi_rising_at_end(rise=rise, seed=seed))
        assert s <= base + 1e-9, (seed, rise)
    for fall in (0.02, 0.2):  # and OI falling cannot help either
        s = _score_target(others, cfg, target, oi_rising_at_end(rise=-fall, seed=seed))
        assert s <= base + 1e-9, (seed, -fall)


@pytest.mark.parametrize("seed", range(12))
def test_funding_never_raises_a_score(cfg, seed):
    rng, others = _scenario_universe(seed, cfg)
    target = random_bars(rng.normal(0, 0.003), rng.uniform(0.4, 0.6), seed=seed + 7)
    oi = oi_rising_at_end(rise=rng.uniform(0, 0.2), seed=seed)
    without = _score_target(others, cfg, target, oi)
    idx = pd.date_range(end=AS_OF, periods=60, freq="8h")
    for scale in (-1e-2, -1e-4, 0.0, 1e-4, 1e-2):
        fund = pd.Series(rng.normal(scale, abs(scale) + 1e-5, 60), index=idx)
        s = _score_target(others, cfg, target, oi, fund)
        assert s <= without + 1e-9, (seed, scale)
