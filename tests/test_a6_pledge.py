"""Promoter pledge %: SHP XBRL parse, A6 gating, sync dedup.

The XBRL snippet mirrors the live NITCO instance (fetched 2026-08-08):
percent facts are decimal fractions, the promoter aggregate lives in a
ShareholdingOfPromoterAndPromoterGroup context, and other holder
contexts must NOT leak into the promoter numbers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from marketsense.agents.a6_risk.engine import assess_symbol
from marketsense.agents.a6_risk.pledge import parse_shp_xbrl, sync_symbol
from marketsense.db.models import Filing, PriceDaily, PromoterPledge

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)

SHP_XML = """<xbrli:xbrl xmlns:in-bse-shp="http://example/shp">
<xbrli:context id="ShareholdingOfPromoterAndPromoterGroup_ContextI"><xbrli:entity>
<xbrldi:explicitMember dimension="in-bse-shp:d">in-bse-shp:ShareholdingOfPromoterAndPromoterGroupMember</xbrldi:explicitMember>
</xbrli:entity></xbrli:context>
<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares
  contextRef="ShareholdingOfPromoterAndPromoterGroup_ContextI"
  unitRef="pure" decimals="4">0.2227</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
<in-bse-shp:NumberOfSharesEncumbered
  contextRef="ShareholdingOfPromoterAndPromoterGroup_ContextI"
  unitRef="shares">32571028</in-bse-shp:NumberOfSharesEncumbered>
<in-bse-shp:EncumberedSharesHeldAsPercentageOfTotalNumberOfShares
  contextRef="ShareholdingOfPromoterAndPromoterGroup_ContextI"
  unitRef="pure">0.5919</in-bse-shp:EncumberedSharesHeldAsPercentageOfTotalNumberOfShares>
<in-bse-shp:EncumberedSharesHeldAsPercentageOfTotalNumberOfShares
  contextRef="OthersIndianShareholders_Context22"
  unitRef="pure">1</in-bse-shp:EncumberedSharesHeldAsPercentageOfTotalNumberOfShares>
</xbrli:xbrl>"""


def test_parse_promoter_context_only_fractions_scaled():
    r = parse_shp_xbrl(SHP_XML)
    assert r["promoter_pct"] == 22.27          # fraction ×100
    assert r["encumbered_pct"] == 59.19        # promoter ctx, NOT the 100%
    assert r["encumbered_shares"] == 32571028
    assert r["encumbered_pct_of_total"] == 13.18


def test_parse_without_promoter_context_is_none():
    assert parse_shp_xbrl("<xbrl><a:X contextRef='Other_C1'>5</a:X></xbrl>") is None


OLD_ERA_XML = """<xbrli:xbrl xmlns:in-bse-shp="http://example/shp">
<xbrli:context id="ShareholdingOfPromoterAndPromoterGroupI"><xbrli:entity>
<xbrldi:explicitMember dimension="in-bse-shp:d">in-bse-shp:ShareholdingOfPromoterAndPromoterGroupMember</xbrldi:explicitMember>
</xbrli:entity></xbrli:context>
<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares
  contextRef="ShareholdingOfPromoterAndPromoterGroupI">0.1637</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
<in-bse-shp:PledgedOrEncumberedSharesHeldAsPercentageOfTotalNumberOfShares
  contextRef="ShareholdingOfPromoterAndPromoterGroupI">0.6144</in-bse-shp:PledgedOrEncumberedSharesHeldAsPercentageOfTotalNumberOfShares>
</xbrli:xbrl>"""

AFFIRMED_ZERO_XML = """<xbrli:xbrl xmlns:in-bse-shp="http://example/shp">
<xbrli:context id="ShareholdingOfPromoterAndPromoterGroup_ContextI"><xbrli:entity>
<xbrldi:explicitMember dimension="in-bse-shp:d">in-bse-shp:ShareholdingOfPromoterAndPromoterGroupMember</xbrldi:explicitMember>
</xbrli:entity></xbrli:context>
<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares
  contextRef="ShareholdingOfPromoterAndPromoterGroup_ContextI">0.3509</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>
<in-bse-shp:WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged
  contextRef="C1">false</in-bse-shp:WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged>
