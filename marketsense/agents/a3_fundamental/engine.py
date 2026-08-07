"""A3 — fundamental engine over financials_quarterly.

Score composition (0-100):
    growth   40 — revenue + PAT, YoY (preferred) / QoQ (fallback)
    margins  25 — operating-margin level and trajectory
    quality  35 — red-flag battery: other-income dependency, tax-rate
                  anomaly, exceptional-item reliance, finance-cost burden

Honesty rules (§10):
  * confidence scales with history depth — one quarter of data means
    conf ≈ 0.25 and NO growth points either way (absent ≠ neutral zero,
    absent = excluded and the composition renormalised).
  * checks whose inputs don't exist in the data (CFO/EBITDA needs cash
    flow; receivable days needs balance sheet — both arrive only in
    half-yearly/annual filings) are listed in components.unavailable,
    never silently scored.
  * every number in components traces to financials_quarterly rows,
    which trace to filing_id.

Basis: consolidated preferred, standalone when it's all a company files.
Never mixed within one score — same-basis is also the §9 reconciliation
rule.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from marketsense.bus import topics
from marketsense.bus.outbox import publish
from marketsense.core.logging import get_logger
from marketsense.db.models import FinancialsQuarterly, Score

log = get_logger("a3.engine")

MODEL_VERSION = "a3-v1"


def _pct(a: float | None, b: float | None) -> float | None:
    """Growth a vs b, guarded for None/zero/sign-flip garbage."""
    if a is None or b is None or b == 0:
        return None
    if b < 0:  # negative base makes growth% meaningless
        return None
    return (a - b) / b


def _op_margin(q: FinancialsQuarterly) -> float | None:
    """Operating margin proxy: (revenue - operating expenses) / revenue.
    IndAS 'Expenses' includes depreciation + finance costs; add them back
    to get an EBITDA-flavoured operating result."""
    if not q.revenue or q.revenue <= 0 or q.expenses is None:
        return None
    op = q.revenue - (q.expenses - (q.finance_costs or 0) - (q.depreciation or 0))
    return op / q.revenue


def compute(quarters: list[FinancialsQuarterly]) -> dict | None:
    """quarters: chronological, single basis. Returns score dict or None."""
    if not quarters:
        return None
    latest = quarters[-1]
    n = len(quarters)
    unavailable = ["cfo_vs_pat (needs cash flow)",
                   "receivable_days (needs balance sheet)",
                   "beneish_altman_piotroski (needs balance sheet + cash flow)"]

    # ---- growth (40, renormalised if absent) ----
    yoy = quarters[-5] if n >= 5 else None
    qoq = quarters[-2] if n >= 2 else None
    rev_yoy = _pct(latest.revenue, yoy.revenue) if yoy else None
    pat_yoy = _pct(latest.pat, yoy.pat) if yoy else None
    rev_qoq = _pct(latest.revenue, qoq.revenue) if qoq else None
    pat_qoq = _pct(latest.pat, qoq.pat) if qoq else None

    growth_pts = None
    rev_g = rev_yoy if rev_yoy is not None else rev_qoq
    pat_g = pat_yoy if pat_yoy is not None else pat_qoq
    if rev_g is not None or pat_g is not None:
        pts = 0.0
        if rev_g is not None:                     # ±25% maps 0..20
            pts += max(0.0, min(20.0, 10.0 + 40.0 * rev_g))
        else:
            pts += 10.0
        if pat_g is not None:
            pts += max(0.0, min(20.0, 10.0 + 40.0 * pat_g))
        else:
            pts += 10.0
        growth_pts = pts

    # ---- margins (25) ----
    margins = [m for m in (_op_margin(q) for q in quarters) if m is not None]
    margin_pts = None
    cur_margin = margins[-1] if margins else None
    if cur_margin is not None:
        level = max(0.0, min(15.0, 15.0 * cur_margin / 0.25))  # 25% margin = full
        traj = 5.0
        if len(margins) >= 3:
            traj = 10.0 if margins[-1] > margins[-3] else 2.0
        elif len(margins) == 2:
            traj = 8.0 if margins[-1] > margins[-2] else 3.0
        margin_pts = min(25.0, level + traj)

    # ---- quality battery (35) ----
    flags: list[str] = []
    q_pts = 35.0
    if latest.pbt and latest.pbt > 0 and latest.other_income:
        oi_share = latest.other_income / latest.pbt
        if oi_share > 0.5:
            flags.append(f"other_income {oi_share:.0%} of PBT")
            q_pts -= 12.0
        elif oi_share > 0.3:
            flags.append(f"other_income {oi_share:.0%} of PBT (mild)")
            q_pts -= 6.0
    if latest.pbt and latest.pbt > 0 and latest.tax is not None:
        tax_rate = latest.tax / latest.pbt
        if tax_rate < 0.10 or tax_rate > 0.45:
            flags.append(f"tax rate anomaly {tax_rate:.0%}")
            q_pts -= 8.0
    exc = (latest.raw or {}).get("ExceptionalItemsBeforeTax")
    try:
        if exc is not None and latest.pbt and abs(float(exc)) > 0.25 * abs(latest.pbt):
            flags.append("exceptional items >25% of PBT")
            q_pts -= 8.0
    except ValueError:
        pass
    if latest.revenue and latest.finance_costs and latest.revenue > 0:
        fin_burden = latest.finance_costs / latest.revenue
        if fin_burden > 0.15:
            flags.append(f"finance costs {fin_burden:.0%} of revenue")
            q_pts -= 7.0
    if latest.pat is not None and latest.pat < 0:
        flags.append("loss-making quarter")
        q_pts -= 10.0
    q_pts = max(0.0, q_pts)

    # ---- compose, renormalising absent parts ----
    parts = [(growth_pts, 40.0), (margin_pts, 25.0), (q_pts, 35.0)]
    got = [(p, w) for p, w in parts if p is not None]
    weight_covered = sum(w for _, w in got)
    score = round(sum(p for p, _ in got) * (100.0 / weight_covered), 1) if weight_covered else None
    if score is None:
        return None

    confidence = round(min(1.0, n / 8.0) * (weight_covered / 100.0), 2)
    return {
        "score": min(100.0, score),
        "label": "flagged" if len(flags) >= 2 else ("clean" if not flags else "watch"),
        "confidence": confidence,
        "components": {
            "rev_yoy": rev_yoy, "pat_yoy": pat_yoy,
            "rev_qoq": rev_qoq, "pat_qoq": pat_qoq,
            "op_margin": round(cur_margin, 4) if cur_margin is not None else None,
            "growth_pts": growth_pts, "margin_pts": margin_pts,
            "quality_pts": q_pts, "flags": flags,
            "quarters_available": n, "basis": latest.basis,
            "weight_covered": weight_covered,
            "unavailable": unavailable,
            "filing_ids": [q.filing_id for q in quarters if q.filing_id],
        },
    }


def score_all(db_factory) -> dict:
    stats = {"scored": 0, "skipped": 0}
    with db_factory() as db:
        symbols = [s for (s,) in db.execute(
            select(FinancialsQuarterly.symbol).distinct())]
        for sym in symbols:
            rows = db.scalars(
                select(FinancialsQuarterly)
                .where(FinancialsQuarterly.symbol == sym)
                .order_by(FinancialsQuarterly.period_end)).all()
            # consolidated preferred; standalone only if that's all there is
            cons = [r for r in rows if r.basis == "consolidated"]
            series = cons or [r for r in rows if r.basis == "standalone"]
            result = compute(series)
            if result is None:
                stats["skipped"] += 1
                continue
            latest = series[-1]
            row = Score(agent="a3", symbol=sym, security_id=latest.security_id,
                        score=result["score"], label=result["label"],
                        confidence=result["confidence"],
                        components=result["components"],
                        model_version=MODEL_VERSION,
                        as_of=latest.period_end)
            db.add(row)
            db.flush()
            publish(db, topics.FUNDAMENTAL_UPDATED, {
                "symbol": sym, "score": result["score"],
                "label": result["label"], "confidence": result["confidence"],
                "score_id": row.id, "flags": result["components"]["flags"],
            })
            stats["scored"] += 1
        db.commit()
    log.info("a3_scored", **stats)
    return stats
