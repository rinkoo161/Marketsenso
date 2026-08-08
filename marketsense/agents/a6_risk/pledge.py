"""Promoter pledge % from quarterly SHP XBRL.

Sourcing decision (2026-08-08): the Reg-31/encumbrance filings NSE
serves are PDF forms — one of two sampled was a pure scan. But every
quarterly shareholding-pattern submission carries a SEBI-taxonomy XBRL
(`corporate-share-holdings-master` API → `xbrl` URL) whose
ShareholdingOfPromoterAndPromoterGroup aggregate context holds exactly
the numbers the brief's hard block needs:

    ShareholdingAsAPercentageOfTotalNumberOfShares        0.2227
    NumberOfSharesEncumbered                              32571028
    EncumberedSharesHeldAsPercentageOfTotalNumberOfShares 0.5919

(NITCO, verified live — 59.19% of promoter holding encumbered, and the
0.2227 cross-checks the master API's own pr_and_prgrp "22.27".)
Percent facts are decimal FRACTIONS; ×100 on store.

Refresh model: event-driven, not polled. The shareholding_pattern RSS
feed (already ingested) tells us exactly when a company files a new SHP
— that symbol becomes pending. Cold start walks scored symbols once;
symbols with pledge ACTIVITY filings (encumbrance / sast_reg31) jump the
queue because they are the ones where the % decides a veto. Cost:
2 budgeted requests per symbol, paced by the shared token bucket.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import func, select

from marketsense.core.logging import get_logger
from marketsense.db.models import Filing, PromoterPledge, Score
from marketsense.net.nse_client import NSE_WWW, NSEUnavailable

log = get_logger("a6.pledge")

SHP_MASTER_API = NSE_WWW + "/api/corporate-share-holdings-master?index=equities&symbol={sym}"
_PROMOTER_CTX = "ShareholdingOfPromoterAndPromoterGroup"

_FACTS = {
    "ShareholdingAsAPercentageOfTotalNumberOfShares": "promoter_pct",
    "NumberOfSharesEncumbered": "encumbered_shares",
    "EncumberedSharesHeldAsPercentageOfTotalNumberOfShares": "encumbered_pct",
}


def _pct(v: float) -> float:
    """SEBI SHP percent facts are fractions of 1 (0.5919 = 59.19%). A
    handful of preparers file already-in-percent values; 1.0 exactly is
    ambiguous but as a fraction (100%) it is the conservative read for a
    risk gate, so only values strictly above 1 are treated as percent."""
    return round(v * 100.0, 2) if v <= 1.0 else round(v, 2)


def parse_shp_xbrl(xml: str) -> dict | None:
    """Extract the promoter-aggregate encumbrance facts. Returns None
    when the promoter context is absent (bad/partial instance)."""
    out: dict = {}
    for fact, key in _FACTS.items():
        m = re.search(
            rf"<[A-Za-z0-9_.-]+:{fact}\s+[^>]*"
            rf"contextRef=\"{_PROMOTER_CTX}[^\"]*\"[^>]*>([^<]+)<", xml)
        if m:
            try:
                out[key] = float(m.group(1).strip())
            except ValueError:
                continue
    if "promoter_pct" not in out:
        return None
    promoter = _pct(out["promoter_pct"])
    enc_pct = _pct(out["encumbered_pct"]) if "encumbered_pct" in out else None
    return {
        "promoter_pct": promoter,
        "encumbered_shares": out.get("encumbered_shares"),
        "encumbered_pct": enc_pct,
        "encumbered_pct_of_total":
            round(promoter * enc_pct / 100.0, 2) if enc_pct is not None else None,
    }


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(s.strip().title(), fmt).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def sync_symbol(db, client, symbol: str) -> str:
    """Fetch the latest SHP submission's XBRL for one symbol and store
    the promoter-encumbrance row. Returns a stats key. Raises
    NSEUnavailable through — the caller decides whether to stop the run
    (budget) or move on."""
    rows = client.get_json(SHP_MASTER_API.format(sym=symbol))
    rows = rows.get("data") if isinstance(rows, dict) else rows
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    rows.sort(key=lambda r: _parse_dt(r.get("broadcastDate"))
              or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    latest = next((r for r in rows if r.get("xbrl")), None)

    if latest is None:
        # sentinel: looked, nothing usable — don't retry every cycle
        db.add(PromoterPledge(symbol=symbol, record_id="none",
                              encumbered_pct=None))
        return "no_shp"

    record_id = str(latest.get("recordId") or "none")
    if db.scalar(select(PromoterPledge.id).where(
            PromoterPledge.symbol == symbol,
            PromoterPledge.record_id == record_id)):
        return "dup"

    xbrl_url = latest["xbrl"]
    resp = client.get(xbrl_url)
    parsed = parse_shp_xbrl(resp.content.decode("utf-8", errors="replace"))
    if parsed is None:
        db.add(PromoterPledge(symbol=symbol, record_id=record_id,
                              xbrl_url=xbrl_url, encumbered_pct=None,
                              event_at=_parse_dt(latest.get("broadcastDate"))))
        return "unparseable"

    db.add(PromoterPledge(
        symbol=symbol,
        shp_date=_parse_dt(latest.get("date")),
        record_id=record_id,
        xbrl_url=xbrl_url,
        event_at=_parse_dt(latest.get("broadcastDate")),
        **parsed,
    ))
    return "stored"


def pending_symbols(db, limit: int) -> list[str]:
    """Priority-ordered symbols needing a pledge fetch:
    1. pledge-ACTIVITY symbols (encumbrance/sast_reg31 filings) without a
       row — the % decides an actual veto there;
    2. symbols whose newest shareholding_pattern filing postdates our row
       (or who have none) — the event-driven quarterly refresh;
    3. cold start: scored symbols with no row at all."""
    latest = {s: t for s, t in db.execute(
        select(PromoterPledge.symbol, func.max(PromoterPledge.observed_at))
        .group_by(PromoterPledge.symbol))}

    act = [s for (s,) in db.execute(
        select(Filing.symbol).where(
            Filing.feed.in_(("encumbrance", "sast_reg31")),
            Filing.symbol.isnot(None)).distinct()) if s not in latest]
    fresh = [s for s, t in db.execute(
        select(Filing.symbol, func.max(Filing.observed_at))
        .where(Filing.feed == "shareholding_pattern",
               Filing.symbol.isnot(None))
        .group_by(Filing.symbol))
        if s not in latest or t > latest[s]]
    scored = [s for (s,) in db.execute(
        select(Score.symbol).where(Score.agent.in_(("a3", "a4", "a5")))
        .distinct()) if s not in latest]

    out: list[str] = []
    seen: set[str] = set()
    for s in act + fresh + scored:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:limit]


def sync_pending(db_factory, client, *, limit: int = 300,
                 max_minutes: float = 25.0) -> dict:
    """One paced pass. Commits per symbol so any stop keeps everything
    already fetched. At 2 requests/symbol the shared 30/min bucket
    refuses every ~15 symbols — that refusal is the pacing working, so
    we sleep through the refill and continue (still never exceeding the
    budget) instead of aborting the pass. A circuit-breaker trip (NSE
    actually pushing back) or the wall-clock cap DOES end the pass."""
    import time

    stats = {"stored": 0, "dup": 0, "no_shp": 0, "unparseable": 0,
             "deferred": 0}
    with db_factory() as db:
        symbols = pending_symbols(db, limit)
    started = time.monotonic()
    i = 0
    while i < len(symbols):
        if time.monotonic() - started > max_minutes * 60:
            log.info("pledge_sync_time_capped", done=i, of=len(symbols))
            break
        sym = symbols[i]
        try:
            with db_factory() as db:
                stats[sync_symbol(db, client, sym)] += 1
                db.commit()
        except NSEUnavailable as e:
            if "budget" in str(e):          # token refill — wait, retry same
                time.sleep(2.5)
                continue
            stats["deferred"] += 1          # circuit open — NSE said stop
            log.info("pledge_sync_deferred", symbol=sym, error=str(e))
            break
        except Exception as e:  # one bad symbol must not kill the pass
            log.warning("pledge_sync_error", symbol=sym, error=str(e)[:200])
        i += 1
    log.info("pledge_sync", requested=len(symbols), **stats)
    return stats
