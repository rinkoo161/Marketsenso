# ROADMAP

Repo: https://github.com/rinkoo161/Marketsenso · local: `Stock Tools/MakretSenso`
Build brief: `~/Downloads/nse_market_intelligence_build_prompt.md` (§ numbers
below refer to it). This file is the cross-session source of truth — append a
section per release; update PENDING WORK as part of the change, not after.

## PENDING WORK

**Phase 1 — COMPLETE.** 48h soak PASSED 2026-08-07 13:05 IST (56h+ continuous
at report time). Evidence: `docs/phase1-evidence.md` — 0 dupes / 0 auth
failures / 100% coverage both full days / 0 agent-run errors across 48k runs.
The ingest + API processes stay running as the production baseline.
- Backfill currently covers the announcements API only. Board meetings /
  corporate actions / shareholding JSON APIs are enrichment, not gaps — RSS
  covers them live; extend `backfill.py` per-endpoint as Phase 3 needs them.

**Phase 2 — GATE PASSED 2026-08-08 (provisional).** v5: set-1 98%/0.904,
disjoint set-2 95%/0.744 (cleanest held-out number: 94% accuracy on set 2
before any set-2-driven fix). Full verdict + caveats:
`evaluation/reports/phase2-gate-verdict.md`. Labels are Claude-assigned
(user-delegated; revisable — any edit to eval_set*.csv re-scores via
`score_eval.py [csv]`). Residuals: KMP-change metadata needs PDF pull for
materiality; Haiku materiality calibration on personnel changes; local
qwen tier is effectively decorative at 0.16 tok/s on this hardware.
Phase 3 (A3 fundamentals / A4 technicals / A5 flow) is clear to start.

**Phase 2 build history (superseded):**
Shipped: taxonomy + rule layer with hard floors, Ollama-first strict-JSON
classifier (qwen2.5:3b) with Claude fallback + rules-only degradation, bus
consumer at ≤200/cycle draining the 12.7k backlog live, PDF extraction on the
thin-metadata path, eval harness. Remaining to close the phase gate:
- Backlog classification completing in the background (check:
  `select count(*) from filing_classifications`).
- **USER ACTION: label the eval set.** Once the backlog is done, run
  `.venv/bin/python evaluation/build_eval_set.py`, hand-fill the two label
  columns in `evaluation/eval_set.csv` (categories from taxonomy.CATEGORIES,
  materiality 0-10), then `.venv/bin/python evaluation/score_eval.py`.
  Gate: ≥85% category accuracy, ≥0.7 materiality Spearman. If qwen2.5:3b
  misses it, that is data — revisit the provider decision with numbers.
- OCR is disabled until `brew install tesseract` (scans degrade gracefully).
- Watch-item from first live batch: 'ma' category count looks high — check
  for over-matching of routine "scheme of arrangement" mentions during eval.

**Phase 4 progress (2026-08-08):** A6 shipped (2,494 assessed / 237
hard-blocked, stated reasons, honesty ledger). A7 shipped (483 signals,
7 versioned profiles, deterministic evidence-traced theses, hysteresis;
suppressed-by-A6 works). Walk-forward backtest harness shipped
(pit_quality=reconstructed on its face; flow/surveillance honestly not
replayable). Interim result (44 obs, 22 rebalances, small-n): buy stance
+3.65% 12w excess vs Nifty 500; accumulate band (60-70 conviction)
consistently NEGATIVE — hypothesis: threshold noise; re-examine stance
bands after the full run. Full run + reconciliation gate + A3 re-score
queued on the finhist backfill (~08:30 IST ETA, 105/500 done, 0 errors).

**Phase 3 — Analysis (A3 fundamentals / A4 technicals / A5 flow)**
- A3: XBRL parsing from result filings (Document rows with .xml already being
  archived by A1 — the corpus is accumulating now). Rolling 8+ quarter /
  5+ year history; growth, quality (CFO/EBITDA <0.6 flag, CFO-vs-PAT
  divergence, receivable-days spikes), balance sheet, red-flag battery
  (Beneish M, Altman Z, Piotroski F) → Forensic Score 0–100; valuation vs own
  5Y and sector medians + DCF/reverse-DCF → `fundamental.updated`.
