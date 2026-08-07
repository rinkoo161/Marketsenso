"""A5 engine over seeded fixtures: deal pressure, surveillance penalty,
promoter delta parse, renormalisation honesty."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from marketsense.agents.a5_flow.engine import _PR_GRP, compute_symbol
from marketsense.db.models import Filing, LargeDeal, PriceDaily, Surveillance

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _seed_prices(db, symbol, turnover=1000.0):
    for i in range(20):
        db.add(PriceDaily(symbol=symbol, trade_date=NOW - timedelta(days=i + 1),
                          close=100.0, turnover=turnover, source="bhavcopy"))


def test_promoter_regex():
    m = _PR_GRP.search("AS ON DATE : 30-Jun-2026 | PR_AND_PRGRP: 57.34 | PUBLIC_VAL: 42.66")
    assert float(m.group(1)) == 57.34


def test_buy_deals_lift_score_sell_deals_sink_it(db_factory):
    with db_factory() as db:
        for sym, side in (("BUYCO", "BUY"), ("SELLCO", "SELL")):
            _seed_prices(db, sym)
            db.add(LargeDeal(day=NOW - timedelta(days=3), symbol=sym,
                             client="Big Fund", side=side, qty=100000.0,
                             price=100.0, kind="bulk"))
        db.commit()
        buy = compute_symbol(db, "BUYCO", now=NOW)
        sell = compute_symbol(db, "SELLCO", now=NOW)
    assert buy["score"] > sell["score"]
    assert buy["components"]["deal_ratio"] > 0 > sell["components"]["deal_ratio"]


def test_gsm_floors_the_score(db_factory):
    with db_factory() as db:
        _seed_prices(db, "GSMCO")
        db.add(Surveillance(as_of=NOW, symbol="GSMCO", framework="gsm",
                            stage="II", detail="x"))
        db.commit()
        r = compute_symbol(db, "GSMCO", now=NOW)
    assert r["label"] == "surveillance"
    assert r["score"] < 40


def test_promoter_delta_from_filings(db_factory):
    with db_factory() as db:
        for i, pct in enumerate(("55.00", "57.50")):
            db.add(Filing(feed="shareholding_pattern", symbol="PROMCO",
                          content_hash=f"a5p{i}".ljust(64, "0"), source="rss",
                          subject=f"PR_AND_PRGRP: {pct} | PUBLIC_VAL: x",
                          event_at=NOW - timedelta(days=90 - i * 80)))
        db.commit()
        r = compute_symbol(db, "PROMCO", now=NOW)
    assert r["components"]["promoter_delta_pp"] == 2.5
    # +2.5pp promoter add = max promoter points → pushes score up
    assert r["score"] > 60


def test_unavailable_axes_are_named(db_factory):
    with db_factory() as db:
        db.add(Surveillance(as_of=NOW, symbol="PLAINCO", framework="asm_st",
                            stage="I", detail="x"))
        db.commit()
        r = compute_symbol(db, "PLAINCO", now=NOW)
    assert any("fno" in u for u in r["components"]["unavailable"])
    assert r["confidence"] < 0.5  # only insider+surveillance covered