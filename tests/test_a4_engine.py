"""Indicator math against hand-checkable fixtures, and score behaviour
on synthetic regimes. RSI fixture cross-checked against the classic
Wilder worked example structure (14-period, smoothed)."""
from marketsense.agents.a4_technical.engine import (
    MIN_BARS,
    adx,
    atr,
    compute,
    rsi,
    sma,
    zscore,
)


def test_sma():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1, 2], 3) is None


def test_rsi_extremes_and_range():
    up = list(range(1, 40))                      # monotonic rise
    assert rsi([float(x) for x in up]) == 100.0
    down = [float(x) for x in range(40, 1, -1)]  # monotonic fall
    assert rsi(down) < 1.0
    flat_wiggle = [100 + (1 if i % 2 else -1) * 0.5 for i in range(40)]
    r = rsi(flat_wiggle)
    assert 40 <= r <= 60                          # oscillating ≈ neutral


def test_atr_constant_range():
    n = 30
    highs = [102.0] * n
    lows = [98.0] * n
    closes = [100.0] * n
    assert abs(atr(highs, lows, closes) - 4.0) < 1e-9  # TR is 4 every bar


def test_adx_trending_vs_flat():
    n = 60
    up_h = [100 + i + 1.0 for i in range(n)]
    up_l = [100 + i - 1.0 for i in range(n)]
    up_c = [100 + float(i) for i in range(n)]
    a_up = adx(up_h, up_l, up_c)
    assert a_up is not None
    adx_v, pdi, mdi = a_up
    assert adx_v > 25 and pdi > mdi              # strong up trend

    import math
    osc_c = [100 + 3 * math.sin(i / 3) for i in range(n)]
    osc_h = [c + 1 for c in osc_c]
    osc_l = [c - 1 for c in osc_c]
    a_osc = adx(osc_h, osc_l, osc_c)
    assert a_osc[0] < adx_v                       # weaker than the trend


def test_zscore():
    v = [10.0] * 20 + [10.0]
    assert zscore(v) == 0.0
    v = [10.0] * 20 + [20.0]
    assert zscore(v) > 3  # constant window, spike


def _bars(closes, vol=1000.0, dp=40.0):
    return [{"close": c, "high": c * 1.01, "low": c * 0.99,
             "volume": vol, "delivery_pct": dp} for c in closes]


def test_compute_uptrend_beats_downtrend():
    up = compute(_bars([100 * 1.003 ** i for i in range(250)]), None)
    dn = compute(_bars([100 * 0.997 ** i for i in range(250)]), None)
    assert up["score"] > 65 > dn["score"]
    assert up["label"] in ("uptrend", "strong_uptrend")
    assert dn["label"] in ("downtrend", "strong_downtrend")
    assert up["components"]["atr_stop"] < up["components"]["close"]


def test_compute_needs_min_bars():
    assert compute(_bars([100.0] * (MIN_BARS - 1)), None) is None


def test_rel_strength_moves_score():
    closes = [100 * 1.002 ** i for i in range(250)]
    weak_idx = [100 * 1.004 ** i for i in range(250)]   # index outruns stock
    strong_idx = [100.0] * 250                          # stock outruns index
    weak = compute(_bars(closes), weak_idx)
    strong = compute(_bars(closes), strong_idx)
    assert strong["score"] > weak["score"]
    assert strong["components"]["rs_excess_63d"] > 0 > weak["components"]["rs_excess_63d"]
