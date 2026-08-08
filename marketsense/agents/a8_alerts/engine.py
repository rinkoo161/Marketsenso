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

def _send_telegram(text: str, html: bool = False) -> str:
    s = settings()
    if not s.telegram_bot_token or not s.telegram_chat_id:
        return "disabled"
    payload = {"chat_id": s.telegram_chat_id, "text": text,
               "disable_web_page_preview": True}
    if html:
        payload["parse_mode"] = "HTML"
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
            json=payload, timeout=15)
        return "sent" if r.status_code == 200 else f"http_{r.status_code}"
    except httpx.HTTPError as e:
        return f"error:{str(e)[:60]}"


def _esc(t) -> str:
    return (str(t or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _fmt_range(lo, hi) -> str:
    if lo is None:
        return "–"
    return f"{lo:g} – {hi:g}" if hi is not None else f"{lo:g}"


def format_event_push(db, p: dict) -> str:
    """Multi-row HTML push for a classified filing: what happened, the
    expected impact, and what the standing signal says to DO about it —
    every number from stored records (user requirement 2026-08-09)."""
    from sqlalchemy import select

    from marketsense.db.models import (
        FilingClassification,
        Security,
        Signal,
    )

    sym = p.get("symbol") or "?"
    sent = p.get("sentiment") or 0
    m = p.get("materiality") or 0
    sev_icon = "🔴" if m >= 8 else "🟠"
    dir_icon = "📈 positive" if sent > 0.15 else (
        "📉 negative" if sent < -0.15 else "➖ neutral")

    name = db.scalar(select(Security.company_name)
                     .where(Security.symbol == sym)) or ""
    summary = None
    if p.get("classification_id"):
        c = db.get(FilingClassification, p["classification_id"])
        summary = c.summary if c else None

    lines = [
        f"{sev_icon} <b>{_esc(sym)}</b>" + (f" · {_esc(name[:40])}" if name else ""),
        f"<b>Event:</b> {_esc(p.get('category'))}  (materiality {m}/10)",
        f"<b>Expected impact:</b> {dir_icon}",
    ]
    if summary:
        lines.append(f"<b>What happened:</b> {_esc(summary[:180])}")

    sig = db.scalars(select(Signal).where(Signal.symbol == sym,
                                          Signal.profile == "default")
                     .order_by(Signal.as_of.desc()).limit(1)).first()
    if sig:
        lines.append(f"<b>Standing view:</b> {sig.stance.capitalize()} "
                     f"(conviction {sig.conviction:g}, "
                     f"confidence {round(100 * (sig.confidence or 0))}%)")
        if sig.stance in ("buy", "accumulate") and sent < -0.3:
            lines.append("⚠️ <b>Adverse news against a long stance — "
                         "review before acting</b>")
        if sig.invalidation is not None:
            lines.append(f"<b>Stop:</b> {sig.invalidation:g}")
    else:
        lines.append("<i>No standing signal for this symbol yet</i>")
    lines.append(f"<i>evidence: filing #{p.get('filing_id')}</i>")
    return "\n".join(lines)


def format_signal_push(db, p: dict) -> str:
    """Multi-row HTML push for a stance change: the decision up top,
    parameters one per row."""
    from sqlalchemy import select

    from marketsense.db.models import Security, Signal

    sig = db.get(Signal, p.get("signal_id")) if p.get("signal_id") else None
    sym = p.get("symbol") or "?"
    name = db.scalar(select(Security.company_name)
                     .where(Security.symbol == sym)) or ""
    stance = (p.get("stance") or "").capitalize()
    icon = {"Buy": "🟢", "Exit": "🔴", "Suppressed": "🚫"}.get(stance, "🟡")

    lines = [f"{icon} <b>{stance}: {_esc(sym)}</b>"
             + (f" · {_esc(name[:40])}" if name else "")]
    if sig:
        lines += [
            f"<b>Horizon:</b> {_esc(sig.horizon)} ({_esc(sig.profile)})",
            f"<b>Conviction:</b> {sig.conviction:g}/100 · "
            f"<b>Confidence:</b> {round(100 * (sig.confidence or 0))}%",
            f"<b>Risk check:</b> {_esc(sig.risk_verdict)}",
        ]
        if sig.entry_low is not None:
            lines.append(f"<b>Entry zone:</b> "
                         f"{_fmt_range(sig.entry_low, sig.entry_high)}")
        if sig.target_low is not None:
            lines.append(f"<b>Target zone:</b> "
                         f"{_fmt_range(sig.target_low, sig.target_high)}")
        if sig.invalidation is not None:
            lines.append(f"<b>Stop loss:</b> {sig.invalidation:g}")
        if sig.size_pct is not None:
            lines.append(f"<b>Position size:</b> {sig.size_pct:g}% of capital")
        t = sig.thesis or {}
        if t.get("for"):
            lines.append(f"<b>Why:</b> {_esc(t['for'][0][:140])}")
        if t.get("view_changer"):
            lines.append(f"<b>View changes if:</b> {_esc(t['view_changer'][:120])}")
        lines.append(f"<i>evidence: signal #{sig.id}</i>")
    else:
        lines.append(f"conviction {p.get('conviction')} "
                     f"({_esc(p.get('profile'))}), risk {_esc(p.get('risk_verdict'))}")
    return "\n".join(lines)


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
                message: str, evidence: dict | None = None,
                push_html: str | None = None) -> Alert:
    """Log always; push by severity + rate budget. Caller commits.
    push_html: rich multi-row HTML for Telegram (user requirement
    2026-08-09: informative pushes with impact, decision, and parameters
    on separate rows); falls back to the plain one-liner when absent."""
    channels: dict = {}
    push = severity == "high"
    if push and _high_count_last_hour(db) >= settings().alert_max_high_per_hour:
        channels["suppressed"] = "rate_limit"
        push = False
    if push:
        if push_html:
            channels["telegram"] = _send_telegram(push_html, html=True)
        else:
            channels["telegram"] = _send_telegram(
                f"[{severity.upper()}] {symbol or ''} {category}: {message}")
        channels["webhook"] = _send_webhook(
            {"severity": severity, "category": category, "symbol": symbol,
             "message": message, "evidence": evidence})
    alert = Alert(severity=severity, category=category, symbol=symbol,
                  message=(push_html or message)[:1000], evidence_ref=evidence,
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
                          "classification_id": p.get("classification_id")},
                push_html=format_event_push(db, p))
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
                evidence={"signal_id": p.get("signal_id")},
                push_html=format_signal_push(db, p))
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
