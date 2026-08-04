# CLAUDE.md

## What this is

MarketSense — NSE corporate-disclosure intelligence platform, Phase 1 complete
(ingestion). Sibling of `../LTP Monitor online/ltp-monitor` but a **separate
repo and separate processes**; integration is read-only over REST in Phase 5.
Decision support only: no order placement anywhere, ever (build brief §10).

## Commands

```bash
.venv/bin/pytest -q                  # full suite; needs marketsense_test DB
.venv/bin/ms run                     # ingest supervisor (foreground)
.venv/bin/ms serve                   # read-only API on :8100
.venv/bin/ms feeds / budget / verify # ops introspection
.venv/bin/alembic upgrade head       # schema
```

Postgres 16 via brew (`brew services start postgresql@16`), DBs `marketsense`
and `marketsense_test`. No Docker on this machine.

## Architecture rules that are load-bearing

- **One NSEClient per process** (`runtime.nse_client()`), wrapped in a shared
  token-bucket budget + circuit breaker. Never construct a second client, never
  call NSE with requests/httpx — Akamai checks the TLS fingerprint and only
  curl_cffi Chrome impersonation passes. The ingest supervisor is deliberately
  single-threaded so NSE access is serialized by construction.
- **Postgres is the event bus.** `bus/outbox.publish()` inside the same
  transaction as the facts; consumers are at-least-once with dead-lettering
  (`bus/outbox.Consumer`). No Redis; don't add one without a measured need.
- **Point-in-time integrity is structural.** Every fact table has `event_at` +
  `observed_at` (see `test_pit.py::test_every_fact_table_has_observed_at`).
  Analytical reads go through `db/pit.py` and require a tz-aware `as_of`.
  The poison test (`tests/test_pit.py`) failing is a build failure — never
  weaken it. Backfilled rows get `observed_at = now` (the truth), which means
  backtests over backfilled windows are labelled reconstructed, not observed.
- **Dedup lives in DB constraints**, not application memory: global
  `content_hash` unique + feed-scoped `dedup_key` (attachment URL / seq_id).
  RSS and JSON-API backfill share the attachment URL as dedup_key — that bridge
  is what lets the hourly catch-up close RSS window gaps without duplicates.
- **Config is pydantic-settings with extra="forbid"** — unknown MS_* env keys
  fail at startup. There is no config save path that can silently drop keys.

## Live-verified NSE facts (2026-08-05) — re-verify before "fixing"

- RSS `<title>` is the **company name**, not the symbol; the symbol is the
  first token of the attachment filename. XBRL links carry no symbol → resolve
  by company name against securities master (exact match only, no fuzzy).
- brsr / annual_reports / encumbrance / some related_party publish **empty
  pubDate**; `parse.event_at_from_link()` recovers timestamps from filenames
  (two different embedded formats).
- The SME equity list lives at `/emerge/corporates/content/SME_EQUITY_L.csv`
  (~560 rows, underscore headers). `/content/equities/SME_EQUITY_L.csv`
  answers 200 with a 1-row stub — the >10-rows guard in securities_master
  exists for exactly this.
- `symbolchange.csv` has **no header row** (name, old, new, date positional).
- The announcements JSON API (`/api/corporate-announcements`) needs the cookie
  bootstrap; archives CSVs don't. Both share one budget.

## Conventions

- Tests assert against **live-observed fixture payloads** (see test_parse.py's
  header note) — don't replace them with invented shapes.
- ROADMAP.md is the cross-session source of truth; append a section per release
  explaining what changed and why, including bugs exposed.
- Comments carry the reasoning for specific past failures — preserve them when
  touching the code.