</xbrli:xbrl>"""


def test_parse_old_era_pledged_element_name():
    # FEL 2023 shape: PledgedOrEncumbered..., context id without _Context
    r = parse_shp_xbrl(OLD_ERA_XML)
    assert r["promoter_pct"] == 16.37
    assert r["encumbered_pct"] == 61.44


def test_parse_omitted_facts_with_all_false_booleans_is_affirmed_zero():
    # DEVX shape: zero-pledge companies omit the facts; the booleans
    # affirm the zero. Omission WITHOUT the booleans must stay unknown.
    r = parse_shp_xbrl(AFFIRMED_ZERO_XML)
    assert r["encumbered_pct"] == 0.0
    r2 = parse_shp_xbrl(AFFIRMED_ZERO_XML.replace("false", "true"))
    assert r2["encumbered_pct"] is None


def _seed_prices(db, symbol):
    for i in range(60):
        db.add(PriceDaily(symbol=symbol, trade_date=NOW - timedelta(days=i + 1),
                          close=100.0, high=102.0, low=98.0,
                          turnover=1000.0, series="EQ", source="bhavcopy"))


def test_pledge_over_25pct_hard_blocks_with_stated_reason(db_factory):
    with db_factory() as db:
        _seed_prices(db, "PLEDGECO")
        db.add(PromoterPledge(symbol="PLEDGECO", record_id="r1",
                              shp_date=NOW - timedelta(days=30),
                              promoter_pct=22.27, encumbered_pct=59.19))
        db.commit()
        r = assess_symbol(db, "PLEDGECO", now=NOW)
    assert r["verdict"] == "hard_block"
    assert any("promoter pledge 59.2%" in b for b in r["hard_blocks"])
    assert r["checks"]["promoter_pledge_pct"] == 59.19
    # we HAVE the number now — the ledger must not claim otherwise
    assert not any("pledge_pct" in u for u in r["unavailable"])


def test_pledge_between_10_and_25_is_penalty_only(db_factory):
    with db_factory() as db:
        _seed_prices(db, "MIDPLEDGE")
        db.add(PromoterPledge(symbol="MIDPLEDGE", record_id="r1",
                              promoter_pct=50.0, encumbered_pct=18.0))
        db.commit()
        r = assess_symbol(db, "MIDPLEDGE", now=NOW)
    assert r["verdict"] == "penalty"
    assert any("promoter pledge 18.0%" in p for p in r["penalties"])


def test_no_row_still_names_pledge_unavailable(db_factory):
    with db_factory() as db:
        _seed_prices(db, "NOROWCO")
        db.commit()
        r = assess_symbol(db, "NOROWCO", now=NOW)
    assert r["verdict"] == "clear"
    assert any("pledge_pct" in u for u in r["unavailable"])


def test_activity_penalty_drops_ignorance_claim_when_pct_known(db_factory):
    with db_factory() as db:
        _seed_prices(db, "ACTCO")
        db.add(Filing(feed="encumbrance", symbol="ACTCO",
                      content_hash="a6act".ljust(64, "0"), source="rss"))
        db.add(PromoterPledge(symbol="ACTCO", record_id="r1",
                              promoter_pct=40.0, encumbered_pct=5.0))
        db.commit()
        r = assess_symbol(db, "ACTCO", now=NOW)
    acts = [p for p in r["penalties"] if "activity" in p]
    assert acts and "unavailable" not in acts[0]


class _StubClient:
    """Two-call client: master JSON then XBRL bytes."""

    def __init__(self, rows, xml):
        self.rows, self.xml = rows, xml
        self.calls = 0

    def get_json(self, url):
        self.calls += 1
        return {"data": self.rows}

    def get(self, url):
        self.calls += 1

        class R:
            content = self.xml.encode()
        return R()


def test_sync_stores_once_and_dedups_by_record_id(db_factory):
    rows = [{"recordId": "213025", "broadcastDate": "07-AUG-2026 18:59:47",
             "date": "23-JUL-2026", "xbrl": "https://x/shp.xml"}]
    client = _StubClient(rows, SHP_XML)
    with db_factory() as db:
        assert sync_symbol(db, client, "SYNCCO") == "stored"
        db.commit()
        assert sync_symbol(db, client, "SYNCCO") == "dup"  # no re-fetch of XBRL
        db.commit()
        n = db.scalar(select(func.count()).select_from(PromoterPledge))
        row = db.scalars(select(PromoterPledge)).first()
    assert n == 1
    assert row.encumbered_pct == 59.19
    assert row.xbrl_url == "https://x/shp.xml"          # evidence stored
    assert row.shp_date.date().isoformat() == "2026-07-23"


def test_sync_no_submissions_writes_sentinel_not_retry_bait(db_factory):
    client = _StubClient([], "")
    with db_factory() as db:
        assert sync_symbol(db, client, "NEWCO") == "no_shp"
        db.commit()
        row = db.scalars(select(PromoterPledge)).first()
    assert row.encumbered_pct is None                   # sentinel
    # and A6 must treat a sentinel as ignorance, not as 0% pledge
    with db_factory() as db:
        _seed_prices(db, "NEWCO")
        db.commit()
        r = assess_symbol(db, "NEWCO", now=NOW)
    assert any("pledge_pct" in u for u in r["unavailable"])
