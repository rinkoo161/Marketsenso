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
_PROMOTER_MEMBER = "ShareholdingOfPromoterAndPromoterGroupMember"
_PROMOTER_CTX_PREFIX = "ShareholdingOfPromoterAndPromoterGroup"

# Two filing eras, same lesson as A3's banking map: element names changed.
# New era (NITCO 2026): EncumberedShares...; old era (FEL 2023):
# PledgedOrEncumbered... . Zero-pledge companies OMIT the facts entirely —
# the Whether...Encumbered booleans disambiguate omitted-because-zero
# (all false → affirmed 0) from genuinely unreported (→ None, unknown).
_ENC_PCT_FACTS = ("EncumberedSharesHeldAsPercentageOfTotalNumberOfShares",
                  "PledgedOrEncumberedSharesHeldAsPercentageOfTotalNumberOfShares")
_ENC_NUM_FACTS = ("NumberOfSharesEncumbered", "PledgedOrEncumberedNumberOfShares")
_PROMOTER_PCT_FACT = "ShareholdingAsAPercentageOfTotalNumberOfShares"


def _pct(v: float) -> float:
    """SEBI SHP percent facts are fractions of 1 (0.5919 = 59.19%). A
    handful of preparers file already-in-percent values; 1.0 exactly is
    ambiguous but as a fraction (100%) it is the conservative read for a
    risk gate, so only values strictly above 1 are treated as percent."""
    return round(v * 100.0, 2) if v <= 1.0 else round(v, 2)


def _promoter_ctx_ids(xml: str) -> list[str]:
    """Context ids carrying the promoter-group aggregate. The id string
    is preparer-chosen, so resolve semantically via the dimension member
    first; fall back to the conventional id prefix."""
    ids = [m.group(1) for m in re.finditer(
        r'<xbrli:context id="([^"]+)">(?:(?!</xbrli:context>).)*?'
        + _PROMOTER_MEMBER + r'(?:(?!</xbrli:context>).)*?</xbrli:context>',
        xml, re.S)]
    return ids or re.findall(
        rf'<xbrli:context id="({_PROMOTER_CTX_PREFIX}[^"]*)"', xml)


def _fact(xml: str, names: tuple[str, ...] | str, ctx_ids: list[str]) -> float | None:
    if isinstance(names, str):
        names = (names,)
    for name in names:
        for ctx in ctx_ids:
            m = re.search(
                rf"<[A-Za-z0-9_.-]+:{name}\s+[^>]*"
                rf"contextRef=\"{re.escape(ctx)}\"[^>]*>([^<]+)<", xml)
            if m:
                try:
                    return float(m.group(1).strip())
                except ValueError:
                    continue
    return None


def _affirmed_zero(xml: str) -> bool:
    """True when every Whether...Encumbered boolean the instance carries
    says false — the preparer affirmed there is no encumbrance, so the
    omitted percentage facts mean 0, not unknown."""
    vals = [v.strip().lower() for v in re.findall(
        r"<[A-Za-z0-9_.-]+:WhetherAnySharesHeldByPromoters[A-Za-z]*\s"
        r"[^>]*>([^<]+)<", xml)]
    return bool(vals) and all(v == "false" for v in vals)


def parse_shp_xbrl(xml: str) -> dict | None:
    """Extract the promoter-aggregate encumbrance facts. Returns None
    when the promoter context or promoter % is absent (bad/partial
    instance); returns encumbered_pct=None for pledged-but-unquantified
    (old instances that carry only the boolean)."""
    ctx_ids = _promoter_ctx_ids(xml)
    if not ctx_ids:
        return None
    promoter_raw = _fact(xml, _PROMOTER_PCT_FACT, ctx_ids)
    if promoter_raw is None:
        return None
    promoter = _pct(promoter_raw)
    enc_raw = _fact(xml, _ENC_PCT_FACTS, ctx_ids)
    if enc_raw is not None:
        enc_pct = _pct(enc_raw)
    elif _affirmed_zero(xml):
        enc_pct = 0.0
    else:
        enc_pct = None
    return {
        "promoter_pct": promoter,
        "encumbered_shares": _fact(xml, _ENC_NUM_FACTS, ctx_ids),
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
