# Labelling guide — eval_set.csv

Fill the last two columns of every row. Your labels are the held-out truth
for the Phase 2 gate — label from the subject/description text alone, the
same information the classifier saw. Don't look up what the pipeline
predicted (that's the point).

## label_category — exactly one of these strings

| category | use for |
|---|---|
| `order_win` | contract/order/LoI received |
| `capex` | capital expenditure announced |
| `capacity_expansion` | new plant/facility/line, greenfield/brownfield |
| `ma` | merger, acquisition, amalgamation, takeover, slump sale |
| `demerger` | demerger, spin-off, scheme of arrangement (split) |
| `fundraise` | QIP, rights, preferential, warrants, public offer |
| `debt_raise` | NCDs, bonds, commercial paper, loans |
| `credit_rating_change` | any rating action, either direction |
| `results` | financial results (the results themselves, incl. outcome-of-board-meeting approving them) |
| `guidance` | forward guidance / outlook change |
| `dividend_bonus_split_buyback` | any of those four |
| `management_change` | MD/CEO/CFO/board appointments, resignations |
| `auditor_resignation` | auditor resigns or is removed |
| `regulatory_action` | SEBI/NCLT/ED/tax action against the company |
| `litigation` | court/arbitration matters |
| `insider_trade` | SAST/PIT disclosures, promoter buys/sells |
| `pledge_creation_release` | pledge/encumbrance created, invoked, released |
| `plant_shutdown` | operations halted/suspended |
| `fire_accident` | fire, accident, explosion |
| `clarification_to_rumour` | reply to exchange query / news clarification |
| `other` | everything else — NAV declarations, board-meeting *intimations*, newspaper ads, trading windows, periodic compliance (RPT/shareholding/BRSR/complaints), AGM/EGM notices |

## label_materiality — 0–10, "how much could this move the stock"

- **0–1** routine noise: NAV, trading window, newspaper ad, intimations,
  periodic compliance filings
- **2–3** mildly informative: small insider trades, debt issues in the
  normal course, minor management changes
- **4–6** market-relevant: results, dividends/buybacks, meaningful orders,
  fundraises, pledge changes, CFO/CEO change
- **7–8** significant: major M&A, rating downgrade, regulatory action,
  plant shutdown, big order relative to company size
- **9–10** thesis-changing: auditor resignation, fraud, going-concern,
  transformational M&A

Judge size RELATIVE to the company where the text allows; when the text
doesn't say, score the category's typical weight.

## When done

    .venv/bin/python evaluation/score_eval.py

Prints PASS/FAIL against the gate (≥85% category accuracy, ≥0.7 materiality
Spearman) and writes a misses report to evaluation/reports/.
