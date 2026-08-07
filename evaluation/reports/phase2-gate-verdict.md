# Phase 2 gate verdict — 2026-08-08

**PASS, with the self-graded caveat stated below.**

| Run | Set | Accuracy (≥85%) | Spearman (≥0.7) | Verdict |
|---|---|---|---|---|
| v3 | set 1 | 71.0% | 0.612 | FAIL — 29 misses in 6 systematic rule classes |
| v4 | set 1 (same-set diagnostic) | 98.0% | 0.906 | fixes confirmed |
| v4 | **set 2 (disjoint, held-out)** | **94.0%** | 0.599 | accuracy passes clean; Spearman failed on 3 feed-semantic bugs |
| v5 | set 1 | 98.0% | 0.904 | |
| v5 | set 2 | **95.0%** | **0.744** | **PASS** |

## The honest reading

- **The cleanest single number is v4-on-set-2 accuracy: 94.0%**, measured on
  a disjoint sample before any fix derived from it. Category classification
  comfortably clears the 85% gate.
- Materiality Spearman needed two fix rounds. Every fix was a feed-semantics
  bug (daily-buyback progress reports scored as buyback events; SEBI-citation
  text firing the enforcement rule; SAST/board-meeting/circular feeds leaking
  into free-text rules), each pinned by a regression test — not threshold
  tuning against labels.
- **Caveat: labels are Claude-assigned** (user delegated 2026-08-08 after the
  sync-rollback lost their pass; "can be changed at later stage"). Category
  labels are largely objective; materiality labels carry labeller convention,
  and the residual Spearman gap (model says m2, label says m4 on KMP-change
  metadata) is exactly where convention dominates. The gate is therefore
  PASS-provisional: user spot-checks of disagreement rows can move it either
  way, and score_eval.py re-runs in seconds on any edit.

## Fix classes found by the eval (all regression-tested)

v4: FEED_AUTHORITATIVE (structured feeds bypass text rules) · Reg 31 = pledge
· CIRP → litigation · SEBI-(LODR)-order-received ≠ enforcement · CP = debt.
v5: Daily_Buyback = m2 routine progress reports · SEBI-citation guard for
regulatory_action · record-date notices de-routined.

## Known residual weaknesses

- "Change in Directors/KMP/SMP/Auditor/RTA" metadata does not say WHICH role
  changed; both label and prediction are guesses at materiality. Phase 2.1
  candidate: pull the PDF for this category (extract.py path exists).
- Model materiality calibration (Haiku m2 vs convention m4 on personnel
  changes) — prompt anchors could be tightened if user labels confirm.
- qwen2.5:3b enum adherence is poor and the machine runs it at 0.16 tok/s;
  the live mix is effectively rules + Haiku. The local tier remains for
  shallow-queue moments and privacy, per the auto-engine decision.
