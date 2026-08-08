"""Walk fetched XBRL documents → financials_quarterly rows.

Idempotent: keyed (symbol, period_end, basis); a re-filed/revised result
updates in place with the newer filing's values (NSE revisions are the
corrected truth). filing_id keeps the evidence trail to the disclosure.
Symbol resolution: the instance's own Symbol fact first (authoritative),
falling back to the filing's symbol.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from marketsense.agents.a3_fundamental.xbrl import parse_instance
from marketsense.core.logging import get_logger
from marketsense.db.models import Document, FinancialsQuarterly, Filing, Security

log = get_logger("a3.loader")


def load_all(db_factory, *, limit: int | None = None) -> dict:
    """Parse every fetched .xml document not yet loaded. Cheap to re-run:
    non-results instances are skipped by the parser in milliseconds and
    remembered nowhere (reparse cost ≈ open+scan)."""
    stats = {"parsed": 0, "loaded": 0, "not_results": 0, "no_symbol": 0}
    with db_factory() as db:
        docs = db.execute(
            select(Document.local_path, Document.filing_id)
            .where(Document.fetch_status == "fetched",
                   Document.local_path.ilike("%.xml"))
            .order_by(Document.id)
        ).all()
        sec_ids = dict(db.execute(select(Security.symbol, Security.id)).all())
        filings = {}  # filing_id -> (symbol, event_at) lazy cache

        batch: list[dict] = []
        for path, filing_id in docs[:limit] if limit else docs:
            inst = parse_instance(path)
            stats["parsed"] += 1
            if inst is None:
                stats["not_results"] += 1
                continue
            if not inst["quarterly"]:
                # H1/9M/annual cumulatives are NOT quarters; storing one
                # under its end date doubles the "quarter" (gate finding
                # 2026-08-08). De-cumulation is future work; skipping is
                # honest today.
                stats["non_quarter"] = stats.get("non_quarter", 0) + 1
                continue
            symbol = inst["symbol"]
            event_at = None
            if filing_id:
                if filing_id not in filings:
                    f = db.get(Filing, filing_id)
                    filings[filing_id] = (f.symbol if f else None,
                                          f.event_at if f else None)
                fsym, event_at = filings[filing_id]
                symbol = symbol or fsym
            if not symbol or inst["period_end"] is None:
                stats["no_symbol"] += 1
                continue
            batch.append({
                "symbol": symbol,
                "security_id": sec_ids.get(symbol),
                "filing_id": filing_id,
                "period_end": inst["period_end"],
                "basis": inst["basis"],
                "audited": inst["audited"],
                "event_at": event_at,
                "raw": inst["raw"],
                **{k: inst["values"].get(k) for k in
                   ("revenue", "other_income", "total_income", "expenses",
                    "finance_costs", "depreciation", "pbt", "tax", "pat",
                    "eps_basic")},
            })

        if batch:
            # Same (symbol, period, basis) can appear twice in one run —
            # original + revision filings of one result. ON CONFLICT cannot
            # touch a row twice per statement, so keep only the LAST
            # occurrence (docs are id-ordered; the later filing is the
            # revision and wins).
            deduped: dict[tuple, dict] = {}
            for row in batch:
                key = (row["symbol"], row["period_end"], row["basis"])
                prev = deduped.get(key)
                if prev is not None:
                    # Misfile guard (RKFORGE 2026-08-08: same quarter filed
                    # once correct, once at exactly 10x; both internally
                    # consistent). When duplicates disagree wildly, trust
                    # the one closer to the symbol's own median revenue.
                    r_new, r_old = row.get("revenue"), prev.get("revenue")
                    if (r_new and r_old
                            and max(r_new, r_old) > 5 * min(r_new, r_old)):
                        others = [b["revenue"] for b in batch
                                  if b["symbol"] == row["symbol"]
                                  and b["period_end"] != row["period_end"]
                                  and b.get("revenue")]
                        if others:
                            med = sorted(others)[len(others) // 2]
                            if abs(r_old - med) < abs(r_new - med):
                                continue  # keep prev; drop the outlier
                deduped[key] = row
            batch = list(deduped.values())
            # Chunked: ~19 params/row against Postgres's 65,535-parameter
            # wire limit — a single statement died at corpus scale
            # (live crash 2026-08-08, 500-symbol backfill's final pass).
            CHUNK = 1500
            for i in range(0, len(batch), CHUNK):
                chunk = batch[i:i + CHUNK]
                stmt = pg_insert(FinancialsQuarterly).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_fin_symbol_period_basis",
                    set_={c: stmt.excluded[c] for c in
                          ("revenue", "other_income", "total_income", "expenses",
                           "finance_costs", "depreciation", "pbt", "tax", "pat",
                           "eps_basic", "audited", "raw", "filing_id",
                           "event_at", "security_id")},
                )
                db.execute(stmt)
            stats["loaded"] = len(batch)
        db.commit()
    log.info("a3_loaded", **stats)
    return stats
