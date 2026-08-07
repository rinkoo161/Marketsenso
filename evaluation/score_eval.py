#!/usr/bin/env python3
"""Score A2 against the hand-labelled eval set.

Gate (brief §9 Phase 2): ≥85% category accuracy, ≥0.7 correlation on
materiality. Correlation is Spearman (rank) — materiality is ordinal and
the labels are human gut scores; Pearson would over-reward matching the
scale rather than the ordering. Hand-rolled, dependency-free.

Run after labelling:  .venv/bin/python evaluation/score_eval.py
Writes evaluation/reports/eval-<model_version>.md and prints the verdict.
"""
from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from marketsense.agents.a2_docintel.classifier import MODEL_VERSION  # noqa: E402
from marketsense.agents.a2_docintel.taxonomy import CATEGORIES  # noqa: E402
from marketsense.db.engine import session  # noqa: E402
from marketsense.db.models import FilingClassification  # noqa: E402

EVAL = Path(__file__).parent / "eval_set.csv"
REPORTS = Path(__file__).parent / "reports"


def spearman(xs: list[float], ys: list[float]) -> float:
    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):          # average ranks over ties
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


def main() -> None:
    rows = list(csv.DictReader(EVAL.open()))
    labelled = [r for r in rows if r["label_category"].strip()]
    if len(labelled) < len(rows):
        print(f"only {len(labelled)}/{len(rows)} rows labelled — label the rest first")
        if not labelled:
            sys.exit(1)

    bad = [r["label_category"] for r in labelled
           if r["label_category"].strip() not in CATEGORIES]
    if bad:
        print(f"unknown label categories: {sorted(set(bad))}")
        sys.exit(1)

    with session() as db:
        preds = {
            c.filing_id: c
            for c in db.scalars(
                select(FilingClassification).where(
                    FilingClassification.filing_id.in_(
                        [int(r["filing_id"]) for r in labelled]),
                    FilingClassification.model_version == MODEL_VERSION,
                )
            )
        }

    hits, misses, mat_pred, mat_true = 0, [], [], []
    for r in labelled:
        pred = preds.get(int(r["filing_id"]))
        if pred is None:
            misses.append((r["filing_id"], "UNCLASSIFIED", r["label_category"], ""))
            continue
        if pred.category == r["label_category"].strip():
            hits += 1
        else:
            misses.append((r["filing_id"], pred.category,
                           r["label_category"], (r["subject"] or "")[:60]))
        mat_pred.append(float(pred.materiality))
        mat_true.append(float(r["label_materiality"] or 0))

    n = len(labelled)
    acc = hits / n
    rho = spearman(mat_pred, mat_true) if len(mat_pred) > 2 else 0.0
    ok = acc >= 0.85 and rho >= 0.7

    REPORTS.mkdir(exist_ok=True)
    report = REPORTS / f"eval-{MODEL_VERSION}-{date.today()}.md"
    with report.open("w") as fh:
        fh.write(f"# A2 eval — {MODEL_VERSION} — {date.today()}\n\n"
                 f"Labelled: {n} · Category accuracy: **{acc:.1%}** (gate ≥85%)\n"
                 f"Materiality Spearman: **{rho:.3f}** (gate ≥0.7)\n"
                 f"Verdict: **{'PASS' if ok else 'FAIL'}**\n\n## Misses\n\n"
                 "| filing | predicted | label | subject |\n|---|---|---|---|\n")
        for m in misses:
            fh.write(f"| {m[0]} | {m[1]} | {m[2]} | {m[3]} |\n")

    print(f"accuracy={acc:.1%}  spearman={rho:.3f}  -> {'PASS' if ok else 'FAIL'}")
    print(f"report: {report}")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
