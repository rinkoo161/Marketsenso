"""Holiday refresh from NSE's holiday-master API.

Why this exists: the static seed in core/clock.py was proven incomplete
on 2026-08-05 — the live API lists "15-Jan-2026 Municipal Corporation
Election - Maharashtra", which no circular-derived seed had. Ad-hoc
holidays (elections, mourning days) appear mid-year, so a calendar that
never refreshes will happily poll at P0 cadence on a closed day and,
worse, let later phases mistake a holiday for a no-data outage.

Shape (verified live 2026-08-05): GET /api/holiday-master?type=trading →
{"CM": [{"tradingDate": "15-Jan-2026", "description": ...}, ...], "FO":
[...], ...}. CM = capital market segment, current calendar year only —
so the DB accumulates years, and the in-memory calendar is seed ∪ DB
with DB winning on conflicts.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from marketsense.core.clock import calendar
from marketsense.core.logging import get_logger
from marketsense.db.models import Holiday
from marketsense.net.nse_client import NSE_WWW, NSEClient

log = get_logger("holidays")

HOLIDAY_API = NSE_WWW + "/api/holiday-master?type=trading"


def refresh_holidays(db_factory, client: NSEClient) -> dict:
    """Fetch → upsert `holidays` → swap the live calendar. Returns stats.
    Raises on fetch failure — the caller records the error; the calendar
    keeps its current (seed ∪ previously-stored) table, which is the
    correct degraded behaviour."""
    data = client.get_json(HOLIDAY_API)
    rows = data.get("CM") or []
    stats = {"api_rows": len(rows), "inserted": 0, "total_known": 0}

    with db_factory() as db:
        for row in rows:
            try:
                d = datetime.strptime(row["tradingDate"].strip(), "%d-%b-%Y").replace(
                    tzinfo=timezone.utc
                )
            except (KeyError, ValueError):
                continue
            name = (row.get("description") or "").strip() or "holiday"
            if db.get(Holiday, (d, "CM")) is None:
                db.add(Holiday(day=d, name=name, segment="CM"))
                stats["inserted"] += 1
                log.info("holiday_learned", day=d.date().isoformat(), name=name)
        db.commit()

        merged = dict(calendar.holidays)  # seed + anything learned earlier
        for h in db.scalars(select(Holiday).where(Holiday.segment == "CM")):
            merged[h.day.date()] = h.name
        calendar.update_holidays(merged)
        stats["total_known"] = len(merged)

    return stats


def load_holidays_from_db(db_factory) -> int:
    """Startup path: overlay previously-learned holidays onto the seed
    without touching NSE. Returns how many the calendar now knows."""
    with db_factory() as db:
        merged = dict(calendar.holidays)
        for h in db.scalars(select(Holiday).where(Holiday.segment == "CM")):
            merged[h.day.date()] = h.name
    calendar.update_holidays(merged)
    return len(merged)
