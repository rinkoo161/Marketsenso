"""A6 — risk & governance. Independent veto authority (§3).

Verdicts:
    hard_block — A7 must not issue a stance, full stop
    penalty    — stance allowed; conviction capped / sized down, reasons attached
    clear      — no governance/liquidity objection

Every verdict carries its NAMED reasons — "Can downgrade or suppress any
signal from A7 **with a stated reason**" is the brief's own emphasis, and
an unexplained veto is indistinguishable from a bug.

Battery implemented vs deferred (honesty ledger):
  IMPLEMENTED: GSM/ASM stages · illiquidity (median 20d turnover) ·
  statutory auditor resignation ≤365d · regulatory action ≤180d ·
  circuit-lock frequency (close==high==low days) · penny price ·
  trade-for-trade series · pledge ACTIVITY ≤180d · microcap where market
  cap is computable from results XBRL (paid-up capital / face value).
  UNAVAILABLE (named in every assessment): pledge PERCENTAGE (needs Reg-31
  XBRL parsing) · going-concern flag (needs audit-report text) · free
  float (needs full shareholding parse) · portfolio exposure caps (needs
  a holdings source — Phase 5).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from marketsense.bus import topics
from marketsense.bus.outbox import publish
from marketsense.core.logging import get_logger
from marketsense.db.models import (
    Filing,
    FilingClassification,
    FinancialsQuarterly,
    PriceDaily,
    Score,
    Surveillance,
)

log = get_logger("a6.engine")

MODEL_VERSION = "a6-v1"

MIN_MEDIAN_TURNOVER_LACS = 10.0   # ₹10L/day — below this, exits are fantasy
PENNY_CLOSE = 5.0
MICROCAP_CR = 300.0
CIRCUIT_LOCK_HARD = 0.20          # >20% of last 60 sessions locked

UNAVAILABLE = [
    "pledge_pct (needs Reg-31 XBRL parse)",
    "going_concern (needs audit-report text)",
    "free_float (needs full shareholding parse)",
    "portfolio_caps (needs holdings source — Phase 5)",
]


def assess_symbol(db, symbol: str, *, now: datetime) -> dict:
    hard: list[str] = []
    penalties: list[str] = []
    checks: dict = {}

    # --- surveillance (latest snapshot) ---
    surv = db.execute(
        select(Surveillance.framework, Surveillance.stage,
               func.max(Surveillance.as_of))
        .where(Surveillance.symbol == symbol)
        .group_by(Surveillance.framework, Surveillance.stage)).all()
    for framework, stage, _ in surv:
        tag = f"{framework}:{stage}"
        checks.setdefault("surveillance", []).append(tag)
        if framework == "gsm":
            hard.append(f"GSM surveillance ({stage})")
        elif framework == "asm_lt" and stage not in ("Stage I",):
            hard.append(f"ASM long-term {stage}")
        else:
            penalties.append(f"surveillance {tag}")

    # --- price-derived checks (last 60 sessions) ---
    bars = db.scalars(
        select(PriceDaily)
        .where(PriceDaily.symbol == symbol, PriceDaily.source == "bhavcopy")
        .order_by(PriceDaily.trade_date.desc()).limit(60)).all()
    if bars:
        turnovers = sorted((b.turnover or 0.0) for b in bars[:20])
        median_t = turnovers[len(turnovers) // 2]
        checks["median_20d_turnover_lacs"] = round(median_t, 1)
        if median_t < MIN_MEDIAN_TURNOVER_LACS:
            hard.append(f"illiquid (median 20d turnover ₹{median_t:.0f}L "
                        f"< ₹{MIN_MEDIAN_TURNOVER_LACS:.0f}L)")
        last_close = bars[0].close
        checks["close"] = last_close
        if last_close is not None and last_close < PENNY_CLOSE:
            hard.append(f"penny stock (close ₹{last_close})")
        locked = sum(1 for b in bars
                     if b.high is not None and b.high == b.low)
        lock_ratio = locked / len(bars)
        checks["circuit_lock_ratio_60d"] = round(lock_ratio, 3)
        if lock_ratio > CIRCUIT_LOCK_HARD:
            hard.append(f"circuit-locked {lock_ratio:.0%} of last 60 sessions")
        series = bars[0].series
        if series in ("BE", "BZ"):
            penalties.append(f"trade-for-trade series ({series})")

    # --- governance from our own classifications ---
    y1 = now - timedelta(days=365)
    auditor = db.scalar(
        select(func.count()).select_from(FilingClassification)
        .join(Filing, Filing.id == FilingClassification.filing_id)
        .where(Filing.symbol == symbol,
               FilingClassification.category == "auditor_resignation",
               FilingClassification.materiality >= 9,
               FilingClassification.observed_at >= y1)) or 0
    if auditor:
        hard.append(f"statutory auditor resignation in last 12m ({auditor} filing(s))")
    d180 = now - timedelta(days=180)
    reg = db.scalar(
        select(func.count()).select_from(FilingClassification)
        .join(Filing, Filing.id == FilingClassification.filing_id)
        .where(Filing.symbol == symbol,
               FilingClassification.category == "regulatory_action",
               FilingClassification.observed_at >= d180)) or 0
    if reg:
        penalties.append(f"regulatory action filings in last 180d ({reg})")
    pledge = db.scalar(
        select(func.count()).select_from(Filing)
        .where(Filing.symbol == symbol, Filing.feed == "encumbrance",
               Filing.observed_at >= d180)) or 0
    if pledge:
        penalties.append(f"pledge/encumbrance activity in last 180d ({pledge}); "
                         "pledge % unavailable")

    # --- microcap where computable ---
    fin = db.scalars(
        select(FinancialsQuarterly)
        .where(FinancialsQuarterly.symbol == symbol)
        .order_by(FinancialsQuarterly.period_end.desc()).limit(1)).first()
    if fin and bars and bars[0].close:
        raw = fin.raw or {}
        try:
            paidup = float(raw.get("PaidUpValueOfEquityShareCapital", 0))
            face = float(raw.get("FaceValueOfEquityShareCapital", 0))
            if paidup > 0 and face > 0:
                shares = paidup / face
                mcap_cr = shares * bars[0].close / 1e7
                checks["mcap_cr"] = round(mcap_cr)
                if mcap_cr < MICROCAP_CR:
                    penalties.append(f"microcap (₹{mcap_cr:.0f} Cr)")
        except (TypeError, ValueError):
            pass

    verdict = "hard_block" if hard else ("penalty" if penalties else "clear")
    score = 0.0 if hard else max(0.0, 100.0 - 15.0 * len(penalties))
    return {
        "verdict": verdict, "score": score,
        "hard_blocks": hard, "penalties": penalties,
        "checks": checks, "unavailable": UNAVAILABLE,
    }


def assess_all(db_factory) -> dict:
    """EOD pass over every symbol any agent scored today + all
    surveillance members (they need standing verdicts even unscored)."""
    stats = {"assessed": 0, "hard_block": 0, "penalty": 0, "clear": 0}
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        d1 = now - timedelta(days=2)
        symbols = {s for (s,) in db.execute(
            select(Score.symbol).where(Score.observed_at >= d1,
                                       Score.agent.in_(("a3", "a4", "a5")))
            .distinct())}
        symbols |= {s for (s,) in db.execute(select(Surveillance.symbol).distinct())}

        for sym in sorted(symbols):
            result = assess_symbol(db, sym, now=now)
            row = Score(agent="a6", symbol=sym, score=result["score"],
                        label=result["verdict"], confidence=1.0,
                        components={k: result[k] for k in
                                    ("hard_blocks", "penalties", "checks",
                                     "unavailable")},
                        model_version=MODEL_VERSION, as_of=now)
            db.add(row)
            db.flush()
            publish(db, topics.RISK_ASSESSED, {
                "symbol": sym, "verdict": result["verdict"],
                "score": result["score"], "score_id": row.id,
                "hard_blocks": result["hard_blocks"],
                "penalties": result["penalties"],
            })
            stats["assessed"] += 1
            stats[result["verdict"]] += 1
        db.commit()
    log.info("a6_assessed", **stats)
    return stats
