"""A3: parser on a minimal real-shaped instance; engine on synthetic
quarter series exercising growth, flags, and the honesty rules."""
from __future__ import annotations

from datetime import datetime, timezone

from marketsense.agents.a3_fundamental.engine import compute
from marketsense.agents.a3_fundamental.xbrl import parse_instance

# Minimal instance mirroring the live SEBI IndAS shape (SURANAT&P corpus
# file): main duration context OneD, meta + P&L facts keyed to it.
INSTANCE = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt">
 <xbrli:context id="OneD">
  <xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate>
  <xbrli:endDate>2026-06-30</xbrli:endDate></xbrli:period>
 </xbrli:context>
 <xbrli:context id="PrevD">
  <xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate>
  <xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
 </xbrli:context>
 <in-capmkt:Symbol contextRef="OneD">TESTCO</in-capmkt:Symbol>
 <in-capmkt:DateOfStartOfReportingPeriod contextRef="OneD">2026-04-01</in-capmkt:DateOfStartOfReportingPeriod>
 <in-capmkt:DateOfEndOfReportingPeriod contextRef="OneD">2026-06-30</in-capmkt:DateOfEndOfReportingPeriod>
 <in-capmkt:NatureOfReportStandaloneConsolidated contextRef="OneD">Consolidated</in-capmkt:NatureOfReportStandaloneConsolidated>
 <in-capmkt:WhetherResultsAreAuditedOrUnaudited contextRef="OneD">Unaudited</in-capmkt:WhetherResultsAreAuditedOrUnaudited>
 <in-capmkt:TypeOfReportingPeriod contextRef="OneD">Quarterly</in-capmkt:TypeOfReportingPeriod>
 <in-capmkt:RevenueFromOperations contextRef="OneD">1000000</in-capmkt:RevenueFromOperations>
 <in-capmkt:RevenueFromOperations contextRef="PrevD">900000</in-capmkt:RevenueFromOperations>
 <in-capmkt:ProfitBeforeTax contextRef="OneD">200000</in-capmkt:ProfitBeforeTax>
 <in-capmkt:TaxExpense contextRef="OneD">50000</in-capmkt:TaxExpense>
 <in-capmkt:ProfitLossForPeriod contextRef="OneD">150000</in-capmkt:ProfitLossForPeriod>
</xbrli:xbrl>"""


def test_parse_instance_main_context_only(tmp_path):
    p = tmp_path / "x.xml"
    p.write_text(INSTANCE)
    inst = parse_instance(p)
    assert inst["symbol"] == "TESTCO"
    assert inst["basis"] == "consolidated"
    assert inst["quarterly"] and not inst["audited"]
    assert inst["values"]["revenue"] == 1000000.0  # OneD, NOT the PrevD 900000
    assert inst["values"]["pat"] == 150000.0
    assert inst["period_end"].date().isoformat() == "2026-06-30"


def test_parse_rejects_non_results(tmp_path):
    p = tmp_path / "y.xml"
    p.write_text("<?xml version='1.0'?><root><a>meeting schedule</a></root>")
    assert parse_instance(p) is None


class Q:
    """FinancialsQuarterly stand-in."""

    def __init__(self, revenue=None, expenses=None, other_income=None,
                 finance_costs=0.0, depreciation=0.0, pbt=None, tax=None,
                 pat=None, basis="consolidated", raw=None, filing_id=None):
        self.revenue, self.expenses = revenue, expenses
        self.other_income, self.finance_costs = other_income, finance_costs
        self.depreciation, self.pbt, self.tax, self.pat = depreciation, pbt, tax, pat
        self.basis, self.raw, self.filing_id = basis, raw, filing_id
        self.period_end = datetime(2026, 6, 30, tzinfo=timezone.utc)
        self.security_id = None


def _clean_q(rev):
    return Q(revenue=rev, expenses=rev * 0.8, other_income=rev * 0.01,
             pbt=rev * 0.2, tax=rev * 0.05, pat=rev * 0.15)


def test_single_quarter_no_growth_low_confidence():
    r = compute([_clean_q(1000.0)])
    assert r["components"]["growth_pts"] is None      # absent, not neutral
    assert r["components"]["weight_covered"] == 60.0  # renormalised
    assert r["confidence"] < 0.1
    assert "cfo_vs_pat (needs cash flow)" in r["components"]["unavailable"][0]


def test_growth_rewards_growers():
    growing = [_clean_q(100.0 * 1.08 ** i) for i in range(8)]
    shrinking = [_clean_q(100.0 * 0.92 ** i) for i in range(8)]
    g, s = compute(growing), compute(shrinking)
    assert g["score"] > s["score"]
    assert g["components"]["rev_yoy"] > 0 > s["components"]["rev_yoy"]
    assert g["confidence"] > 0.5  # 8 quarters, full coverage


def test_flags_fire_and_cost_points():
    bad = Q(revenue=1000.0, expenses=1100.0, other_income=300.0,
            finance_costs=200.0, pbt=100.0, tax=1.0, pat=-50.0)
    r = compute([bad])
    flags = r["components"]["flags"]
    assert any("other_income" in f for f in flags)
    assert any("tax rate" in f for f in flags)
    assert any("finance costs" in f for f in flags)
    assert any("loss-making" in f for f in flags)
    assert r["label"] == "flagged"
    clean = compute([_clean_q(1000.0)])
    assert clean["components"]["quality_pts"] > r["components"]["quality_pts"]


def test_negative_base_growth_is_none_not_garbage():
    q1 = _clean_q(100.0)
    q1.pat = -10.0
    q2 = _clean_q(100.0)
    q2.pat = 5.0
    r = compute([q1, q2])
    assert r["components"]["pat_qoq"] is None  # -10 → 5 is not "-150% growth"
