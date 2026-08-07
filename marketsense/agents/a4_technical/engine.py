"""A4 — technical engine. Dependency-free indicator math (house style:
plain Python over numpy; the series are ≤1,250 floats and this keeps the
whole platform's install surface small).

Score composition (0-100), components stored per §10 traceability:
    trend       30  — close vs SMA20/50/200 stack
    momentum    20  — RSI(14) Wilder + ADX(14) direction
    rel_strength 20 — 63-day return vs Nifty 500
    vol_delivery 15 — volume z-score + 20d delivery-% trend
    range_pos   15  — position in the 52-week range

Every score row carries as_of = the bar date it was computed FROM, and
observed_at = when we computed it — a backtest replays exactly what was
knowable, including our own computation lag.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from marketsense.bus import topics
from marketsense.bus.outbox import publish
from marketsense.core.logging import get_logger
from marketsense.db.models import PriceDaily, Score

log = get_logger("a4.engine")

MODEL_VERSION = "a4-v1"
MIN_BARS = 60          # below this, no score — degrade by absence, not noise
NIFTY500 = "IDX:Nifty 500"


# ------------------------------------------------------------ indicator math

def sma(v: list[float], n: int) -> float | None:
    return sum(v[-n:]) / n if len(v) >= n else None


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def atr(highs: list[float], lows: list[float], closes: list[float],
        n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def adx(highs: list[float], lows: list[float], closes: list[float],
        n: int = 14) -> tuple[float, float, float] | None:
    """(adx, +di, -di) — Wilder smoothing throughout."""
    if len(closes) < 2 * n + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if up > dn and up > 0 else 0.0)
        minus_dm.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    tr_s, pdm_s, mdm_s = sum(trs[:n]), sum(plus_dm[:n]), sum(minus_dm[:n])
    dxs = []
    pdi = mdi = 0.0
    for i in range(n, len(trs)):
        tr_s = tr_s - tr_s / n + trs[i]
        pdm_s = pdm_s - pdm_s / n + plus_dm[i]
        mdm_s = mdm_s - mdm_s / n + minus_dm[i]
        pdi = 100.0 * pdm_s / tr_s if tr_s else 0.0
        mdi = 100.0 * mdm_s / tr_s if tr_s else 0.0
        dxs.append(100.0 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    if len(dxs) < n:
        return None
    a = sum(dxs[:n]) / n
    for dx in dxs[n:]:
        a = (a * (n - 1) + dx) / n
    return a, pdi, mdi


def zscore(v: list[float], n: int = 20) -> float | None:
    if len(v) < n + 1:
        return None
    window = v[-(n + 1):-1]
    mean = sum(window) / n
    var = sum((x - mean) ** 2 for x in window) / n
    sd = var ** 0.5
    if sd == 0:
        # constant window: any deviation is infinitely surprising — cap it
        # rather than lie with 0 (a volume spike after 20 flat days IS the
        # signal this exists to catch)
        return 0.0 if v[-1] == mean else (5.0 if v[-1] > mean else -5.0)
    return (v[-1] - mean) / sd


# --------------------------------------------------------------- the score

def compute(bars: list[dict], index_closes: list[float] | None) -> dict | None:
    """bars: chronological [{close, high, low, volume, delivery_pct}, …].
    Returns indicators + score components, or None below MIN_BARS."""
    if len(bars) < MIN_BARS:
        return None
    closes = [b["close"] for b in bars]
    highs = [b["high"] or b["close"] for b in bars]
    lows = [b["low"] or b["close"] for b in bars]
    vols = [b["volume"] or 0.0 for b in bars]
    close = closes[-1]

    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    r = rsi(closes)
    a = atr(highs, lows, closes)
    adx_t = adx(highs, lows, closes)
    vz = zscore(vols)
    hi52 = max(closes[-250:])
    lo52 = min(closes[-250:])

    # trend (30)
    trend = 0.0
    checks = [(s20 and close > s20), (s50 and close > s50),
              (s200 and close > s200), (s20 and s50 and s20 > s50),
              (s50 and s200 and s50 > s200)]
    trend = 30.0 * sum(bool(c) for c in checks) / len(checks)

    # momentum (20)
    mom = 0.0
    if r is not None:
        mom += 10.0 * max(0.0, 1.0 - abs(r - 60.0) / 40.0)  # peak near RSI 60
    if adx_t:
        adx_v, pdi, mdi = adx_t
        if adx_v > 20:
            mom += 10.0 if pdi > mdi else 2.0
    # rel strength (20): 63d return vs index
    rs_pts, rs_excess = 10.0, None
    if index_closes and len(index_closes) >= 64 and len(closes) >= 64:
        stock_ret = closes[-1] / closes[-64] - 1.0
        idx_ret = index_closes[-1] / index_closes[-64] - 1.0
        rs_excess = stock_ret - idx_ret
        rs_pts = max(0.0, min(20.0, 10.0 + 100.0 * rs_excess))  # ±10% maps 0..20

    # volume/delivery (15)
    vd = 7.5
    if vz is not None:
        vd = max(0.0, min(10.0, 5.0 + 2.5 * vz))
    dels = [b["delivery_pct"] for b in bars[-20:] if b.get("delivery_pct")]
    if len(dels) >= 10:
        first, second = dels[:len(dels) // 2], dels[len(dels) // 2:]
        vd += 5.0 if sum(second) / len(second) > sum(first) / len(first) else 0.0
    vd = min(vd, 15.0)

    # 52w position (15)
    rng = hi52 - lo52
    pos = (close - lo52) / rng if rng else 0.5
    range_pos = 15.0 * pos

    score = round(trend + mom + rs_pts + vd + range_pos, 1)
    label = ("strong_uptrend" if score >= 75 else
             "uptrend" if score >= 60 else
             "range" if score >= 40 else
             "downtrend" if score >= 25 else "strong_downtrend")
    return {
        "score": score, "label": label,
        "components": {
            "trend": round(trend, 1), "momentum": round(mom, 1),
            "rel_strength": round(rs_pts, 1), "vol_delivery": round(vd, 1),
            "range_pos": round(range_pos, 1),
            "close": close, "sma20": s20, "sma50": s50, "sma200": s200,
            "rsi14": round(r, 1) if r is not None else None,
            "atr14": round(a, 2) if a is not None else None,
            "adx14": round(adx_t[0], 1) if adx_t else None,
            "vol_z": round(vz, 2) if vz is not None else None,
            "rs_excess_63d": round(rs_excess, 4) if rs_excess is not None else None,
            "pct_off_52w_high": round(100.0 * (close / hi52 - 1.0), 1),
            "atr_stop": round(close - 2.0 * a, 2) if a else None,
            "bars": len(bars),
        },
    }


def score_all(db_factory, *, min_turnover_lacs: float = 10.0) -> dict:
    """Score every symbol with enough history on the latest bar date.
    min_turnover filters the illiquid tail (median-20d turnover below
    ~₹1L trades too thin to chart honestly — A6 will hard-block them
    anyway; skipping saves 60% of the loop)."""
    stats = {"scored": 0, "skipped_thin": 0, "skipped_short": 0}
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        latest = db.scalar(select(PriceDaily.trade_date)
                           .where(PriceDaily.source == "bhavcopy")
                           .order_by(PriceDaily.trade_date.desc()).limit(1))
        if latest is None:
            return {"error": "no price data"}
        idx_rows = db.scalars(
            select(PriceDaily).where(PriceDaily.symbol == NIFTY500)
            .order_by(PriceDaily.trade_date)).all()
        index_closes = [r.close for r in idx_rows if r.close]

        symbols = [s for (s,) in db.execute(
            select(PriceDaily.symbol).where(
                PriceDaily.trade_date == latest,
                PriceDaily.source == "bhavcopy").distinct())]

        for sym in symbols:
            rows = db.scalars(
                select(PriceDaily).where(PriceDaily.symbol == sym,
                                         PriceDaily.source == "bhavcopy")
                .order_by(PriceDaily.trade_date)).all()
            if len(rows) < MIN_BARS:
                stats["skipped_short"] += 1
                continue
            recent_turnover = sorted(r.turnover or 0 for r in rows[-20:])
            if recent_turnover[len(recent_turnover) // 2] < min_turnover_lacs:
                stats["skipped_thin"] += 1
                continue
            bars = [{"close": r.close, "high": r.high, "low": r.low,
                     "volume": r.volume, "delivery_pct": r.delivery_pct}
                    for r in rows if r.close]
            result = compute(bars, index_closes)
            if result is None:
                stats["skipped_short"] += 1
                continue
            row = Score(agent="a4", symbol=sym,
                        security_id=rows[-1].security_id,
                        score=result["score"], label=result["label"],
                        confidence=min(1.0, len(bars) / 250.0),
                        components=result["components"],
                        model_version=MODEL_VERSION, as_of=latest)
            db.add(row)
            db.flush()
            publish(db, topics.TECHNICAL_UPDATED, {
                "symbol": sym, "score": result["score"],
                "label": result["label"], "score_id": row.id,
                "as_of": latest.isoformat(),
            })
            stats["scored"] += 1
        db.commit()
    log.info("a4_scored", **stats)
    return stats
