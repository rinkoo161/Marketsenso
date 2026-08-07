"""Daily EOD prices + delivery from sec_bhavdata_full.

Why this file and not the UDiFF bhavcopy zip: verified live 2026-08-08,
sec_bhavdata_full_DDMMYYYY.csv carries OHLC, prev close, AVG_PRICE (true
VWAP), volume, turnover, trades, DELIV_QTY and DELIV_PER in one flat CSV
on the archives host — everything price_daily needs, one request per day,
no zip handling. The UDiFF zip adds nothing for the equity segment.

Series kept: EQ (rolling settlement), BE/BZ (trade-for-trade — needed
because surveillance moves liquid names there and A6 must see them),
SM/ST (SME). Bonds/G-secs/ETF-only series are skipped.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from marketsense.core.clock import IST, calendar
from marketsense.core.logging import get_logger
from marketsense.db.models import PriceDaily, Security
from marketsense.net.nse_client import NSE_ARCHIVES, NSEClient, NSEUnavailable

log = get_logger("a4.bhavcopy")

URL = NSE_ARCHIVES + "/products/content/sec_bhavdata_full_{d}.csv"
# Daily index closes (all NSE indices, one CSV). Needed for relative
# strength vs Nifty 500 / sector indices. Stored in price_daily with the
# index name as symbol and source='index'.
INDEX_URL = NSE_ARCHIVES + "/content/indices/ind_close_all_{d}.csv"
KEEP_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}
KEEP_INDICES = {"Nifty 50", "Nifty 500", "Nifty Bank", "Nifty Midcap 150",
                "Nifty Smallcap 250", "India VIX"}


def _f(v: str) -> float | None:
    v = (v or "").strip()
    if not v or v == "-":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def ingest_day(db_factory, client: NSEClient, day: date) -> dict:
    """Fetch and upsert one trading day. Idempotent (ON CONFLICT update —
    NSE re-publishes corrected files, the later fetch wins)."""
    stats = {"day": str(day), "rows": 0, "kept": 0}
    if not calendar.is_trading_day(day):
        stats["skipped"] = "not a trading day"
        return stats
    try:
        res = client.get(URL.format(d=day.strftime("%d%m%Y")))
    except NSEUnavailable as e:
        if e.kind == "deferred":
            raise  # budget — caller waits and retries this day
        stats["error"] = str(e)[:200]  # 404 etc: day genuinely unavailable
        return stats

    reader = csv.DictReader(io.StringIO(res.content.decode("utf-8", "replace")))
    trade_ts = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)

    # symbol -> security_id map once per call, not per row
    with db_factory() as db:
        sec_ids = dict(db.execute(
            __import__("sqlalchemy").select(Security.symbol, Security.id)).all())

        batch: list[dict] = []
        for row in reader:
            row = {k.strip(): (v.strip() if isinstance(v, str) else v)
                   for k, v in row.items()}
            stats["rows"] += 1
            series = row.get("SERIES", "")
            if series not in KEEP_SERIES:
                continue
            symbol = row.get("SYMBOL", "")
            if not symbol:
                continue
            batch.append({
                "symbol": symbol,
                "security_id": sec_ids.get(symbol),
                "trade_date": trade_ts,
                "series": series,
                "open": _f(row.get("OPEN_PRICE")),
                "high": _f(row.get("HIGH_PRICE")),
                "low": _f(row.get("LOW_PRICE")),
                "close": _f(row.get("CLOSE_PRICE")),
                "prev_close": _f(row.get("PREV_CLOSE")),
                "vwap": _f(row.get("AVG_PRICE")),
                "volume": _f(row.get("TTL_TRD_QNTY")),
                "turnover": _f(row.get("TURNOVER_LACS")),
                "trades": _f(row.get("NO_OF_TRADES")),
                "delivery_qty": _f(row.get("DELIV_QTY")),
                "delivery_pct": _f(row.get("DELIV_PER")),
                "source": "bhavcopy",
            })
        stats["kept"] = len(batch)
        if batch:
            stmt = pg_insert(PriceDaily).values(batch)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_price_symbol_date",
                set_={c: stmt.excluded[c] for c in
                      ("open", "high", "low", "close", "prev_close", "vwap",
                       "volume", "turnover", "trades", "delivery_qty",
                       "delivery_pct", "source", "series", "security_id")},
            )
            db.execute(stmt)
        db.commit()
    log.info("bhavcopy_ingested", **stats)
    return stats


def ingest_indices_day(db_factory, client: NSEClient, day: date) -> dict:
    """Index closes for one day → price_daily (source='index')."""
    stats = {"day": str(day), "kept": 0}
    if not calendar.is_trading_day(day):
        return stats
    try:
        res = client.get(INDEX_URL.format(d=day.strftime("%d%m%Y")))
    except NSEUnavailable as e:
        if e.kind == "deferred":
            raise
        stats["error"] = str(e)[:200]
        return stats
    reader = csv.DictReader(io.StringIO(res.content.decode("utf-8", "replace")))
    trade_ts = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)
    batch = []
    for row in reader:
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        name = row.get("Index Name", "")
        if name not in KEEP_INDICES:
            continue
        batch.append({
            "symbol": f"IDX:{name}",
            "trade_date": trade_ts,
            "open": _f(row.get("Open Index Value")),
            "high": _f(row.get("High Index Value")),
            "low": _f(row.get("Low Index Value")),
            "close": _f(row.get("Closing Index Value")),
            "prev_close": _f(row.get("Points Change")) and None,
            "volume": _f(row.get("Volume")),
            "turnover": _f(row.get("Turnover (Rs. Cr.)")),
            "source": "index",
        })
    stats["kept"] = len(batch)
    if batch:
        with db_factory() as db:
            stmt = pg_insert(PriceDaily).values(batch)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_price_symbol_date",
                set_={c: stmt.excluded[c] for c in
                      ("open", "high", "low", "close", "volume", "turnover")},
            )
            db.execute(stmt)
            db.commit()
    return stats


def backfill(db_factory, client: NSEClient, *, days: int = 400,
             end: date | None = None) -> dict:
    """Walk trading days backwards. The archives serve sec_bhavdata_full
    for years back; each missing/failed day is recorded, not fatal."""
    import time

    end = end or date.today()
    done = failed = 0
    d = end
    remaining = days
    while remaining > 0:
        if calendar.is_trading_day(d):
            try:
                r = ingest_day(db_factory, client, d)
                ingest_indices_day(db_factory, client, d)
            except NSEUnavailable:
                time.sleep(30)  # budget refill; same day retries next loop
                continue
            if r.get("kept"):
                done += 1
            elif "error" in r:
                failed += 1
            remaining -= 1
        d = calendar.prev_trading_day(d)
    return {"days_ingested": done, "days_failed": failed}
