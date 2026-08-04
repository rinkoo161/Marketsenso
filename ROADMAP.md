# ROADMAP

## PENDING WORK

**Phase 1 remaining**
- 48h soak run in progress (started 2026-08-05, ~00:20 IST). Evidence report due
  at completion: duplicate count (target 0), unhandled 403 count (target 0),
  per-feed coverage vs the NSE API, budget consumption profile.
- Holiday refresh from the NSE holiday-master API into the `holidays` table
  (static 2024–2026 seed is live; the refresh endpoint needs the cookie
  bootstrap — wire through `runtime.nse_client()` when touched next).
- Backfill currently covers the announcements API only. Board meetings /
  corporate actions / shareholding JSON APIs are enrichment, not gaps — RSS
  covers them live; extend `backfill.py` per-endpoint as Phase 3 needs them.

**Phase 2 — Document intelligence (next)**
- A2 consumer over `filing.received` (bus.Consumer is ready for it).
- Metadata-first classification: deterministic router (auditor resignation →
  materiality ≥9 etc.) → local Ollama triage (user chose local-first) → strong
  model only above the materiality bar. Strict JSON schema, cached by document
  sha256 (Document.sha256 is already content-addressed for this).
- pdfplumber+Tesseract extraction (`pip install -e ".[docintel]"`).
- 100-filing hand-labelled held-out set; gate: ≥85% category accuracy, ≥0.7
  materiality correlation. If the local model misses the gate, that is data —
  revisit the provider decision with the eval numbers, not opinion.

**Phase 3–5** — fundamentals (XBRL; Kite historical API confirmed available for
5y price backfill), technicals from bhavcopy, flow, fusion + walk-forward
backtest (PIT layer already enforces no look-ahead), alerts + read-only LTP
Monitor integration (one additive poller agent on their side, MarketSense REST
on :8100).

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