- A4: daily bhavcopy + `sec_bhavdata_full` (delivery %) from the archives host
  (static files — survives JSON-API blocks). 5y price backfill via **Kite
  historical API (confirmed available)**. Trend structure, RSI/ADX/ATR, RS vs
  Nifty 500 + sector, volume z-score, delivery trend, S/R →
  `technical.updated`.
- A5: bulk/block deals, FII/DII, shareholding deltas, insider aggregation,
  F&O OI/basis/PCR/IV-rank (reuse the instrument_registry pattern from
  ltp-monitor for the F&O universe), ASM/GSM/surveillance lists →
  `flow.updated`.
- New tables follow the PIT rule (event_at + observed_at + model_version) —
  `test_pit.py::test_every_fact_table_has_observed_at` will enforce it.
- Acceptance (renegotiated from the brief, user-accepted): ≥95% of Nifty 500
  within 2% on SAME-BASIS revenue/PAT vs Screener.in, every outlier explained
  by a named cause. Coverage tiers per §4: T0 portfolio/watchlist · T1 F&O+
  Nifty200 · T2 Nifty500 · T3 event-triggered only.

**Phase 4 — Fusion (A6 risk / A7 conviction)**
- A6 veto layer: hard blocks (ASM/GSM stage, pledge >25%, auditor resignation
  <4q, illiquidity, going-concern) — vetoes carry stated reasons.
- A7: Conviction 0–100, versioned weight profiles (default F30/T25/Fl20/E25),
  stance + entry/target/invalidation + vol-adjusted size, 3-for/3-against
  thesis where every number joins back to a stored record (LLM writes prose
  around computed values, never produces values — §10). Hysteresis band on
  re-issue to prevent alert spam.
- Walk-forward backtest through the PIT layer only. Windows labelled by
  pit_quality: `observed` (rows we ingested live — accumulating since
  2026-08-05) vs `reconstructed` (backfilled; visibility inferred from
  event_at). A backtest on reconstructed data is a hypothesis, and the report
  must say so on its face. Baseline: Nifty 500 buy-and-hold.
- Forward-return scoring harness should start recording as soon as any score
  exists — do not wait for A7.

**Phase 5 — Delivery & integration**
- A8 alerts: Telegram/email/webhook, pre-open 08:15 / mid-day / post-close
  16:30 / Sunday digests, severity routing, every alert links to evidence.
- LTP Monitor integration (user-confirmed read-only): MarketSense REST on
  :8100 is already safe to poll (no NSEClient in the API process). Their side
  gets ONE additive poller agent writing bus keys `ms_event_flag:{SYM}`,
  `ms_risk_flag:{SYM}`, `ms_levels:{SYM}`; register new config keys in
  `config.DEFAULTS` (their save() drops unregistered keys); run their full
  test suite before/after (their tests assert on source text — additive only).
  Their inbound contract: `GET :8000/api/ltp-monitor`.
- React dashboard (Market Pulse, Signals, Deep Dive, Screener, Portfolio,
  Agent Health, Signal Performance — §7). Signal-performance page is a §10
  requirement, not polish.

## OPERATIONS CHEAT-SHEET (future sessions start here)

- Processes: `nohup .venv/bin/ms run > logs/soak-ingest.log 2>&1 &` (ingest),
  `nohup .venv/bin/ms serve > logs/soak-api.log 2>&1 &` (API :8100).
  Stop: `pkill -f "ms run"` / `pkill -f "ms serve"`.
- Postgres 16 via `brew services start postgresql@16`; DBs `marketsense` +
  `marketsense_test`; no Docker on this machine. Migrations: `alembic
  upgrade head`. Tests: `.venv/bin/pytest -q` (35 passing as of v0.1.1).
- Ops introspection: `ms feeds` · `ms budget` · `ms verify [--day]` ·
  `ms stats` via `curl :8100/api/stats` · Prometheus at `:8100/metrics`.
