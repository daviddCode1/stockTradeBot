"""Momentum, ranking, trend, breakout, pullback, regime, ATR and look-ahead tests."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy import indicators as ind
from src.strategy.interface import compute_features, trend_mask
from src.strategy.momentum import ClassicMomentum, PureMomentum
from src.strategy.momentum_breakout import MomentumBreakout
from src.strategy.momentum_pullback import MomentumPullback
from src.strategy.momentum_trend import MomentumTrend, MomentumTrendRegime
from src.strategy.ranking import percentile_rank
from src.strategy.regime import bull_regime


def test_momentum_12_1_skips_last_month():
    px = pd.Series(np.arange(1, 301, dtype=float))  # strictly rising prices
    m = ind.momentum(px, "MOM_12_1", {"m12": 252}, skip=21)
    t = 299
    assert m.iloc[t] == pytest.approx(px.iloc[t - 21] / px.iloc[t - 252] - 1)  # P[t-21]/P[t-252]-1


def test_momentum_lookbacks():
    px = pd.Series(np.linspace(10, 20, 300))
    for n in (63, 126, 189, 252):
        m = ind.momentum(px, f"MOM_{n}", {"m12": 252})
        assert m.iloc[-1] == pytest.approx(px.iloc[-1] / px.iloc[-1 - n] - 1)
        assert m.iloc[: n].isna().all()  # undefined until enough history


def test_percentile_rank_bounds_and_ties():
    sig = pd.DataFrame([[1.0, 2.0, 3.0, 3.0, np.nan]], columns=list("abcde"))
    elig = pd.DataFrame([[True, True, True, True, True]], columns=list("abcde"))
    r = percentile_rank(sig, elig, min_names=2)
    assert r.at[0, "a"] == 0.0 and r.at[0, "c"] == r.at[0, "d"] == pytest.approx((3.5 - 1) / 3)  # average rank ties
    assert np.isnan(r.at[0, "e"])  # no signal => no rank


def test_percentile_rank_ignores_ineligible_and_small_universe():
    sig = pd.DataFrame([[1.0, 2.0, 3.0]], columns=list("abc"))
    elig = pd.DataFrame([[True, False, True]], columns=list("abc"))
    r = percentile_rank(sig, elig, min_names=2)
    assert np.isnan(r.at[0, "b"]) and r.at[0, "c"] == 1.0
    assert percentile_rank(sig, elig, min_names=5).isna().all().all()  # too few names => no ranks


def test_sma_and_trend_variants():
    c = pd.DataFrame({"x": np.r_[np.full(200, 10.0), np.full(10, 20.0)]})
    s50, s200 = ind.sma(c, 50), ind.sma(c, 200)
    assert trend_mask(c, s50, s200, "T1")["x"].iloc[-1]  # price above SMA200
    assert trend_mask(c, s50, s200, "T2")["x"].iloc[-1]  # SMA50 above SMA200
    assert trend_mask(c, s50, s200, "T0")["x"].all()  # no filter
    with pytest.raises(ValueError):
        trend_mask(c, s50, s200, "T9")


def test_atr_wilder_matches_definition():
    n = 30
    h = pd.DataFrame({"x": np.full(n, 11.0)})
    l = pd.DataFrame({"x": np.full(n, 9.0)})
    c = pd.DataFrame({"x": np.full(n, 10.0)})
    atr = ind.atr_wilder(h, l, c, 14)
    assert atr["x"].iloc[-1] == pytest.approx(2.0)  # constant range 2 => ATR 2
    assert atr["x"].iloc[:13].isna().all()  # needs 14 bars to seed


def test_atr_includes_gaps():
    h = pd.DataFrame({"x": [10.0, 20.0]}); l = pd.DataFrame({"x": [9.0, 19.0]}); c = pd.DataFrame({"x": [9.5, 19.5]})
    tr = ind.true_range(h, l, c)
    assert tr["x"].iloc[1] == pytest.approx(10.5)  # |H - prev close| dominates on a gap


def test_breakout_excludes_today():
    high = pd.DataFrame({"x": [1, 2, 3, 4, 10.0]})
    rh = ind.rolling_high(high, 3)
    assert rh["x"].iloc[-1] == 4.0  # max of the previous 3 highs, not today's 10


def test_regime():
    up = pd.Series(np.linspace(100, 200, 250))
    assert bull_regime(up).iloc[-1] and not bull_regime(up).iloc[10]  # warm-up is not bull
    down = pd.Series(np.linspace(200, 100, 250))
    assert not bull_regime(down).iloc[-1]


def test_no_lookahead_features_and_entries(md, cfg):
    """Features/entries at day t must not change when future data is removed."""
    full_f = compute_features(md, cfg)
    t = md.dates[700]
    cut = md.slice(t)
    cut_f = compute_features(cut, cfg)
    pd.testing.assert_series_equal(full_f.rank.loc[t], cut_f.rank.loc[t], check_names=False)
    pd.testing.assert_series_equal(full_f.atr.loc[t], cut_f.atr.loc[t], check_names=False)
    for S in (PureMomentum, MomentumTrend, MomentumTrendRegime, MomentumBreakout, MomentumPullback):
        s = S(cfg)
        a = s.entries(md, full_f).loc[t]
        b = s.entries(cut, cut_f).loc[t]
        assert (a == b).all(), S.name


def test_variants_are_nested(md, cfg):
    f = compute_features(md, cfg)
    a, b = PureMomentum(cfg).entries(md, f), MomentumTrend(cfg).entries(md, f)
    d = MomentumBreakout(cfg).entries(md, f)
    assert (b <= a).all().all()  # adding a trend filter can only remove signals
    assert (d <= b).all().all()  # breakout is a subset of momentum+trend
    s0 = ClassicMomentum(cfg).entries(md, f)
    days = s0.any(axis=1)
    assert all(md.dates[i].month != md.dates[i + 1].month for i in np.flatnonzero(days.to_numpy()) if i + 1 < len(md.dates))
