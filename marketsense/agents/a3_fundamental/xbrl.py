"""SEBI IndAS results XBRL → normalised fact dict.

Structure verified against the live corpus (2026-08-08, SURANAT&P Q1
instance): the CURRENT reporting period's facts share one duration
context (conventionally 'OneD'), identified robustly as the contextRef
of the DateOfEndOfReportingPeriod fact — never by hardcoded id. Values
are plain RUPEES (no scale factor in these instances). Segment and OCI
detail live in other contexts and stay in `raw` only if wanted later.

defusedxml everywhere — filings are untrusted input.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import defusedxml.ElementTree as ET

from marketsense.core.logging import get_logger

log = get_logger("a3.xbrl")

XBRLI = "{http://www.xbrl.org/2003/instance}"

# XBRL fact name -> FinancialsQuarterly column
FACT_MAP = {
    "RevenueFromOperations": "revenue",
    "OtherIncome": "other_income",
    "Income": "total_income",
    "Expenses": "expenses",
    "FinanceCosts": "finance_costs",
    "DepreciationDepletionAndAmortisationExpense": "depreciation",
    "ProfitBeforeTax": "pbt",
    "TaxExpense": "tax",
    "ProfitLossForPeriod": "pat",
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_basic",
}

META_FACTS = {
    "Symbol", "ScripCode", "DateOfStartOfReportingPeriod",
    "DateOfEndOfReportingPeriod", "NatureOfReportStandaloneConsolidated",
    "WhetherResultsAreAuditedOrUnaudited", "TypeOfReportingPeriod",
}


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def parse_instance(path: str | Path) -> dict | None:
    """One XBRL instance → {symbol, period_start, period_end, basis,
    audited, quarterly, values{col: float}, raw{fact: text}} or None when
    the file is not a results instance (many XBRLs on disk are RPT/BRSR/
    meeting schedules — cheaply skipped by the missing period fact)."""
    try:
        root = ET.parse(str(path)).getroot()
    except Exception as e:
        log.warning("xbrl_parse_failed", path=str(path), error=str(e)[:120])
        return None

    facts: list[tuple[str, str | None, str]] = []  # (name, ctx, text)
    for el in root.iter():
        if el.text and el.text.strip() and el.get("contextRef"):
            facts.append((_local(el.tag), el.get("contextRef"), el.text.strip()))

    main_ctx = next((ctx for name, ctx, _ in facts
                     if name == "DateOfEndOfReportingPeriod"), None)
    if main_ctx is None:
        return None  # not a results instance

    main = {name: text for name, ctx, text in facts if ctx == main_ctx}
    if "ProfitLossForPeriod" not in main and "RevenueFromOperations" not in main:
        return None  # results shell without a P&L (e.g. audit-qualification file)

    def _date(s: str | None) -> datetime | None:
        try:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    values: dict[str, float] = {}
    for fact, col in FACT_MAP.items():
        v = main.get(fact)
        if v is not None:
            try:
                values[col] = float(v)
            except ValueError:
                pass

    basis_raw = (main.get("NatureOfReportStandaloneConsolidated") or "").lower()
    start = _date(main.get("DateOfStartOfReportingPeriod"))
    end = _date(main.get("DateOfEndOfReportingPeriod"))
    # Quarterly-ness from DATE MATH, not the label: Sep-30 filings labelled
    # "Quarterly" routinely carry Apr-Sep H1 cumulatives (reconciliation
    # gate 2026-08-08: IRFC/VEDL/OIL/CGPOWER all ~2x on Sep quarters).
    period_days = (end - start).days if (start and end) else None
    is_quarter = period_days is not None and 80 <= period_days <= 100
    return {
        "symbol": (main.get("Symbol") or "").strip().upper() or None,
        "period_start": start,
        "period_end": end,
        "period_days": period_days,
        "basis": "consolidated" if "consolidated" in basis_raw and "non" not in basis_raw
                 else "standalone",
        "audited": (main.get("WhetherResultsAreAuditedOrUnaudited") or ""
                    ).lower().startswith("audited"),
        "quarterly": is_quarter,
        "values": values,
        # every main-context fact survives, per §10 traceability
        "raw": {k: v for k, v in main.items()},
    }