- Files on disk: PDFs/XBRL at `~/.marketsense/documents/{sha[:2]}/{sha}.ext`
  (content-addressed — A2's cache key).
- Read CLAUDE.md's "Live-verified NSE facts" before touching feed parsing —
  each entry is a trap someone already fell into.

## v0.1.1 — soak-found fixes + holiday learning (2026-08-05)

The first 25 minutes of the soak surfaced a real bug, which is the point of
soaking before accepting:

- **404 head-of-line block.** A dead attachment URL
  (`CP_NI_IFL_..._030826.zip`, NSE 404s it permanently) was treated by
  `documents.drain()` as a budget deferral → stop-the-cycle → retried every
  30s forever, blocking 3,341 queued documents behind it and burning 3
  requests per cycle. Root cause: one exception type (`NSEUnavailable`) for
  two opposite situations. Fix: `NSEUnavailable.kind` = `deferred` (budget/
  breaker — resource is fine, stop the cycle, retry later) vs `exhausted`
  (retries spent against a responding server — fail THIS item, keep
  draining, never retry it). `tests/test_documents.py` pins all three
  behaviours, including no-retry-of-known-dead-URLs.
- **Static holiday seed proven wrong.** The live holiday-master API lists
  "15-Jan-2026 Municipal Corporation Election - Maharashtra" — absent from
  the circular-derived seed in `clock.py`. Ad-hoc holidays (elections,
  mourning days) appear mid-year; a stale calendar would poll P0 feeds on a
  closed day and let later phases mistake a holiday for an outage. Fix:
  `universe/holidays.py` — daily refresh into the `holidays` table (piggybacks
  the master-sync cadence) + startup overlay from DB so learned holidays
  survive restarts without HTTP. Live run: 20 API rows learned, calendar now
  knows 50 days. Fetch failure degrades to seed∪DB — never an empty table.
- Removed a stub (`is_special_session`) that violated the brief's no-stubs
  rule; muhurat handling stays in `clock.py`.
- Soak restarted 00:49 IST with fixes; 35 tests green; pushed to GitHub
  (auth: user re-scoped the PAT — first push 403'd as `rinkoo161`).

## v0.1.0 — Phase 1: ingestion foundation (2026-08-05)

Everything from repo-zero to a running, verified ingest pipeline in one session:

- **Hardened NSE gateway** (`net/`): curl_cffi Chrome TLS impersonation (plain
  requests is Akamai-blocked — settled finding carried over from ltp-monitor),
  cookie bootstrap for www JSON APIs, token bucket 30/min that REFUSES rather
  than queues, circuit breaker on 2 consecutive 401/403 with single half-open
  probe, conditional GET (ETag/Last-Modified) on RSS, full per-request audit
  into `http_audit`.
- **Postgres 16 as store AND bus** (decision: no Redis — events commit in the
  same transaction as facts, replay is SQL). Outbox + consumer offsets + dead
  letters; at-least-once with poison-event isolation.
- **Securities master**: 2,395 main-board + 560 SME, ISIN-keyed identity, 971
  rename/name aliases from symbolchange/namechange CSVs. Live bugs found and
  fixed: SME list moved to /emerge/corporates/ (old path serves a 1-row stub
  that answers 200 — added a >10-row guard); symbolchange.csv has no header.
- **A1**: all 23 feeds polled on priority schedules, single-threaded by design;
  295 filings on first pass; dedup via content_hash + (feed, dedup_key)
  constraints. RSS titles are company names → dual resolution (filename symbol
  token, then exact name match). Feeds with empty pubDate get event_at
  recovered from attachment filenames.
- **Backfill + catch-up**: announcements JSON API (richer: seq_id, symbol,
  ISIN, broadcast ts, NSE category). 3,260 rows over 6 days on first run; 24
  correctly deduped against RSS via the shared attachment-URL key. Hourly
  2-day catch-up in the supervisor closes RSS rolling-window gaps — this is
  what makes "zero missed filings" structural rather than hopeful.
- **Coverage verifier**: vs NSE's own API, gaps named not just counted.
  2026-08-03: 764/764 = 100.0%.
- **PIT layer**: every fact table carries event_at + observed_at (structural
  test enforces it); `pit_filings()` requires tz-aware as_of; poison test
  proves a future-observed row cannot change a past read.
- **CLI** (`ms run/filings/feeds/budget/backfill/verify/master-sync/serve`) and
  read-only API on :8100 with /health (names its problems), /metrics.
- 29 tests green. Resolution rate 97%, event_at coverage 98% at commit time.

Decisions logged (user-confirmed): Postgres-only (no Redis/Timescale until
measured) · local Ollama first for A2 with Claude fallback, benchmarked on the
Phase 2 eval set · Kite historical API available for Phase 3 price backfill ·
separate repo, read-only ltp-monitor integration.
