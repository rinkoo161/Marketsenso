"""A6 veto battery over seeded fixtures — each hard block and penalty
class, plus the stated-reason invariant."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketsense.agents.a6_risk.engine import assess_symbol
from marketsense.db.models import (
    Filing,
    FilingClassification,
    PriceDaily,
    Surveillance,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _seed_prices(db, symbol, close=100.0, turnover=1000.0, series="EQ",
                 locked_days=0, n=60):
    for i in range(n):
        locked = i < locked_days
        db.add(PriceDaily(
            symbol=symbol, trade_date=NOW - timedelta(days=i + 1),
            close=close, high=close if locked else close * 1.02,
            low=close if locked else close * 0.98,
            turnover=turnover, series=series, source="bhavcopy"))


def test_clear_symbol(db_factory):
    with db_factory() as db:
        _seed_prices(db, "CLEANCO")
        db.commit()
        r = assess_symbol(db, "CLEANCO", now=NOW)
    assert r["verdict"] == "clear" and r["score"] == 100.0
    assert r["unavailable"]  # the honesty ledger is always present


def test_gsm_is_hard_block_with_reason(db_factory):
    with db_factory() as db:
        _seed_prices(db, "GSMCO")
        db.add(Surveillance(as_of=NOW, symbol="GSMCO", framework="gsm",
                            stage="III", detail="x"))
        db.commit()
        r = assess_symbol(db, "GSMCO", now=NOW)
    assert r["verdict"] == "hard_block" and r["score"] == 0.0
    assert any("GSM" in b for b in r["hard_blocks"])  # stated reason


def test_illiquidity_and_penny_hard_block(db_factory):
    with db_factory() as db:
        _seed_prices(db, "THINCO", turnover=2.0)
        _seed_prices(db, "PENNYCO", close=3.0)
        db.commit()
        thin = assess_symbol(db, "THINCO", now=NOW)
        penny = assess_symbol(db, "PENNYCO", now=NOW)
    assert any("illiquid" in b for b in thin["hard_blocks"])
    assert any("penny" in b for b in penny["hard_blocks"])


def test_circuit_lock_frequency(db_factory):
    with db_factory() as db:
        _seed_prices(db, "LOCKCO", locked_days=20)  # 20/60 = 33% > 20%
        db.commit()
        r = assess_symbol(db, "LOCKCO", now=NOW)
    assert any("circuit-locked" in b for b in r["hard_blocks"])


def test_auditor_resignation_hard_blocks_a_year(db_factory):
    with db_factory() as db:
        _seed_prices(db, "AUDCO")
        f = Filing(feed="announcements", symbol="AUDCO",
                   content_hash="a6aud".ljust(64, "0"), source="rss")
        db.add(f)
        db.flush()
        db.add(FilingClassification(
            filing_id=f.id, category="auditor_resignation", materiality=9,
            sentiment=-0.9, confidence=0.95, engine="rules",
            model_version="test", event_at=NOW - timedelta(days=100)))
        db.commit()
        r = assess_symbol(db, "AUDCO", now=NOW)
    assert r["verdict"] == "hard_block"
    assert any("auditor" in b for b in r["hard_blocks"])


def test_penalties_stack_but_do_not_block(db_factory):
    with db_factory() as db:
        _seed_prices(db, "PENCO", series="BE")
        db.add(Surveillance(as_of=NOW, symbol="PENCO", framework="asm_st",
                            stage="Stage I", detail="x"))
        db.add(Filing(feed="encumbrance", symbol="PENCO",
                      content_hash="a6pen".ljust(64, "0"), source="rss"))
        db.commit()
        r = assess_symbol(db, "PENCO", now=NOW)
    assert r["verdict"] == "penalty"
    assert len(r["penalties"]) == 3       # ASM + BE series + pledge activity
    assert r["score"] == 55.0             # 100 - 3×15
    assert any("pledge % unavailable" in p for p in r["penalties"])