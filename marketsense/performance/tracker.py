"""§7.7 — signal performance tracking. "Non-negotiable: I need to know
whether this thing actually works."

Nightly: for every issued signal whose measurement window has fully
elapsed, record entry close → window-end close (+ Nifty 500 excess).
These are OBSERVED forward returns of signals that were actually issued
at the time — the credible counterpart to the reconstructed backtest,
accumulating from the system's first live signal (2026-08-08) forward.

Suppressed signals are tracked too: they measure what the A6 veto SAVED
(or cost) — a veto layer nobody audits is just a mood.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from marketsense.core.logging import get_logger
from marketsense.db.models import PriceDaily, Signal, SignalPerformance

log = get_logger("performance")

WINDOWS = {"1w": 5, "4w": 20, "12w": 60}
NIFTY500 = "IDX:Nifty 500"


def _closes(db, symbol: str) -> list[tuple[datetime, float]]:
    return [(r.trade_date, r.close) for r in db.scalars(
        select(PriceDaily)
        .where(PriceDaily.symbol == symbol,
               PriceDaily.source.in_(("bhavcopy", "index")),
               PriceDaily.close.isnot(None))
        .order_by(PriceDaily.trade_date))]


def _window_return(closes: list[tuple[datetime, float]], start: datetime,
                   n_bars: int) -> tuple[float, float] | None:
    """(entry, exit) closes for a window starting at the first bar ON or
    AFTER `start`. None until the window has fully elapsed."""
    idx = next((i for i, (d, _) in enumerate(closes) if d >= start), None)
    if idx is None or idx + n_bars >= len(closes):
        return None
    return closes[idx][1], closes[idx + n_bars][1]


def measure(db_factory) -> dict:
    stats = {"measured": 0, "pending": 0}
    with db_factory() as db:
        idx_closes = _closes(db, NIFTY500)
        signals = db.scalars(select(Signal)).all()
        done = {(sid, w) for sid, w in db.execute(
            select(SignalPerformance.signal_id, SignalPerformance.window))}
        closes_cache: dict[str, list] = {}

        for s in signals:
            closes = closes_cache.setdefault(s.symbol, _closes(db, s.symbol))
            if not closes:
                continue
            for window, n in WINDOWS.items():
                if (s.id, window) in done:
                    continue
                w = _window_return(closes, s.as_of, n)
                if w is None:
                    stats["pending"] += 1
                    continue
                entry, exit_ = w
                if not entry:
                    continue
                ret = exit_ / entry - 1.0
                iw = _window_return(idx_closes, s.as_of, n)
                idx_ret = (iw[1] / iw[0] - 1.0) if iw and iw[0] else None
                db.add(SignalPerformance(
                    signal_id=s.id, symbol=s.symbol, stance=s.stance,
                    profile=s.profile, conviction=s.conviction,
                    window=window, entry_price=entry, exit_price=exit_,
                    ret=ret, index_ret=idx_ret,
                    excess=(ret - idx_ret) if idx_ret is not None else None))
                stats["measured"] += 1
        db.commit()
    log.info("performance_measured", **stats)
    return stats


def summary(db_factory) -> dict:
    """Aggregates for the API/dashboard: by stance × window."""
    with db_factory() as db:
        rows = db.scalars(select(SignalPerformance)).all()
    out: dict = {}
    for r in rows:
        b = out.setdefault(r.stance, {}).setdefault(r.window, {"n": 0, "ex": []})
        b["n"] += 1
        if r.excess is not None:
            b["ex"].append(r.excess)
    return {
        stance: {
            window: {
                "n": b["n"],
                "avg_excess_pct": round(100 * sum(b["ex"]) / len(b["ex"]), 2)
                                  if b["ex"] else None,
                "hit_rate_pct": round(100 * sum(1 for x in b["ex"] if x > 0)
                                      / len(b["ex"]), 1) if b["ex"] else None,
            }
            for window, b in windows.items()
        }
        for stance, windows in out.items()
    }
