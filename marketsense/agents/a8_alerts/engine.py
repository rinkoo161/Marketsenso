"""A8 — alert & delivery.

Triggers:
  * filing.classified with materiality ≥ 8         → HIGH
  * filing.classified with materiality ≥ 6         → MEDIUM
  * signal.issued: stance buy/exit or suppressed→* → MEDIUM (stance
    changes are decisions; hold-band churn is not alert-worthy)

Channels: the alerts TABLE always (it is the delivery log and the
dashboard feed); Telegram and a generic webhook when configured
(empty token/url = disabled — never an error). Every alert carries an
evidence_ref (filing/signal ids) so the dashboard can deep-link.

Rate limiting: at most alert_max_high_per_hour high pushes; overflow is
recorded with channels={"suppressed": "rate_limit"} and folded into the
next digest — alert fatigue is how real alerts get ignored.

Digests (called from the supervisor): pre-open 08:15 and post-close
16:30 IST summaries of new high-materiality events + stance changes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select

from marketsense.bus.outbox import Consumer, Outbox
from marketsense.core.config import settings
from marketsense.core.logging import get_logger
from marketsense.db.models import Alert

log = get_logger("a8")


# ---------------------------------------------------------------- channels

def _send_telegram(text: str) -> str:
    s = settings()
    if not s.telegram_bot_token or not s.telegram_chat_id:
        return "disabled"
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
            json={"chat_id": s.telegram_chat_id, "text": text,
                  "disable_web_page_preview": True},
            timeout=15)
        return "sent" if r.status_code == 200 else f"http_{r.status_code}"
    except httpx.HTTPError as e:
        return f"error:{str(e)[:60]}"


def _send_webhook(payload: dict) -> str:
    s = settings()
    if not s.alert_webhook_url:
        return "disabled"
    try:
        r = httpx.post(s.alert_webhook_url, json=payload, timeout=15)
        return "sent" if 200 <= r.status_code < 300 else f"http_{r.status_code}"
    except httpx.HTTPError as e:
        return f"error:{str(e)[:60]}"


def _high_count_last_hour(db) -> int:
    return db.scalar(
        select(func.count()).select_from(Alert)
        .where(Alert.severity == "high",
               Alert.observed_at >= datetime.now(timezone.utc) - timedelta(hours=1),
               Alert.channels["telegram"].as_string() == "sent")) or 0


def raise_alert(db, *, severity: str, category: str, symbol: str | None,
                message: str, evidence: dict | None = None) -> Alert:
    """Log always; push by severity + rate budget. Caller commits."""
    channels: dict = {}
    push = severity == "high"
    if push and _high_count_last_hour(db) >= settings().alert_max_high_per_hour:
        channels["suppressed"] = "rate_limit"
        push = False
    if push:
        text = f"[{severity.upper()}] {symbol or ''} {category}: {message}"
        channels["telegram"] = _send_telegram(text)
        channels["webhook"] = _send_webhook(
            {"severity": severity, "category": category, "symbol": symbol,
             "message": message, "evidence": evidence})
    alert = Alert(severity=severity, category=category, symbol=symbol,
                  message=message[:1000], evidence_ref=evidence,
                  channels=channels)
    db.add(alert)
    return alert


# --------------------------------------------------------------- consumers

def make_classified_consumer(session_factory) -> Consumer:
    def handle(evt: Outbox) -> None:
        p = evt.payload
        m = p.get("materiality") or 0
        if m < 6 or p.get("routine"):
            return
        severity = "high" if m >= 8 else "medium"
        with session_factory() as db:
            raise_alert(
                db, severity=severity, category=p.get("category", "event"),
                symbol=p.get("symbol"),
                message=f"materiality {m} filing "
                        f"({p.get('category')}), sentiment {p.get('sentiment')}",
                evidence={"filing_id": p.get("filing_id"),
                          "classification_id": p.get("classification_id")})
            db.commit()

    return Consumer("a8_classified", "filing.classified", handle,
                    session_factory, batch_size=100)


def make_signal_consumer(session_factory) -> Consumer:
    def handle(evt: Outbox) -> None:
        p = evt.payload
        stance = p.get("stance")
        if stance not in ("buy", "exit", "suppressed"):
            return
        with session_factory() as db:
            raise_alert(
                db, severity="medium", category=f"signal_{stance}",
                symbol=p.get("symbol"),
                message=f"{stance} @ conviction {p.get('conviction')} "
                        f"({p.get('profile')}), risk {p.get('risk_verdict')}",
                evidence={"signal_id": p.get("signal_id")})
            db.commit()

    return Consumer("a8_signals", "signal.issued", handle,
                    session_factory, batch_size=100)


# ----------------------------------------------------------------- digests

def digest(db_factory, *, kind: str) -> dict:
    """kind: 'pre_open' | 'post_close'. Summarises the window since the
    previous digest of this kind."""
    since = datetime.now(timezone.utc) - timedelta(hours=20)
    with db_factory() as db:
        highs = db.scalars(
            select(Alert).where(Alert.observed_at >= since,
                                Alert.severity == "high",
                                Alert.category != "digest")
            .order_by(Alert.observed_at.desc()).limit(15)).all()
        stances = db.scalars(
            select(Alert).where(Alert.observed_at >= since,
                                Alert.category.like("signal_%"))
            .order_by(Alert.observed_at.desc()).limit(15)).all()
        lines = [f"MarketSense {kind} digest"]
        if highs:
            lines.append(f"High-materiality events ({len(highs)}):")
            lines += [f"  {a.symbol or '?'}: {a.message[:80]}" for a in highs[:8]]
        if stances:
            lines.append(f"Stance changes ({len(stances)}):")
            lines += [f"  {a.symbol}: {a.message[:70]}" for a in stances[:8]]
        if len(lines) == 1:
            lines.append("quiet window — no high-materiality events, "
                         "no stance changes")
        text = "\n".join(lines)
        channels = {"telegram": _send_telegram(text),
                    "webhook": _send_webhook({"digest": kind, "text": text})}
        db.add(Alert(severity="low", category="digest", symbol=None,
                     message=text[:1000], channels=channels))
        db.commit()
    log.info("digest_sent", kind=kind, highs=len(highs), stances=len(stances))
    return {"kind": kind, "highs": len(highs), "stances": len(stances)}
