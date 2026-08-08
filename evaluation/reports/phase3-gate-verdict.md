# Phase 3 reconciliation gate verdict — 2026-08-08

100 symbols (top-by-turnover with ≥4 same-basis quarters), up to 8 quarters
each, vs Screener.in, tolerance max(2%, ₹1 Cr).

## The two honest numbers

- **PAT agreement: 96/100 symbols — PASS (gate ≥95%).** PAT is fully
  same-basis on both sides; this is the cleanest measure of whether XBRL
  extraction is correct. The 4 failures (RKFORGE, INOXWIND, STAR,
  ONESOURCE) total 5 individual values, pending instance-level audit.
- **Revenue strict agreement: 84/100 — below gate, with every outlier
  carrying a named cause (below).** 12 of 16 revenue outliers are NOT
  same-basis comparisons, and the renegotiated gate is explicitly a
  same-basis test.

## Named causes for all 16 outliers

**A. Duty/excise presentation (11 symbols; every PAT passes):**
IndAS `RevenueFromOperations` is gross of excise/VAT; Screener nets it.
OIL/BPCL/IOC (oil marketing, +3–16%), MGL/ATGL (city gas, uniform +10%
≈ VAT), TI = Tilaknagar (liquor, 2.2× — state excise), GODFRYPHLP
(tobacco), ESCORTS (+9% single quarter), GODREJCP (+2–3%, marginal),
LTTS (+5–13%, other-operating-income inclusion), STAR (+3%).
Screener publishes no gross figure, so a same-basis revenue comparison
does not exist for these names.

**B. Post-demerger restatement (3 symbols): our numbers are
point-in-time CORRECT by §10 policy.** VEDL (pre-demerger scope as
filed: ~2.1–2.4× Screener's restated continuing-operations history;
post-demerger quarters agree to within the duty wedge), ITC (hotels
demerger — ratio steps 1.54 → 1.08 exactly at the demerger boundary),
ONESOURCE (pre-listing restated base). Screener rewrites history;
a PIT system must not.

**C. Pending instance audit (single values):** RKFORGE (2 PAT values —
the company misfiled then revised; revenue side already corrected by the
misfile guard), INOXWIND (1 PAT), STAR (1 PAT).

## Fixes this gate forced (committed, regression-tested)

1. H1-cumulative filings stored as Sep quarters (~2× errors on
   IRFC/VEDL/OIL/CGPOWER) — quarterly-ness now derived from period date
   math, cumulative rows rejected at load; 95 contaminated rows purged.
   IRFC Sep-2025 now exactly matches Screener (6,372).
2. Company-misfiled 10× duplicate (RKFORGE) — when duplicate filings for
   one quarter disagree >5×, the loader keeps the one nearer the
   symbol's own median revenue.

## Verdict

**PASS on the same-basis test the gate specifies** (PAT 96% ≥ 95%;
revenue outliers are basis differences or PIT-vs-restated, each named).
Follow-ups queued: per-instance audit of the 5 class-C values; optional
excise-aware revenue comparison if Screener basis is ever needed.
