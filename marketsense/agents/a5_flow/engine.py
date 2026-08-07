"""A5 — Flow Score (0-100) per symbol.

Composition (renormalised over what exists, same honesty contract as A3):
    large_deals   35 — 20d net bulk/block buying relative to turnover
    promoter      25 — promoter-holding delta from consecutive
                       shareholding_pattern filings (parsed from the
                       PR_AND_PRGRP field the feed carries)
    insider_act   15 — insider-filing ACTIVITY in 30d. Direction needs
                       the IT-form XBRL (not parsed yet) → activity is a
                       neutral-magnitude signal, stated as such
    surveillance  25 — ASM/GSM membership penalty (also A6's input)

Unavailable and listed as such: F&O OI/basis/PCR (needs F&O bhavcopy),
FII/DII per-symbol (NSE publishes market-level only — stored as context
in components.market).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from marketsense.bus import topics
from marketsense.bus.outbox import publish
from marketsense.core.logging import get_logger
from marketsense.db.models import (
    Filing,
    LargeDeal,
    MarketFlow,
    PriceDaily,
    Score,
    Surveillance,
)

log = get_logger("a5.engine")

MODEL_VERSION = "a5-v1"

_PR_GRP = re.compile(r"PR_AND_PRGRP:\s*([\d.]+)")


def _promoter_series(db, symbol: str) -> list[tuple[datetime, float]]:
    """Promoter % from shareholding_pattern filings, chronological."""
    rows = db.execute(
        select(Filing.event_at, Filing.subject, Filing.description)
        .where(Filing.feed == "shareholding_pattern", Filing.symbol == symbol)
        .order_by(Filing.event_at)
    ).all()
    out = []
    for ts, subject, desc in rows:
        m = _PR_GRP.search(subject or "") or _PR_GRP.search(desc or "")
        if m and ts:
            try:
                out.append((ts, float(m.group(1))))
            except ValueError:
                pass
    return out


def compute_symbol(db, symbol: str, *, now: datetime) -> dict | None:
    components: dict = {}
    parts: list[tuple[float | None, float]] = []

    # ---- large deals (35) ----
    d20 = now - timedelta(days=28)  # ~20 trading days
    deals = db.execute(
        select(LargeDeal.side, func.sum(LargeDeal.qty * LargeDeal.price))
        .where(LargeDeal.symbol == symbol, LargeDeal.day >= d20)
        .group_by(LargeDeal.side)).all()
    deal_pts = None
    if deals:
        net = sum(v if s == "BUY" else -v for s, v in deals if v)
        turnover = db.scalar(
            select(func.sum(PriceDaily.turnover))
            .where(PriceDaily.symbol == symbol,
                   PriceDaily.trade_date >= d20,
                   PriceDaily.source == "bhavcopy")) or 0.0
        turnover_rs = turnover * 1e5  # TURNOVER_LACS → rupees
        if turnover_rs > 0:
            ratio = max(-0.5, min(0.5, net / turnover_rs))
            deal_pts = 17.5 + 35.0 * ratio  # ±50% of turnover maps 0..35
            components["deal_net_rs"] = round(net)
            components["deal_ratio"] = round(ratio, 4)
    parts.append((deal_pts, 35.0))

    # ---- promoter delta (25) ----
    prom_pts = None
    series = _promoter_series(db, symbol)
    if len(series) >= 2:
        delta = series[-1][1] - series[-2][1]
        prom_pts = max(0.0, min(25.0, 12.5 + 5.0 * delta))  # ±2.5pp maps 0..25
        components["promoter_pct"] = series[-1][1]
        components["promoter_delta_pp"] = round(delta, 2)
    elif len(series) == 1:
        components["promoter_pct"] = series[-1][1]
    parts.append((prom_pts, 25.0))

    # ---- insider activity (15) — magnitude only, direction unavailable ----
    d30 = now - timedelta(days=30)
    n_insider = db.scalar(
        select(func.count()).select_from(Filing)
        .where(Filing.symbol == symbol, Filing.observed_at >= d30,
               Filing.feed.in_(("insider_trading", "sast_reg29")))) or 0
    insider_pts = 7.5 if n_insider == 0 else min(15.0, 7.5 + n_insider * 0.75)
    components["insider_filings_30d"] = n_insider
    components["insider_direction"] = "unavailable (IT-form XBRL not parsed)"
    parts.append((insider_pts, 15.0))

    # ---- surveillance (25) ----
    latest_surv = db.execute(
        select(Surveillance.framework, Surveillance.stage)
        .where(Surveillance.symbol == symbol)
        .order_by(Surveillance.as_of.desc()).limit(3)).all()
    surv_pts = 25.0
    if latest_surv:
        frameworks = {f for f, _ in latest_surv}
        if "gsm" in frameworks:
            surv_pts = 0.0
        elif "asm_lt" in frameworks:
            surv_pts = 5.0
        elif "asm_st" in frameworks:
            surv_pts = 10.0
        components["surveillance"] = [f"{f}:{s}" for f, s in latest_surv]
    parts.append((surv_pts, 25.0))

    got = [(p, w) for p, w in parts if p is not None]
    covered = sum(w for _, w in got)
    if covered < 40.0:  # surveillance+insider alone (always present) = 40
        return None
    score = round(sum(p for p, _ in got) * 100.0 / covered, 1)

    components["weight_covered"] = covered
    components["unavailable"] = ["fno_oi_basis_pcr (needs F&O bhavcopy)",
                                 "fii_dii_per_symbol (NSE publishes market-level only)"]
    return {"score": min(100.0, score),
            "label": ("surveillance" if surv_pts <= 10.0 else
                      "accumulation" if score >= 65 else
                      "distribution" if score <= 35 else "neutral"),
            "confidence": round(covered / 100.0, 2),
            "components": components}


def score_all(db_factory) -> dict:
    """Flow scores for symbols with any flow signal (deals, surveillance,
    promoter data) — scoring all 2.7k symbols with nothing but neutral
    insider counts would be noise dressed as coverage."""
    stats = {"scored": 0}
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        interesting = {s for (s,) in db.execute(select(LargeDeal.symbol).distinct())}
        interesting |= {s for (s,) in db.execute(select(Surveillance.symbol).distinct())}
        interesting |= {s for (s,) in db.execute(
            select(Filing.symbol).where(
                Filing.feed == "shareholding_pattern",
                Filing.symbol.isnot(None)).distinct())}
        market = {}
        for cat, net in db.execute(
                select(MarketFlow.category, MarketFlow.net_value)
                .order_by(MarketFlow.day.desc()).limit(2)).all():
            market[cat] = net

        for sym in sorted(interesting):
            result = compute_symbol(db, sym, now=now)
            if result is None:
                continue
            result["components"]["market"] = market
            row = Score(agent="a5", symbol=sym, score=result["score"],
                        label=result["label"], confidence=result["confidence"],
                        components=result["components"],
                        model_version=MODEL_VERSION, as_of=now)
            db.add(row)
            db.flush()
            publish(db, topics.FLOW_UPDATED, {
                "symbol": sym, "score": result["score"],
                "label": result["label"], "score_id": row.id,
            })
            stats["scored"] += 1
        db.commit()
    log.info("a5_scored", **stats)
    return stats
