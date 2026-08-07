# Phase 1 Evidence Report — 48h Soak

**Verdict: PASSED.** All §9 Phase 1 acceptance criteria met with evidence below.
Window: 2026-08-05 13:05 IST → 2026-08-07 13:05 IST (gate), still running at
report time (2026-08-07 21:25 IST, 56h+ continuous, PID uptime verified via
`ps lstart`). Two full NSE market sessions covered (Aug 5, Aug 6) plus the
Aug 7 session; the process was never restarted inside the window.

## Acceptance criteria — brief §9 vs measured

| Criterion | Target | Measured | Evidence source |
|---|---|---|---|
| Continuous run | 48h | 56h+ unbroken (started Aug 5 13:05) | `ps -o lstart` on `ms run` |
| Duplicate filings | 0 | **0** across 12,692 filings | `group by content_hash having count>1` → empty |
| Unhandled 403s | 0 | **0** 401/403 in 27,172 requests over the window | `http_audit where status in (401,403)` → empty |
| Feed coverage vs NSE | full | **100.0% both full days**: 1,009/1,009 (Aug 5), 1,156/1,156 (Aug 6), zero missing, gaps named (none) | `ms verify --day` against NSE's own announcements API |

## Volume and reliability

- 12,692 filings ingested (12,692 outbox events — exactly 1:1, no lost or
  double-published events).
- Agent runs: a1_poller 41,137 ok / **0 errors**; a1_docs 7,041 ok / 0;
  a1_catchup 64 ok / 0; securities_master 6 ok / 0.
- Documents: 11,254 fetched (content-addressed), 79 permanently dead URLs
  (isolated, never blocking), **550 saved by the retry ladder** — files that
  404'd on first touch during archive lag and succeeded on a later rung.
  Without the ladder ~4.6% of all documents would have been silently lost.
- All 23 feeds polling with 0 consecutive errors. `corporate_governance` has
  served 0 entries — the feed itself is empty between quarterly windows
  (verified: polls return 200/ok); not a defect, and it will be re-checked
  when quarterly governance reports fall due.

## Politeness profile (30/min budget)

| Day | Requests | Avg/min | 200 | 304 | 404 | transport-err |
|---|---|---|---|---|---|---|
| Aug 5 | 11,141 | 7.7 | 8,360 | 2,019 | 714 | 48 |
| Aug 6 | 8,206 | 5.7 | 5,445 | 2,369 | 332 | 60 |
| Aug 7 | 7,825 | 5.4 | 5,166 | 2,275 | 384 | 0 |

Declining trend as conditional-GET caches warmed and the two soak fixes
landed; breaker never opened; budget refusals only during the initial
backfill burst (by design — refusals ARE the politeness working).

## Defects found by the soak (all fixed, tested, pushed)

1. **404 head-of-line block** (found 25 min in): a permanently-missing
   attachment re-tried every 30s forever and blocked 3,341 queued documents.
   Fix: `NSEUnavailable.kind` deferred/exhausted split. `0fbc3cd`
2. **Static holiday seed provably incomplete**: live holiday-master API lists
   an ad-hoc election holiday (15-Jan-2026) absent from the seed. Fix: daily
   refresh + startup DB overlay; calendar now learns. `0fbc3cd`
3. **Eventually-consistent archives host** (found at midday load): fresh XBRL
   404s for minutes after its RSS item appears. Fix: 5/15/45-min retry
   ladder; 550 documents saved to date. `ef5eeff`
4. **404 retry waste**: in-process 3× retry on 404 tripled request burn
   (123→10 per 30min after fix). Fix: fail fast, ladder owns the lag.
   `bd4d45a`

## Known characteristics (not defects)

- Symbol resolution is ~84% overall but ~97% for actual equities: the gap is
  ETF/MF NAV declarations and delisted/debt-only issuers, which correctly
  have no row in an equity securities master. Phase 2's taxonomy should tag
  these classes so metrics report against equities only.
- Backfilled rows carry `observed_at = ingestion time` (the truth); backtest
  windows over them are `reconstructed`, per §10 point-in-time policy.

## Gate decision

Phase 1 accepted. Phase 2 (A2 document intelligence) is clear to start.
