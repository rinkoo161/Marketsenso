"""A7 — fusion. The ONLY agent that issues a stance (§3).

Inputs per symbol: latest a3/a4/a5 Score rows + an Event Score derived
from recent high-materiality classifications + A6's verdict.

Fusion rules:
  * missing inputs renormalise the profile weights AND cut confidence —
    a symbol with only a technical score cannot reach high conviction on
    weights alone (same honesty contract as A3/A5).
  * freshness decay: an input older than its half-life contributes less
    confidence (fundamentals age in quarters, technicals in days).
  * A6 hard_block → stance 'suppressed', conviction forced to 0, the
    block reasons quoted verbatim in the thesis. A6 penalty → conviction
    capped at 55 and size halved, reasons attached.
  * hysteresis: a new signal row is written only when the stance changes
    or conviction moves > HYSTERESIS points — §3's alert-spam guard.

Thesis: DETERMINISTIC — bullets are templated from the stored component
values themselves (score ids + filing ids + numbers), so every claim is
traceable by construction. LLM prose polish can be layered in Phase 5;
it will write around these values, never produce them (§10).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import distinct, func, select

from marketsense.agents.a7_fusion.profiles import HORIZON, PROFILES, PROFILES_VERSION
from marketsense.bus import topics
from marketsense.bus.outbox import publish
from marketsense.core.logging import get_logger
from marketsense.db.models import Filing, FilingClassification, Score, Signal

log = get_logger("a7.engine")

# v2 (2026-08-09): confidence scales with weight coverage (missing axes
# cost confidence rather than being normalised away)
# v3 (2026-08-09): stance-aware level semantics — exit/suppressed carry
# no levels or size; reduce/hold keep only the stop
MODEL_VERSION = f"a7-v3-{PROFILES_VERSION}"
HYSTERESIS = 8.0
EVENT_WINDOW_D = 30

# freshness half-lives per input (days) — beyond ~2 half-lives an input
# stops adding confidence, though its value still contributes
HALF_LIFE = {"a3": 100.0, "a4": 5.0, "a5": 7.0, "event": 10.0}


def _decay(age_days: float, agent: str) -> float:
    return 0.5 ** (age_days / HALF_LIFE[agent])


def weighted_fusion(values: dict[str, tuple[float, float] | None],
                    weights: dict[str, float], *, floor: float = 40.0
                    ) -> tuple[float, float, float] | None:
    """Shared fusion math for live A7 and the backtest (one definition —
    two drifting copies of a weighting loop is how backtests lie).
    values: axis -> (score 0-100, effective_confidence 0-1) or None.
    Returns (conviction, confidence, weight_covered) or None below floor."""
    total_w = conviction = conf_acc = 0.0
    for axis, w in weights.items():
        v = values.get(axis)
        if v is None:
            continue
        score, conf = v
        conviction += w * score / 100.0
        conf_acc += w * conf
        total_w += w
    if total_w < floor:
        return None
    # confidence = conf_acc/100, NOT conf_acc/total_w: normalising over
    # covered weight only let a fundamentals-blind signal (45% coverage)
    # report 80% confidence (DIAMONDYD audit, 2026-08-09). Missing axes
    # must cost confidence — same contract A3 already honours.
    return (round(conviction * 100.0 / total_w, 1),
            round(conf_acc / 100.0, 2), total_w)


def event_score(db, symbol: str, *, now: datetime,
                visibility: str = "observed") -> tuple[float, float, list[dict]]:
    """(score 0-100, confidence, evidence) from recent classifications.
    50 = neutral; materiality × sentiment × recency push it either way.

    visibility: 'observed' (live — what we had ingested by `now`) or
    'event' (backtest over backfilled windows — visibility inferred from
    the filing's broadcast time; the caller labels results reconstructed)."""
    from marketsense.agents.a2_docintel.classifier import (
        MODEL_VERSION as A2_VERSION,
    )

    since = now - timedelta(days=EVENT_WINDOW_D)
    ts_col = (FilingClassification.observed_at if visibility == "observed"
              else Filing.event_at)
    # current A2 version only — a filing reclassified across v3/v4/v5 must
    # count ONCE, not once per version (live bug: triplicated M&A filings
    # inflated an event score to 93)
    rows = db.execute(
        select(FilingClassification, Filing.subject)
        .join(Filing, Filing.id == FilingClassification.filing_id)
        .where(Filing.symbol == symbol,
               FilingClassification.model_version == A2_VERSION,
               ts_col >= since, ts_col <= now,
               FilingClassification.routine.is_(False),
               FilingClassification.materiality >= 3)
        .add_columns(Filing.event_at)
        .order_by(FilingClassification.materiality.desc())
        .limit(10)).all()
    if not rows:
        return 50.0, 0.0, []
    push, evidence = 0.0, []
    for c, subject, f_event_at in rows:
        ts = c.observed_at if visibility == "observed" else (f_event_at or c.observed_at)
        age = max(0.0, (now - ts).total_seconds() / 86400.0)
        w = _decay(age, "event") * (c.materiality / 10.0) * c.confidence
        push += 50.0 * w * c.sentiment
        evidence.append({"filing_id": c.filing_id, "classification_id": c.id,
                         "category": c.category, "materiality": c.materiality,
                         "sentiment": c.sentiment,
                         "subject": (subject or "")[:80]})
    score = max(0.0, min(100.0, 50.0 + push))
    conf = min(1.0, max(r[0].confidence for r in rows))
    return score, conf, evidence


PEER_DAMPING = 0.35   # a peer's event moves you at ~1/3 strength
PEER_MIN_MATERIALITY = 7


def peer_event_score(db, symbol: str, *, now: datetime,
                     visibility: str = "observed"
                     ) -> tuple[float, float, list[dict]]:
    """Same-industry propagation (user requirement 2026-08-08: a steel
    filing matters to steel peers; a pharma event to pharma names).
    High-materiality (≥7) events from industry PEERS contribute at
    PEER_DAMPING strength; evidence rows carry the SOURCE symbol so the
    thesis says whose event it was.

    Scope honesty: this covers same-industry filing events. Cross-industry
    input-cost chains (steel → auto) and commodity-price NEWS are not
    filings — they need the news layer + a curated linkage map (planned
    with the ltp-monitor news integration)."""
    from marketsense.agents.a2_docintel.classifier import (
        MODEL_VERSION as A2_VERSION,
    )
    from marketsense.db.models import Security

    me = db.scalar(select(Security).where(Security.symbol == symbol))
    industry = (me.extra or {}).get("industry") if me else None
    if not industry:
        return 50.0, 0.0, []
    peers = [s for (s,) in db.execute(
        select(Security.symbol).where(
            Security.extra["industry"].as_string() == industry,
            Security.symbol != symbol))]
    if not peers:
        return 50.0, 0.0, []

    since = now - timedelta(days=EVENT_WINDOW_D)
    ts_col = (FilingClassification.observed_at if visibility == "observed"
              else Filing.event_at)
    rows = db.execute(
        select(FilingClassification, Filing.symbol, Filing.subject)
        .join(Filing, Filing.id == FilingClassification.filing_id)
        .where(Filing.symbol.in_(peers),
               FilingClassification.model_version == A2_VERSION,
               ts_col >= since, ts_col <= now,
               FilingClassification.routine.is_(False),
               FilingClassification.materiality >= PEER_MIN_MATERIALITY)
        .order_by(FilingClassification.materiality.desc())
        .limit(8)).all()
    if not rows:
        return 50.0, 0.0, []
    push, evidence = 0.0, []
    for c, peer_sym, subject in rows:
        age = max(0.0, (now - c.observed_at).total_seconds() / 86400.0)
        w = _decay(age, "event") * (c.materiality / 10.0) * c.confidence
        push += 50.0 * w * c.sentiment * PEER_DAMPING
        evidence.append({"filing_id": c.filing_id, "peer": peer_sym,
                         "category": c.category, "materiality": c.materiality,
                         "sentiment": c.sentiment, "industry": industry,
                         "subject": (subject or "")[:70]})
    return (max(0.0, min(100.0, 50.0 + push)),
            min(1.0, max(r[0].confidence for r in rows)) * PEER_DAMPING,
            evidence)


def _latest_scores(db, symbol: str) -> dict[str, Score]:
    out: dict[str, Score] = {}
    for agent in ("a3", "a4", "a5", "a6"):
        row = db.scalars(
            select(Score).where(Score.agent == agent, Score.symbol == symbol)
            .order_by(Score.as_of.desc()).limit(1)).first()
        if row is not None:
            out[agent] = row
    return out


def _stance(conviction: float, risk_verdict: str) -> str:
    if risk_verdict == "hard_block":
        return "suppressed"
    if conviction >= 70:
        return "buy"
    if conviction >= 60:
        return "accumulate"
    if conviction >= 40:
        return "hold"
    if conviction >= 25:
        return "reduce"
    return "exit"


def _levels(a4: Score | None, profile: str = "default") -> dict:
    """Per-horizon geometry (p3): the same ATR feeds different stop
    multiples and reward ratios per profile — a 1-5d trade and a 3-6m
    position must not share targets (user finding, 2026-08-09)."""
    from marketsense.agents.a7_fusion.profiles import levels_for

    if a4 is None or not a4.components:
        return {}
    c = a4.components
    close, atr = c.get("close"), c.get("atr14")
    if not close or not atr:
        return {}
    g = levels_for(profile)
    stop = round(close - g["stop_atr"] * atr, 2)
    risk = close - stop
    return {
        "entry_low": round(close - 0.5 * atr, 2),
        "entry_high": round(close + 0.25 * atr, 2),
        "target_low": round(close + g["t_low"] * risk, 2),
        "target_high": round(close + g["t_high"] * risk, 2),
        "invalidation": stop,
        "level_basis": f"close {close}, ATR14 {atr}; {profile}: stop = "
                       f"close - {g['stop_atr']}*ATR, targets "
                       f"{g['t_low']}-{g['t_high']}x risk",
    }


def _size_pct(a4: Score | None, risk_verdict: str,
              profile: str = "default") -> float | None:
    """Volatility-adjusted: risking 1% of capital to THIS profile's stop
    (wider positional stop → smaller size, same rupee risk). Penalty
    halves it. Capped at 10%."""
    from marketsense.agents.a7_fusion.profiles import levels_for

    if a4 is None or not a4.components:
        return None
    close, atr = a4.components.get("close"), a4.components.get("atr14")
    if not close or not atr:
        return None
    stop_atr = levels_for(profile)["stop_atr"]
    size = min(10.0, 1.0 / (stop_atr * atr / close))
    if risk_verdict == "penalty":
        size /= 2.0
    return round(size, 1)


def _thesis(symbol: str, inputs: dict, ev_evidence: list[dict],
            a6: Score | None, conviction: float,
            weights: dict[str, float] | None = None,
            stop: float | None = None) -> dict:
    bullets_for: list[str] = []
    bullets_against: list[str] = []

    # zero-weight axes did not drive this conviction — citing them in the
    # thesis is a false evidence trail (found live: a 1-5d short signal
    # quoting fundamentals that carried 0 weight)
    w = weights or {}
    a3 = inputs.get("a3") if w.get("fundamental", 1) > 0 else None
    a4 = inputs.get("a4") if w.get("technical", 1) > 0 else None
    a5 = inputs.get("a5") if w.get("flow", 1) > 0 else None
    if a4:
        c = a4.components or {}
        line = (f"technical {a4.score:.0f}/100 ({a4.label}); close {c.get('close')} "
                f"vs SMA200 {c.get('sma200') and round(c['sma200'], 1)}, "
                f"RSI {c.get('rsi14')} [score#{a4.id}]")
        (bullets_for if a4.score >= 55 else bullets_against).append(line)
    if a3:
        c = a3.components or {}
        rev_yoy = c.get("rev_yoy")
        yoy_txt = f"{rev_yoy:.0%}" if rev_yoy is not None else "n/a"
        line = (f"fundamentals {a3.score:.0f}/100 over {c.get('quarters_available')}q "
                f"({c.get('basis')}); rev YoY {yoy_txt} [score#{a3.id}]")
        (bullets_for if a3.score >= 55 else bullets_against).append(line)
        for flag in (c.get("flags") or [])[:2]:
            bullets_against.append(f"forensic flag: {flag} [score#{a3.id}]")
    if a5:
        c = a5.components or {}
        line = f"flow {a5.score:.0f}/100 ({a5.label}) [score#{a5.id}]"
        if c.get("deal_ratio") is not None:
            line += f"; 20d large-deal net ratio {c['deal_ratio']:+.1%}"
        if c.get("promoter_delta_pp") is not None:
            line += f"; promoter Δ {c['promoter_delta_pp']:+.2f}pp"
        (bullets_for if a5.score >= 55 else bullets_against).append(line)
    for ev in ev_evidence[:3]:
        peer = ev.get("peer")
        prefix = (f"peer event ({peer}, {ev.get('industry', '')}): "
                  if peer else "")
        line = (f"{prefix}{ev['category']} m{ev['materiality']} "
                f"({ev['subject']}) [filing#{ev['filing_id']}]")
        (bullets_for if ev["sentiment"] > 0 else bullets_against).append(line)
    if a6 and a6.label != "clear":
        for reason in (a6.components or {}).get("hard_blocks", [])[:3]:
            bullets_against.append(f"A6 block: {reason} [score#{a6.id}]")
        for reason in (a6.components or {}).get("penalties", [])[:3]:
            bullets_against.append(f"A6 penalty: {reason} [score#{a6.id}]")

    # the single thing that would change the view — cite THIS profile's
    # stop, not A4's raw 2xATR (levels are per-horizon since p3)
    a6_blocks = (a6.components or {}).get("hard_blocks", []) if a6 else []
    if a6_blocks:
        # a suppressed view changes when the BLOCK lifts, not on news
        changer = (f"A6 clearing its block would restore scoring "
                   f"(currently: {a6_blocks[0]})")
    elif a4 and stop is not None:
        changer = (f"a close below {stop} (this profile's ATR stop) "
                   f"invalidates the technical basis")
    elif a3:
        changer = "next quarterly result reversing the fundamental trend"
    else:
        changer = "any high-materiality filing (event coverage is the only input)"

    return {
        "for": bullets_for[:3], "against": bullets_against[:3],
        "view_changer": changer,
        "evidence": {
            "score_ids": {k: v.id for k, v in inputs.items()},
            "event_filings": [e["filing_id"] for e in ev_evidence],
        },
    }


def fuse_symbol(db, symbol: str, *, profile: str = "default",
                now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    inputs = _latest_scores(db, symbol)
    a6 = inputs.pop("a6", None)
    ev_score, ev_conf, ev_evidence = event_score(db, symbol, now=now)
    # same-industry peer events fold into the event axis at dampened
    # strength; peer evidence is tagged with the source symbol
    pe_score, pe_conf, pe_evidence = peer_event_score(db, symbol, now=now)
    if pe_conf > 0:
        ev_score = max(0.0, min(100.0, ev_score + (pe_score - 50.0)))
        ev_conf = max(ev_conf, pe_conf)
        ev_evidence = ev_evidence + pe_evidence

    weights = PROFILES[profile]
    values: dict[str, tuple[float, float] | None] = {
        "event": (ev_score, ev_conf) if ev_conf > 0 else None,
    }
    for axis, agent in (("fundamental", "a3"), ("technical", "a4"),
                        ("flow", "a5")):
        row = inputs.get(agent)
        if row is None:
            values[axis] = None
            continue
        age = max(0.0, (now - row.as_of).total_seconds() / 86400.0)
        values[axis] = (row.score, (row.confidence or 0.5) * _decay(age, agent))
    fused = weighted_fusion(values, weights)
    if fused is None:
        return None  # not enough signal families to say anything
    conviction, confidence, total_w = fused

    risk_verdict = a6.label if a6 else "unassessed"
    if risk_verdict == "hard_block":
        conviction = 0.0
    elif risk_verdict == "penalty":
        conviction = min(conviction, 55.0)

    stance = _stance(conviction, risk_verdict)
    levels = _levels(inputs.get("a4"), profile)
    size = _size_pct(inputs.get("a4"), risk_verdict, profile)
    # Long-only level semantics per stance (audit 2026-08-09: an 'exit'
    # signal displayed an entry zone and +79% upside — contradictory and
    # dangerous). exit/suppressed: no levels, no size — the action is to
    # be out. reduce/hold: keep the STOP as the sell-the-rest trigger for
    # existing holders, but no entry/target/size invitation.
    if stance in ("exit", "suppressed"):
        levels = {}
        size = None
    elif stance in ("reduce", "hold"):
        levels = {"invalidation": levels.get("invalidation"),
                  "level_basis": levels.get("level_basis")}
        size = None
    thesis = _thesis(symbol, {**inputs, **({"a6": a6} if a6 else {})},
                     ev_evidence, a6, conviction, weights=weights,
                     stop=levels.get("invalidation"))
    thesis["weights_covered_pct"] = total_w
    thesis["event_score"] = ev_score

    return {
        "symbol": symbol, "profile": profile, "stance": stance,
        "conviction": conviction, "confidence": confidence,
        "horizon": HORIZON[profile], "risk_verdict": risk_verdict,
        "size_pct": size,
        "thesis": thesis, **levels,
    }


def issue_all(db_factory, *, profile: str = "default") -> dict:
    """Fuse every symbol with ≥2 score families; write a Signal only when
    hysteresis allows. Emits signal.issued per written row."""
    stats = {"considered": 0, "issued": 0, "held_by_hysteresis": 0,
             "suppressed": 0}
    now = datetime.now(timezone.utc)
    with db_factory() as db:
        symbols = [s for (s,) in db.execute(
            select(Score.symbol).where(Score.agent.in_(("a3", "a4", "a5")))
            .group_by(Score.symbol)
            .having(func.count(distinct(Score.agent)) >= 2))]
        for sym in symbols:
            result = fuse_symbol(db, sym, profile=profile, now=now)
            if result is None:
                continue
            stats["considered"] += 1
            prev = db.scalars(
                select(Signal).where(Signal.symbol == sym,
                                     Signal.profile == profile)
                .order_by(Signal.as_of.desc()).limit(1)).first()
            if (prev is not None and prev.stance == result["stance"]
                    and abs(prev.conviction - result["conviction"]) <= HYSTERESIS
                    and prev.model_version == MODEL_VERSION):
                # a model-version change always re-issues — semantics
                # changed, so the stored row no longer means the same thing
                stats["held_by_hysteresis"] += 1
                continue
            row = Signal(model_version=MODEL_VERSION, as_of=now, **{
                k: result.get(k) for k in
                ("symbol", "profile", "stance", "conviction", "confidence",
                 "horizon", "entry_low", "entry_high", "target_low",
                 "target_high", "invalidation", "size_pct", "thesis",
                 "risk_verdict")})
            db.add(row)
            db.flush()
            publish(db, topics.SIGNAL_ISSUED, {
                "symbol": sym, "signal_id": row.id, "stance": row.stance,
                "conviction": row.conviction, "profile": profile,
                "risk_verdict": row.risk_verdict,
            })
            stats["issued"] += 1
            if row.stance == "suppressed":
                stats["suppressed"] += 1
        db.commit()
    log.info("a7_issued", **stats)
    return stats
