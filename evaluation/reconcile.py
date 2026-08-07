#!/usr/bin/env python3
"""Phase 3 reconciliation gate (renegotiated wording, user-accepted):
≥95% of compared symbols same-basis within 2% on revenue AND PAT, every
outlier named with values and a cause where identifiable.

Same-basis: our consolidated quarters vs Screener /consolidated/;
standalone-only filers vs Screener standalone. Tolerance is
max(2%, ₹1 Cr) — Screener rounds to integer crores, so pure-percentage
tolerance would fail small companies on rounding alone.

Run: .venv/bin/python evaluation/reconcile.py [max_symbols]
Writes evaluation/reports/reconciliation-<date>.md
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from marketsense.core.logging import get_logger, setup_logging  # noqa: E402
from marketsense.db.engine import session  # noqa: E402
from marketsense.db.models import FinancialsQuarterly  # noqa: E402
from marketsense.integrations.screener import fetch_quarterlies  # noqa: E402

log = get_logger("reconcile")
REPORTS = Path(__file__).parent / "reports"

TOL_PCT = 0.02
TOL_ABS_CR = 1.0
MIN_QUARTERS = 4


def within(ours_cr: float, theirs_cr: float) -> bool:
    return abs(ours_cr - theirs_cr) <= max(TOL_PCT * abs(theirs_cr), TOL_ABS_CR)


def main(max_symbols: int = 100) -> None:
    setup_logging("WARNING")
    with session() as db:
        candidates = db.execute(
            select(FinancialsQuarterly.symbol, FinancialsQuarterly.basis,
                   func.count())
            .where(FinancialsQuarterly.revenue.isnot(None))
            .group_by(FinancialsQuarterly.symbol, FinancialsQuarterly.basis)
            .having(func.count() >= MIN_QUARTERS)
            .order_by(func.count().desc())
        ).all()
    # prefer consolidated series per symbol
    chosen: dict[str, str] = {}
    for sym, basis, _n in candidates:
        if sym not in chosen or basis == "consolidated":
            chosen[sym] = basis
    symbols = list(chosen.items())[:max_symbols]
    if not symbols:
        print("no symbols with enough quarters yet — wait for the backfill")
        sys.exit(1)

    passed, outliers, skipped = [], [], []
    for sym, basis in symbols:
        with session() as db:
            ours = db.scalars(
                select(FinancialsQuarterly)
                .where(FinancialsQuarterly.symbol == sym,
                       FinancialsQuarterly.basis == basis,
                       FinancialsQuarterly.revenue.isnot(None))
                .order_by(FinancialsQuarterly.period_end.desc()).limit(8)).all()
        theirs = fetch_quarterlies(sym, consolidated=(basis == "consolidated"))
        if not theirs:
            skipped.append((sym, "not on screener / no table"))
            continue
        compared = 0
        bad: list[str] = []
        for q in ours:
            # match on date only (tz-safe)
            t = next((v for k, v in theirs.items()
                      if k.date() == q.period_end.date()), None)
            if t is None:
                continue
            for field, col in (("revenue_cr", "revenue"), ("pat_cr", "pat")):
                their_v = t.get(field)
                our_v = getattr(q, col)
                if their_v is None or our_v is None:
                    continue
                our_cr = our_v / 1e7
                compared += 1
                if not within(our_cr, their_v):
                    bad.append(f"{q.period_end.date()} {col}: "
                               f"ours {our_cr:,.0f} vs screener {their_v:,.0f}")
        if compared == 0:
            skipped.append((sym, "no overlapping periods"))
        elif bad:
            outliers.append((sym, basis, bad))
        else:
            passed.append((sym, compared))
        print(f"{sym:14} {basis:12} "
              f"{'SKIP' if compared == 0 else 'FAIL' if bad else 'ok':5} "
              f"({compared} values)")

    total = len(passed) + len(outliers)
    pct = 100.0 * len(passed) / total if total else 0.0
    verdict = "PASS" if pct >= 95.0 else "FAIL"

    REPORTS.mkdir(exist_ok=True)
    report = REPORTS / f"reconciliation-{date.today()}.md"
    with report.open("w") as fh:
        fh.write(
            f"# Reconciliation vs Screener.in — {date.today()}\n\n"
            f"Compared: {total} symbols (same-basis, up to 8 quarters, "
            f"tolerance max(2%, ₹1 Cr))\n"
            f"Within tolerance: {len(passed)} ({pct:.1f}%) · gate ≥95% → "
            f"**{verdict}**\nSkipped (no data): {len(skipped)}\n\n## Outliers\n\n")
        for sym, basis, bad in outliers:
            fh.write(f"### {sym} ({basis})\n" +
                     "".join(f"- {b}\n" for b in bad) + "\n")
        fh.write("\n## Skipped\n\n" +
                 "".join(f"- {s}: {why}\n" for s, why in skipped))
    print(f"\n{pct:.1f}% within tolerance ({len(passed)}/{total}) -> {verdict}")
    print(f"report: {report}")
    sys.exit(0 if verdict == "PASS" else 2)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 100)
