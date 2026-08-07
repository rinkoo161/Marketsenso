"""Historical quarterly financials backfill.

Source: /api/corporates-financial-results?symbol=X&period=Quarterly —
verified live 2026-08-08: full filing history (TITAN: 115 rows back to
2005) with direct `xbrl` archive links and broadcast timestamps.

Each historical filing lands as a REAL Filing row (feed=financial_results,
source=api_backfill, dedup_key=xbrl url) + Document + parsed
FinancialsQuarterly — the same evidence chain as live ingestion, so A7
theses can cite a 2024 quarter exactly like yesterday's. observed_at is
NOW (the truth: backfilled), event_at is the broadcast time; backtests
over these rows are `reconstructed` per the §10 policy.

Budget shape: 1 index call + ~2 XBRL fetches per quarter per symbol.
Top-500-by-turnover × 8 quarters ≈ 5k requests ≈ 3h at 30/min — a
weekend job, deliberately run when feeds are quiet.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from marketsense.agents.a1_ingestion.parse import parse_pub
from marketsense.agents.a3_fundamental.loader import load_all
from marketsense.core.config import settings
from marketsense.core.logging import get_logger
from marketsense.db.models import Document, Filing, PriceDaily
from marketsense.net.nse_client import NSE_WWW, NSEClient, NSEUnavailable

log = get_logger("a3.backfill")

# Two endpoints, one history (verified live 2026-08-08): the classic
# results API stops at the Dec-2024 quarter — SEBI's integrated-filing
# regime took over from Mar-2025 — and the integrated API carries
# everything since. Both serve `xbrl` archive links; the same parser
# handles both eras.
API = NSE_WWW + "/api/corporates-financial-results?index=equities&symbol={sym}&period=Quarterly"
INTEGRATED_API = (NSE_WWW + "/api/integrated-filing-results?index=equities"
                  "&symbol={sym}&type=Integrated%20Filing-%20Financials")


def top_symbols_by_turnover(db_factory, n: int = 500) -> list[str]:
    """Liquidity-ranked universe (Tier-2 proxy until index constituents
    land): median turnover over the ingested price history."""
    with db_factory() as db:
        rows = db.execute(
            select(PriceDaily.symbol,
                   func.percentile_cont(0.5).within_group(PriceDaily.turnover))
            .where(PriceDaily.source == "bhavcopy",
                   PriceDaily.turnover.isnot(None))
            .group_by(PriceDaily.symbol)
            .order_by(func.percentile_cont(0.5)
                      .within_group(PriceDaily.turnover).desc())
            .limit(n)
        ).all()
    return [s for s, _ in rows]


def _fetch_doc(client: NSEClient, url: str) -> tuple[str, int] | None:
    """Download one XBRL to the content-addressed store. (path, bytes)."""
    res = client.get(url, timeout=60.0)
    sha = hashlib.sha256(res.content).hexdigest()
    out_dir = settings().pdf_dir / sha[:2]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sha}.xml"
    if not path.exists():
        path.write_bytes(res.content)
    return str(path), len(res.content)


def _normalise(row: dict, era: str) -> dict:
    """Both API row shapes → {xbrl, to_date, broadcast, subject}."""
    if era == "classic":
        return {
            "xbrl": (row.get("xbrl") or "").strip(),
            "to_date": parse_pub(row.get("toDate")),
            "broadcast": parse_pub(row.get("broadCastDate")),
            "subject": f"{row.get('relatingTo', '')} results "
                       f"({row.get('consolidated', '')})".strip(),
        }
    return {  # integrated: qe_Date like '30-JUN-2026'
        "xbrl": (row.get("xbrl") or "").strip(),
        "to_date": parse_pub((row.get("qe_Date") or "").title()),
        "broadcast": parse_pub(row.get("broadcast_Date")
                               or row.get("creation_Date")),
        "subject": f"Integrated filing results ({row.get('consolidated', '')})",
    }


def backfill_symbol(db_factory, client: NSEClient, symbol: str,
                    *, quarters_back: int = 8) -> dict:
    """All quarterly filings for one symbol within the window, both eras."""
    stats = {"symbol": symbol, "listed": 0, "new": 0, "skipped": 0}
    entries: list[dict] = []
    for era, url in (("classic", API), ("integrated", INTEGRATED_API)):
        try:
            data = client.get_json(url.format(sym=symbol))
        except NSEUnavailable as e:
            if e.kind == "deferred":
                raise
            stats["error"] = str(e)[:120]
            continue
        rows = data.get("data", []) if isinstance(data, dict) else data
        if isinstance(rows, list):
            entries.extend(_normalise(r, era) for r in rows if isinstance(r, dict))

    cutoff = datetime.now(timezone.utc) - timedelta(days=int(quarters_back * 95))
    with db_factory() as db:
        for e in entries:
            xbrl, to_date = e["xbrl"], e["to_date"]
            if not xbrl or to_date is None or to_date < cutoff:
                continue
            stats["listed"] += 1
            exists = db.scalar(select(Filing.id).where(
                Filing.feed == "financial_results", Filing.dedup_key == xbrl))
            if exists:
                stats["skipped"] += 1
                continue
            try:
                path, nbytes = _fetch_doc(client, xbrl)
            except NSEUnavailable as exc:
                if exc.kind == "deferred":
                    db.commit()
                    raise
                continue  # dead archive link — rare, skip
            filing = Filing(
                feed="financial_results",
                dedup_key=xbrl,
                content_hash=hashlib.sha256(
                    ("finhist|" + xbrl).encode()).hexdigest(),
                symbol=symbol,
                subject=e["subject"],
                link=xbrl, attachment_url=xbrl,
                raw={"subject": e["subject"],
                     "to_date": to_date.isoformat(),
                     "broadcast": e["broadcast"].isoformat()
                     if e["broadcast"] else None},
                source="api_backfill", event_at=e["broadcast"],
            )
            db.add(filing)
            db.flush()
            db.add(Document(filing_id=filing.id, url=xbrl,
                            sha256=Path(path).stem, local_path=path,
                            bytes=nbytes, fetch_status="fetched"))
            stats["new"] += 1
        db.commit()
    return stats


def backfill_universe(db_factory, client: NSEClient, *, top: int = 500,
                      quarters_back: int = 8) -> dict:
    """The full weekend job: top-N liquid symbols, then one loader pass."""
    import time

    symbols = top_symbols_by_turnover(db_factory, top)
    totals = {"symbols": 0, "new_docs": 0, "errors": 0}
    for sym in symbols:
        while True:
            try:
                r = backfill_symbol(db_factory, client, sym,
                                    quarters_back=quarters_back)
                break
            except NSEUnavailable:
                time.sleep(30)  # budget breather, retry same symbol
        totals["symbols"] += 1
        totals["new_docs"] += r.get("new", 0)
        if "error" in r:
            totals["errors"] += 1
        if totals["symbols"] % 25 == 0:
            log.info("finhist_progress", **totals)
    totals["loader"] = load_all(db_factory)
    log.info("finhist_done", **totals)
    return totals
