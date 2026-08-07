#!/usr/bin/env python3
"""Build the Phase 2 held-out evaluation set.

Stratified sample of 100 filings → evaluation/eval_set.csv with EMPTY
label columns. THE USER fills label_category and label_materiality by
hand; those labels are the gate (≥85% category accuracy, ≥0.7 materiality
correlation) and must never be used to tune prompts or rules — held out
means held out.

Stratification: the corpus is ~60% routine noise; a uniform sample would
grade mostly NAV declarations and prove nothing. So: 30 routine-suspect,
50 signal-suspect (rule-classified non-routine), 20 no-rule residue (the
LLM's hardest cases).

Run:  .venv/bin/python evaluation/build_eval_set.py
Then: hand-edit eval_set.csv, then run evaluation/score_eval.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from marketsense.db.engine import session  # noqa: E402

OUT = Path(__file__).parent / "eval_set.csv"

# setseed makes the sample reproducible — the set must not drift between
# the labelling session and the scoring run.
QUERY = """
select * from (
  (select f.id, f.feed, f.symbol, f.subject, left(f.description, 400) descr
   from filings f join filing_classifications c on c.filing_id = f.id
   where c.routine order by f.id % 997 limit 30)
  union all
  (select f.id, f.feed, f.symbol, f.subject, left(f.description, 400)
   from filings f join filing_classifications c on c.filing_id = f.id
   where not c.routine and c.engine = 'rules' order by f.id % 991 limit 50)
  union all
  (select f.id, f.feed, f.symbol, f.subject, left(f.description, 400)
   from filings f join filing_classifications c on c.filing_id = f.id
   where c.engine in ('local','online') order by f.id % 983 limit 20)
) s order by id
"""


def main() -> None:
    if OUT.exists():
        print(f"refusing to overwrite {OUT} — the eval set must stay fixed.\n"
              "Delete it explicitly if you intend to rebuild (labels will be lost).")
        sys.exit(1)
    with session() as db:
        db.execute(text("select setseed(0.42)"))
        rows = db.execute(text(QUERY)).all()
    with OUT.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["filing_id", "feed", "symbol", "subject", "description",
                    "label_category", "label_materiality"])
        for r in rows:
            w.writerow([r[0], r[1], r[2] or "", (r[3] or "")[:200],
                        (r[4] or "").replace("\n", " "), "", ""])
    print(f"wrote {len(rows)} rows to {OUT}")
    print("Fill label_category (see taxonomy.CATEGORIES) and label_materiality (0-10).")


if __name__ == "__main__":
    main()
