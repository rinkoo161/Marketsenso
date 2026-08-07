"""A2 as a bus consumer over filing.received.

At-least-once + the (filing_id, model_version) unique constraint =
effectively exactly-once classification; a redelivered event finds the
row and no-ops. Dead-lettering isolates a poison filing without stopping
the stream (§3)."""
from __future__ import annotations

from marketsense.agents.a2_docintel.classifier import classify_filing
from marketsense.bus.outbox import Consumer, Outbox
from marketsense.core.logging import get_logger
from marketsense.db.models import Filing

log = get_logger("a2.consumer")


def make_consumer(session_factory) -> Consumer:
    def handle(evt: Outbox) -> None:
        filing_id = evt.payload.get("filing_id")
        if not filing_id:
            return
        with session_factory() as db:
            filing = db.get(Filing, filing_id)
            if filing is None:
                log.warning("filing_missing", filing_id=filing_id)
                return
            row = classify_filing(db, filing)
            db.commit()
            if row is not None and row.materiality >= 7:
                log.info("high_materiality", filing_id=filing_id,
                         symbol=filing.symbol, category=row.category,
                         materiality=row.materiality, engine=row.engine)

    return Consumer("a2", "filing.received", handle, session_factory,
                    batch_size=50)
