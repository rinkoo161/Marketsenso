"""A7 fusion invariants over seeded fixtures: veto supremacy, weight
renormalisation, hysteresis, evidence traceability, version dedup."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from marketsense.agents.a2_docintel.classifier import MODEL_VERSION as A2_V
from marketsense.agents.a7_fusion.engine import (
    HYSTERESIS,
    event_score,
    fuse_symbol,
    issue_all,
)
from marketsense.db.models import Filing, FilingClassification, Score, Signal

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _score(db, agent, symbol, score, label="x", components=None, conf=0.8):
    db.add(Score(agent=agent, symbol=symbol, score=score, label=label,
                 confidence=conf, components=components or {},
                 model_version="test", as_of=NOW - timedelta(hours=1)))


def _full_stack(db, symbol, a3=80, a4=75, a5=70, a6_label="clear"):
    _score(db, "a3", symbol, a3, components={"quarters_available": 8,
                                             "basis": "consolidated",
                                             "rev_yoy": 0.2, "flags": []})
    _score(db, "a4", symbol, a4, label="uptrend",
           components={"close": 100.0, "atr14": 3.0, "atr_stop": 94.0,
                       "sma200": 90.0, "rsi14": 60.0})
    _score(db, "a5", symbol, a5, label="accumulation")
    db.add(Score(agent="a6", symbol=symbol, score=100 if a6_label == "clear" else 0,
                 label=a6_label, confidence=1.0,
                 components={"hard_blocks": ["GSM surveillance (II)"]
                             if a6_label == "hard_block" else [],
                             "penalties": [], "checks": {}},
                 model_version="test", as_of=NOW - timedelta(hours=1)))


def test_hard_block_suppresses_regardless_of_scores(db_factory):
    with db_factory() as db:
        _full_stack(db, "VETOCO", a3=95, a4=95, a5=95, a6_label="hard_block")
        db.commit()
        r = fuse_symbol(db, "VETOCO", now=NOW)
    assert r["stance"] == "suppressed" and r["conviction"] == 0.0
    assert any("GSM" in b for b in r["thesis"]["against"])  # reason quoted


def test_high_scores_clear_risk_is_buy_with_levels(db_factory):
    with db_factory() as db:
        _full_stack(db, "GOODCO")
        db.commit()
        r = fuse_symbol(db, "GOODCO", now=NOW)                    # default
        r_short = fuse_symbol(db, "GOODCO", profile="short", now=NOW)
    assert r["stance"] in ("buy", "accumulate")
    # p3 geometry: default stop = close - 3*ATR = 100 - 9 = 91
    assert r["invalidation"] == 91.0
    assert r["target_low"] > 100.0 > r["entry_low"]
    assert r["size_pct"] is not None
    assert r["thesis"]["evidence"]["score_ids"]  # traceable by construction
    # horizons must NOT share geometry (user finding 2026-08-09):
    # short stop 1.5*ATR = 95.5, tighter than positional's 91; targets differ
    assert r_short["invalidation"] == 95.5
    assert r_short["target_high"] < r["target_high"]
    # same rupee risk → wider stop sizes smaller
    assert r["size_pct"] < r_short["size_pct"]


def test_missing_families_renormalise_or_refuse(db_factory):
    with db_factory() as db:
        _score(db, "a4", "ONLYTECH", 90.0,
               components={"close": 50.0, "atr14": 1.0, "atr_stop": 48.0})
        db.commit()
        r = fuse_symbol(db, "ONLYTECH", now=NOW)
    # technical alone = 25 weight < 40 floor → refuse to opine
    assert r is None


def test_event_score_counts_each_filing_once_across_versions(db_factory):
    with db_factory() as db:
        f = Filing(feed="announcements", symbol="DUPCO",
                   content_hash="a7dup".ljust(64, "0"), source="rss")
        db.add(f)
        db.flush()
        for ver in ("old-v1", "old-v2", A2_V):
            db.add(FilingClassification(
                filing_id=f.id, category="ma", materiality=7, sentiment=0.8,
                confidence=0.9, engine="rules", model_version=ver,
                event_at=NOW - timedelta(days=1)))
        db.commit()
        score, conf, evidence = event_score(db, "DUPCO", now=NOW)
    assert len(evidence) == 1  # one filing, one entry — not one per version


def test_peer_event_propagates_within_industry(db_factory):
    """User requirement 2026-08-08: a pharma peer's m9 event must reach
    other pharma names — dampened, evidence-tagged with the source."""
    from marketsense.agents.a7_fusion.engine import peer_event_score
    from marketsense.db.models import Security

    with db_factory() as db:
        db.add(Security(symbol="PHARMA1", company_name="P1",
                        extra={"industry": "Pharmaceuticals"}))
        db.add(Security(symbol="PHARMA2", company_name="P2",
                        extra={"industry": "Pharmaceuticals"}))
        db.add(Security(symbol="STEELCO", company_name="S1",
                        extra={"industry": "Steel And Steel Products"}))
        f = Filing(feed="announcements", symbol="PHARMA2",
                   content_hash="a7peer".ljust(64, "0"), source="rss",
                   subject="USFDA warning letter",
                   event_at=NOW - timedelta(days=1))
        db.add(f)
        db.flush()
        db.add(FilingClassification(
            filing_id=f.id, category="regulatory_action", materiality=8,
            sentiment=-0.8, confidence=0.9, engine="rules",
            model_version=A2_V, event_at=NOW - timedelta(days=1)))
        db.commit()
        s1, c1, ev1 = peer_event_score(db, "PHARMA1", now=NOW)
        s2, c2, ev2 = peer_event_score(db, "STEELCO", now=NOW)
    assert s1 < 50 and c1 > 0            # negative peer event pushed down
    assert ev1[0]["peer"] == "PHARMA2"   # source named in evidence
    assert ev1[0]["industry"] == "Pharmaceuticals"
    assert c2 == 0 and s2 == 50          # different industry: untouched


def test_hysteresis_holds_small_moves(db_factory):
    with db_factory() as db:
        _full_stack(db, "HYSTCO")
        db.commit()
        r1 = issue_all(db_factory)
        assert r1["issued"] >= 1
        r2 = issue_all(db_factory)  # nothing changed
        held = r2["held_by_hysteresis"]
        assert held >= 1 and r2["issued"] == 0
        with db_factory() as db2:
            n = db2.scalar(select(func.count()).select_from(Signal)
                           .where(Signal.symbol == "HYSTCO"))
        assert n == 1  # no spam rows
    assert HYSTERESIS > 0