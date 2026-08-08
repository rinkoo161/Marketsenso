"""A8 alerting + performance tracker over fixtures."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import marketsense.agents.a8_alerts.engine as a8
from marketsense.db.models import Alert, PriceDaily, Signal, SignalPerformance
from marketsense.performance.tracker import measure, summary

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def test_alert_logged_even_with_channels_disabled(db_factory, monkeypatch):
    # Channels MUST be stubbed: this test originally relied on "no token
    # configured", then the operator configured real Telegram credentials
    # in .env and the suite sent a live test alert to their phone
    # (2026-08-09). Tests never touch real channels again.
    monkeypatch.setattr(a8, "_send_telegram", lambda text, html=False: "disabled")
    monkeypatch.setattr(a8, "_send_webhook", lambda p: "disabled")
    with db_factory() as db:
        a8.raise_alert(db, severity="high", category="auditor_resignation",
                       symbol="X", message="test", evidence={"filing_id": 1})
        db.commit()
    with db_factory() as db:
        a = db.scalar(select(Alert))
        assert a.severity == "high"
        assert a.channels.get("telegram") == "disabled"
        assert a.evidence_ref == {"filing_id": 1}


def test_rate_limit_suppresses_after_budget(db_factory, monkeypatch):
    monkeypatch.setattr(a8, "_send_telegram", lambda text, html=False: "sent")
    monkeypatch.setattr(a8, "_send_webhook", lambda p: "disabled")
    from marketsense.core.config import settings

    budget = settings().alert_max_high_per_hour
    with db_factory() as db:
        for i in range(budget + 3):
            a8.raise_alert(db, severity="high", category="t", symbol=f"S{i}",
                           message="x")
            db.commit()
    with db_factory() as db:
        sup = db.scalars(select(Alert)).all()
        suppressed = [a for a in sup
                      if (a.channels or {}).get("suppressed") == "rate_limit"]
        assert len(suppressed) == 3


def test_performance_measures_only_elapsed_windows(db_factory):
    with db_factory() as db:
        # 30 bars of history after the signal → 1w and 4w measurable, 12w not
        for i in range(35):
            day = NOW - timedelta(days=34 - i)
            db.add(PriceDaily(symbol="PERFCO", trade_date=day,
                              close=100.0 + i, source="bhavcopy"))
            db.add(PriceDaily(symbol="IDX:Nifty 500", trade_date=day,
                              close=1000.0 + i, source="index"))
        db.add(Signal(symbol="PERFCO", profile="default", stance="buy",
                      conviction=75.0, confidence=0.5, model_version="t",
                      as_of=NOW - timedelta(days=30)))
        db.commit()
    r = measure(db_factory)
    assert r["measured"] == 2      # 1w + 4w
    assert r["pending"] >= 1       # 12w not elapsed
    with db_factory() as db:
        perfs = {p.window: p for p in db.scalars(select(SignalPerformance))}
    assert set(perfs) == {"1w", "4w"}
    assert perfs["1w"].ret > 0     # rising series
    assert perfs["1w"].excess is not None
    s = summary(db_factory)
    assert s["buy"]["1w"]["n"] == 1


def test_measure_is_idempotent(db_factory):
    test_performance_measures_only_elapsed_windows.__wrapped__ = None
    # fresh db via fixture; reuse the seeding from the prior test body
    with db_factory() as db:
        for i in range(35):
            day = NOW - timedelta(days=34 - i)
            db.add(PriceDaily(symbol="PERFCO", trade_date=day,
                              close=100.0, source="bhavcopy"))
        db.add(Signal(symbol="PERFCO", profile="default", stance="hold",
                      conviction=50.0, confidence=0.5, model_version="t",
                      as_of=NOW - timedelta(days=30)))
        db.commit()
    r1 = measure(db_factory)
    r2 = measure(db_factory)
    assert r2["measured"] == 0     # unique (signal, window) — no double rows
