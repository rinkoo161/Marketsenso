"""A5 ingesters — FII/DII, bulk/block deals, ASM/GSM surveillance.

All idempotent (natural-key upserts / append-only snapshots). Shapes
verified live 2026-08-08:
  fiidiiTradeReact  → [{category: FII/DII, date, buyValue, ...}]
  bulk.csv/block.csv → recent-window CSVs on the archives host
  reportASM → {longterm: {data: [...]}, shortterm: {data: [...]}}
              (rows carry symbol via 'symbol' or need ISIN mapping)
  reportGSM → [{symbol, gsmStage, survCode, ...}]
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from marketsense.agents.a1_ingestion.parse import parse_pub
from marketsense.core.logging import get_logger
from marketsense.db.models import LargeDeal, MarketFlow, Surveillance
from marketsense.net.nse_client import NSE_ARCHIVES, NSE_WWW, NSEClient, NSEUnavailable

log = get_logger("a5.ingest")

FIIDII = NSE_WWW + "/api/fiidiiTradeReact"
ASM = NSE_WWW + "/api/reportASM"
GSM = NSE_WWW + "/api/reportGSM"
BULK = NSE_ARCHIVES + "/content/equities/bulk.csv"
BLOCK = NSE_ARCHIVES + "/content/equities/block.csv"


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def ingest_fiidii(db_factory, client: NSEClient) -> dict:
    rows = client.get_json(FIIDII)
    batch = []
    for r in rows if isinstance(rows, list) else []:
        day = parse_pub(r.get("date"))
        if day is None:
            continue
        batch.append({"day": day, "category": (r.get("category") or "")[:8],
                      "buy_value": _num(r.get("buyValue")),
                      "sell_value": _num(r.get("sellValue")),
                      "net_value": _num(r.get("netValue"))})
    if batch:
        with db_factory() as db:
            stmt = pg_insert(MarketFlow).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["day", "category"],
                set_={c: stmt.excluded[c] for c in
                      ("buy_value", "sell_value", "net_value")})
            db.execute(stmt)
            db.commit()
    return {"fiidii_rows": len(batch)}


def ingest_deals(db_factory, client: NSEClient) -> dict:
    stats = {"bulk": 0, "block": 0}
    for kind, url in (("bulk", BULK), ("block", BLOCK)):
        try:
            res = client.get(url)
        except NSEUnavailable as e:
            log.warning("deals_unavailable", kind=kind, error=str(e)[:100])
            continue
        reader = csv.DictReader(io.StringIO(res.content.decode("utf-8", "replace")))
        batch = []
        for row in reader:
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            day = parse_pub((row.get("Date") or "").title())
            sym = row.get("Symbol", "")
            if day is None or not sym:
                continue
            batch.append({
                "day": day, "symbol": sym, "kind": kind,
                "client": row.get("Client Name", "")[:1000],
                "side": (row.get("Buy/Sell") or "")[:4].upper(),
                "qty": _num(row.get("Quantity Traded")),
                "price": _num(row.get("Trade Price / Wght. Avg. Price")),
            })
        if batch:
            with db_factory() as db:
                stmt = pg_insert(LargeDeal).values(batch)
                stmt = stmt.on_conflict_do_nothing(constraint="uq_large_deal")
                db.execute(stmt)
                db.commit()
            stats[kind] = len(batch)
    return stats


def ingest_surveillance(db_factory, client: NSEClient) -> dict:
    """Today's ASM/GSM membership snapshot (append-only by as_of)."""
    as_of = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                               microsecond=0)
    batch: list[dict] = []

    try:
        asm = client.get_json(ASM)
        for framework, key in (("asm_lt", "longterm"), ("asm_st", "shortterm")):
            data = (asm.get(key) or {}).get("data") or []
            for r in data:
                sym = (r.get("symbol") or "").strip()
                if not sym:
                    continue
                batch.append({"as_of": as_of, "symbol": sym,
                              "framework": framework,
                              "stage": (r.get("asmSurvIndicator") or "")[:16],
                              "detail": r.get("companyName")})
    except NSEUnavailable as e:
        log.warning("asm_unavailable", error=str(e)[:100])

    try:
        gsm = client.get_json(GSM)
        for r in gsm if isinstance(gsm, list) else []:
            sym = (r.get("symbol") or "").strip()
            if not sym:
                continue
            batch.append({"as_of": as_of, "symbol": sym, "framework": "gsm",
                          "stage": (r.get("gsmStage") or "")[:16],
                          "detail": (r.get("survCode") or "") + " " +
                                    (r.get("survDesc") or "")})
    except NSEUnavailable as e:
        log.warning("gsm_unavailable", error=str(e)[:100])

    if batch:
        # one symbol can appear twice in a feed snapshot — keep first
        seen: dict[tuple, dict] = {}
        for b in batch:
            seen.setdefault((b["symbol"], b["framework"]), b)
        batch = list(seen.values())
        with db_factory() as db:
            stmt = pg_insert(Surveillance).values(batch)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_surv",
                set_={"stage": stmt.excluded["stage"],
                      "detail": stmt.excluded["detail"]})
            db.execute(stmt)
            db.commit()
    return {"surveillance_rows": len(batch)}


def ingest_all(db_factory, client: NSEClient) -> dict:
    out = {}
    for fn in (ingest_fiidii, ingest_deals, ingest_surveillance):
        try:
            out.update(fn(db_factory, client))
        except NSEUnavailable as e:
            out[fn.__name__] = f"unavailable: {str(e)[:80]}"
    log.info("a5_ingested", **{k: str(v) for k, v in out.items()})
    return out
