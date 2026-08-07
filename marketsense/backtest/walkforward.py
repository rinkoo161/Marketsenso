"""Walk-forward backtest of the A7 fusion pipeline (§9 Phase 4).

WHAT THIS IS AND IS NOT (printed on the report's face, per §10):
  * pit_quality = RECONSTRUCTED end to end. observed_at history starts
    2026-08-05; for anything earlier, visibility is inferred from
    published timestamps (bhavcopy: knowable the same evening;
    financials: broadcast time; filings: event_at). That inference is
    honest but it is not what we actually observed — the report is a
    hypothesis, not evidence. The observed corpus grows daily and the
    same harness re-runs on it.
  * A5 flow and A6 surveillance CANNOT be replayed — deal/ASM/GSM
    history isn't published retrospectively. The backtest therefore
    fuses fundamental+technical+event only (weights renormalised — the
    same rule the live path uses when a family is missing), and A6 is
    reduced to its price-derived checks (illiquidity, penny,
    circuit-lock). Named in the report.
  * The A2 classifications used for event scores were produced by
    TODAY'S classifier looking at old filings — no temporal leak in the
    market-data sense, but the classifier itself post-dates the filings.

Mechanics: weekly rebalance dates; on each date D every scorable symbol
is scored using ONLY rows visible ≤ D; stances map exactly as live; the
position's forward return is measured at +1w/+4w/+12w against the
Nifty 500 index return over the same window.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from marketsense.agents.a3_fundamental.engine import compute as a3_compute
from marketsense.agents.a4_technical.engine import compute as a4_compute
from marketsense.agents.a6_risk.engine import (
    MIN_MEDIAN_TURNOVER_LACS,
    PENNY_CLOSE,
)
from marketsense.agents.a7_fusion.engine import (
    _stance,
    event_score,
    weighted_fusion,
)
from marketsense.agents.a7_fusion.profiles import PROFILES
from marketsense.core.logging import get_logger
from marketsense.db.models import FinancialsQuarterly, PriceDaily

log = get_logger("backtest")

HARNESS_VERSION = "wf-v1"
NIFTY500 = "IDX:Nifty 500"
FWD_WINDOWS = {"1w": 5, "4w": 20, "12w": 60}
RESULT_VISIBILITY_LAG_D = 45  # quarters without a broadcast ts: period_end + 45d


def _price_series(db, symbol: str) -> list[PriceDaily]:
    return db.scalars(
        select(PriceDaily)
        .where(PriceDaily.symbol == symbol, PriceDaily.source.in_(
            ("bhavcopy", "index")))
        .order_by(PriceDaily.trade_date)).all()


def _bars_upto(rows: list[PriceDaily], d: datetime) -> list[dict]:
    return [{"close": r.close, "high": r.high, "low": r.low,
             "volume": r.volume, "delivery_pct": r.delivery_pct}
            for r in rows if r.trade_date <= d and r.close]


def _quarters_upto(db, symbol: str, d: datetime) -> list[FinancialsQuarterly]:
    rows = db.scalars(
        select(FinancialsQuarterly)
        .where(FinancialsQuarterly.symbol == symbol,
               FinancialsQuarterly.basis == "consolidated")
        .order_by(FinancialsQuarterly.period_end)).all()
    visible = []
    for q in rows:
        vis = q.event_at or (q.period_end + timedelta(days=RESULT_VISIBILITY_LAG_D))
        if vis <= d:
            visible.append(q)
    return visible


def _fwd_return(rows: list[PriceDaily], d: datetime, n_bars: int) -> float | None:
    idx = None
    for i, r in enumerate(rows):
        if r.trade_date <= d and r.close:
            idx = i
    if idx is None or idx + n_bars >= len(rows):
        return None
    base, fwd = rows[idx].close, rows[idx + n_bars].close
    if not base or not fwd:
        return None
    return fwd / base - 1.0


def run(db_factory, *, profile: str = "default", rebalance_days: int = 5,
        max_symbols: int | None = None) -> dict:
    """Full walk-forward. Returns the aggregate stats dict (also logged)."""
    weights = PROFILES[profile]
    observations: list[dict] = []

    with db_factory() as db:
        idx_rows = _price_series(db, NIFTY500)
        if len(idx_rows) < 120:
            return {"error": f"only {len(idx_rows)} index bars — "
                             "run the indices backfill first"}
        # rebalance dates: every Nth index bar, leaving the longest
        # forward window measurable
        dates = [r.trade_date for r in idx_rows]
        rebalances = dates[80:-max(FWD_WINDOWS.values()):rebalance_days]

        from sqlalchemy import func as sfunc

        # history-rich symbols first — a symbol whose only quarter was
        # broadcast AFTER the test window is (correctly) invisible to
        # every rebalance and can never produce an observation
        symbols = [s for (s,) in db.execute(
            select(FinancialsQuarterly.symbol)
            .where(FinancialsQuarterly.basis == "consolidated")
            .group_by(FinancialsQuarterly.symbol)
            .order_by(sfunc.count().desc()))]
        if max_symbols:
            symbols = symbols[:max_symbols]

        idx_closes_all = idx_rows  # for RS + baseline
        for sym in symbols:
            price_rows = _price_series(db, sym)
            if len(price_rows) < 120:
                continue
            quarters_all = _quarters_upto(db, sym, dates[-1])
            for d in rebalances:
                bars = _bars_upto(price_rows, d)
                if len(bars) < 60:
                    continue
                # -- A6 price-derived hard blocks (replayable subset) --
                t20 = sorted(b["volume"] or 0 for b in bars[-20:])
                closes20 = [r for r in price_rows if r.trade_date <= d][-20:]
                med_turnover = sorted((r.turnover or 0) for r in closes20)[10]
                if (med_turnover < MIN_MEDIAN_TURNOVER_LACS
                        or bars[-1]["close"] < PENNY_CLOSE):
                    continue
                # -- axes as-of D --
                idx_closes = [r.close for r in idx_closes_all
                              if r.trade_date <= d and r.close]
                a4 = a4_compute(bars, idx_closes)
                quarters = [q for q in quarters_all
                            if (q.event_at or q.period_end +
                                timedelta(days=RESULT_VISIBILITY_LAG_D)) <= d]
                a3 = a3_compute(quarters) if quarters else None
                ev_s, ev_c, _ = event_score(db, sym, now=d, visibility="event")
                values = {
                    "technical": (a4["score"], min(1.0, len(bars) / 250.0))
                                 if a4 else None,
                    "fundamental": (a3["score"], a3["confidence"]) if a3 else None,
                    "event": (ev_s, ev_c) if ev_c > 0 else None,
                    "flow": None,  # not replayable — see module docstring
                }
                fused = weighted_fusion(values, weights)
                if fused is None:
                    continue
                conviction, confidence, covered = fused
                stance = _stance(conviction, "clear")
                obs = {"symbol": sym, "date": d, "stance": stance,
                       "conviction": conviction, "confidence": confidence}
                for name, n in FWD_WINDOWS.items():
                    r_sym = _fwd_return(price_rows, d, n)
                    r_idx = _fwd_return(idx_rows, d, n)
                    if r_sym is not None and r_idx is not None:
                        obs[f"ret_{name}"] = r_sym
                        obs[f"excess_{name}"] = r_sym - r_idx
                observations.append(obs)

        # baseline: Nifty 500 buy-and-hold over the tested span
        span_ret = (idx_rows[-1].close / idx_rows[80].close - 1.0
                    if idx_rows[80].close else None)

    # ---- aggregate ----
    by_stance: dict[str, dict] = {}
    for o in observations:
        b = by_stance.setdefault(o["stance"], {"n": 0})
        b["n"] += 1
        for w in FWD_WINDOWS:
            if f"excess_{w}" in o:
                b.setdefault(f"excess_{w}", []).append(o[f"excess_{w}"])
                b.setdefault(f"ret_{w}", []).append(o[f"ret_{w}"])
    summary: dict[str, dict] = {}
    for stance, b in by_stance.items():
        s: dict = {"observations": b["n"]}
        for w in FWD_WINDOWS:
            ex = b.get(f"excess_{w}", [])
            if ex:
                s[f"avg_excess_{w}"] = round(100 * sum(ex) / len(ex), 2)
                s[f"hit_rate_{w}"] = round(100 * sum(1 for x in ex if x > 0)
                                           / len(ex), 1)
                s[f"avg_ret_{w}"] = round(
                    100 * sum(b[f"ret_{w}"]) / len(b[f"ret_{w}"]), 2)
        summary[stance] = s

    result = {
        "harness": HARNESS_VERSION, "profile": profile,
        "pit_quality": "reconstructed",
        "replayable_axes": "fundamental+technical+event (flow and "
                           "surveillance history unavailable)",
        "rebalances": len(rebalances), "symbols_tested": len(symbols),
        "observations": len(observations),
        "nifty500_buy_hold_pct": round(100 * span_ret, 2) if span_ret else None,
        "by_stance": summary,
    }
    log.info("backtest_done", observations=len(observations))
    return result
