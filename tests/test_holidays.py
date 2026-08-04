"""Holiday refresh — fixture mirrors the live holiday-master shape
(verified 2026-08-05), including the ad-hoc election holiday the static
seed provably lacked."""
from __future__ import annotations

from datetime import date

from marketsense.core.clock import calendar
from marketsense.universe.holidays import load_holidays_from_db, refresh_holidays

API_FIXTURE = {
    "CM": [
        {"tradingDate": "15-Jan-2026", "weekDay": "Thursday",
         "description": "Municipal Corporation Election - Maharashtra",
         "morning_session": None, "evening_session": None, "Sr_no": 1},
        {"tradingDate": "26-Jan-2026", "weekDay": "Monday",
         "description": "Republic Day", "Sr_no": 2},
        {"tradingDate": "garbage-date", "description": "bad row survives"},
    ],
    "FO": [{"tradingDate": "01-Jan-2099", "description": "wrong segment"}],
}


class OneShotClient:
    def __init__(self, payload):
        self.payload = payload

    def get_json(self, url, **kw):
        return self.payload


def test_refresh_learns_adhoc_holiday_and_ignores_bad_rows(db_factory):
    baseline = dict(calendar.holidays)
    try:
        assert date(2026, 1, 15) not in calendar.holidays  # the gap that motivated this
        stats = refresh_holidays(db_factory, OneShotClient(API_FIXTURE))
        assert stats["inserted"] == 2  # bad row skipped, FO segment ignored
        assert calendar.holidays[date(2026, 1, 15)].startswith("Municipal")
        assert not calendar.is_trading_day(date(2026, 1, 15))
        # seed entries survive the merge
        assert date(2026, 12, 25) in calendar.holidays
    finally:
        calendar.update_holidays(baseline)


def test_load_from_db_overlays_learned_holidays(db_factory):
    baseline = dict(calendar.holidays)
    try:
        refresh_holidays(db_factory, OneShotClient(API_FIXTURE))
        calendar.update_holidays(baseline)          # simulate fresh process
        assert date(2026, 1, 15) not in calendar.holidays
        n = load_holidays_from_db(db_factory)       # startup path, no HTTP
        assert date(2026, 1, 15) in calendar.holidays
        assert n == len(calendar.holidays)
    finally:
        calendar.update_holidays(baseline)


def test_refresh_is_idempotent(db_factory):
    baseline = dict(calendar.holidays)
    try:
        s1 = refresh_holidays(db_factory, OneShotClient(API_FIXTURE))
        s2 = refresh_holidays(db_factory, OneShotClient(API_FIXTURE))
        assert s1["inserted"] == 2 and s2["inserted"] == 0
    finally:
        calendar.update_holidays(baseline)
