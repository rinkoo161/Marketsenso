"""Phase 1 API — agent health, feed status, filings query, Prometheus metrics.

Read-only over the DB. The ingest supervisor is a separate process; this
app carries no NSEClient and can never spend NSE budget. That is also
what makes it safe for ltp-monitor to poll (§6 read-only integration):
a dashboard refresh loop cannot translate into exchange requests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select, text

from marketsense import __version__
from marketsense.db.engine import session
from marketsense.db.models import AgentRun, FeedState, Filing, Outbox
from marketsense.db.pit import pit_classified, pit_filings

app = FastAPI(title="MarketSense", version=__version__)

STALE_AFTER = {"P0": 300, "P1": 1800, "P2": 7200}  # sec since last poll = unhealthy


@app.get("/", include_in_schema=False)
def dashboard():
    from pathlib import Path

    from fastapi.responses import FileResponse

    return FileResponse(Path(__file__).parent / "static" / "dashboard.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    """Overall + per-feed health. 'degraded' names its causes."""
    now = datetime.now(timezone.utc)
    problems: list[str] = []
    with session() as db:
        states = list(db.scalars(select(FeedState)))
        last_run = db.scalar(
            select(func.max(AgentRun.finished_at)).where(AgentRun.agent == "a1_poller")
        )
    if not states:
        problems.append("no feed_state rows — supervisor has never run")
    for s in states:
        if s.last_polled_at is None:
            problems.append(f"{s.feed}: never polled")
            continue
        age = (now - s.last_polled_at).total_seconds()
        if age > STALE_AFTER.get(s.priority, 3600):
            problems.append(f"{s.feed}: last poll {int(age)}s ago")
        if s.consecutive_errors >= 3:
            problems.append(f"{s.feed}: {s.consecutive_errors} consecutive errors")
    return {
        "status": "degraded" if problems else "ok",
        "version": __version__,
        "supervisor_last_pass": last_run.isoformat() if last_run else None,
        "problems": problems,
    }


@app.get("/api/feeds")
def feeds():
    with session() as db:
        return [
            {
                "feed": s.feed,
                "priority": s.priority,
                "last_polled_at": s.last_polled_at,
                "last_changed_at": s.last_changed_at,
                "last_status": s.last_status,
                "entries_seen": s.entries_seen,
                "entries_new": s.entries_new,
                "consecutive_errors": s.consecutive_errors,
            }
            for s in db.scalars(select(FeedState).order_by(FeedState.feed))
        ]


@app.get("/api/filings/{symbol}")
def filings(symbol: str, days: int = Query(7, le=365), feed: str | None = None):
    now = datetime.now(timezone.utc)
    with session() as db:
        rows = pit_filings(db, as_of=now, symbol=symbol, feed=feed,
                           since=now - timedelta(days=days))
        if not rows:
            raise HTTPException(404, f"no filings for {symbol.upper()} in {days}d")
        return [
            {
                "id": f.id,
                "feed": f.feed,
                "symbol": f.symbol,
                "subject": f.subject,
                "event_at": f.event_at,
                "observed_at": f.observed_at,
                "link": f.link,
                "source": f.source,
            }
            for f in rows
        ]


@app.get("/api/pulse")
def pulse(hours: int = Query(24, le=168), min_materiality: int = Query(5, ge=0, le=10),
          limit: int = Query(50, le=200)):
    """High-materiality classified events — Market Pulse. This is also the
    endpoint ltp-monitor's Phase 5 poller will read for event_flag."""
    now = datetime.now(timezone.utc)
    with session() as db:
        pairs = pit_classified(db, as_of=now, min_materiality=min_materiality,
                               since=now - timedelta(hours=hours), limit=limit)
        return [
            {
                "filing_id": f.id,
                "symbol": f.symbol,
                "category": c.category,
                "materiality": c.materiality,
                "sentiment": c.sentiment,
                "confidence": c.confidence,
                "summary": c.summary or f.subject,
                "engine": c.engine,
                "event_at": f.event_at,
                "link": f.link,
            }
            for f, c in pairs
        ]


@app.get("/api/signals")
def signals(stance: str | None = None, min_conviction: float = Query(0, le=100),
            limit: int = Query(50, le=1000)):
    """Latest signal per symbol, ranked by conviction. Suppressed rows
    included — A6 vetoes are information, not absence. Each row carries
    `news_flag`: the freshest adverse high-materiality event (≤7d,
    m≥7, sentiment ≤ −0.3) so the UI can indicate sell/caution pressure
    from news against the standing stance."""
    from marketsense.agents.a2_docintel.classifier import MODEL_VERSION as A2_V
    from marketsense.db.models import Filing, FilingClassification, Signal
    from sqlalchemy import select

    with session() as db:
        rows = list(db.scalars(
            select(Signal).order_by(Signal.as_of.desc()).limit(2000)))
        # latest per symbol, PREFERRING the default profile — the short
        # profile re-issues later in the same EOD run and was shadowing
        # default rows (JSWENERGY showed short/hold 59.8 while its default
        # accumulate 64.7 was the intended list entry). Company detail
        # shows every profile regardless.
        latest: dict[str, object] = {}
        for s in rows:  # rows are newest-first
            cur = latest.get(s.symbol)
            if cur is None:
                latest[s.symbol] = s
            elif s.profile == "default" and cur.profile != "default":
                latest[s.symbol] = s  # default outranks other profiles
        candidates = [s for s in latest.values()
                      if s.conviction >= min_conviction
                      and (not stance or s.stance == stance)]
        candidates.sort(key=lambda s: -s.conviction)

        # Adverse-news flags across ALL signal-bearing symbols — computed
        # BEFORE the ranking cut, because an adverse event drags conviction
        # down and would sort exactly the rows that need a sell indication
        # out of view (live finding: EMCURE/EIHOTEL flagged but unranked).
        d7 = datetime.now(timezone.utc) - timedelta(days=7)
        flags: dict[str, dict] = {}
        if candidates:
            adverse = db.execute(
                select(Filing.symbol, FilingClassification.category,
                       FilingClassification.materiality,
                       FilingClassification.sentiment,
                       FilingClassification.observed_at)
                .join(Filing, Filing.id == FilingClassification.filing_id)
                .where(Filing.symbol.in_([s.symbol for s in candidates]),
                       FilingClassification.model_version == A2_V,
                       FilingClassification.observed_at >= d7,
                       FilingClassification.materiality >= 7,
                       FilingClassification.sentiment <= -0.3)
                .order_by(FilingClassification.materiality.desc())).all()
            for sym, cat, mat, sent, at in adverse:
                flags.setdefault(sym, {
                    "category": cat, "materiality": mat, "sentiment": sent,
                    "days_ago": (datetime.now(timezone.utc) - at).days})

        # top-N by conviction ∪ every flagged row (sell indications must
        # never be ranked out of the table)
        out = candidates[:limit]
        seen = {s.symbol for s in out}
        out += [s for s in candidates[limit:] if s.symbol in flags
                and s.symbol not in seen]

    def _upside(s) -> float | None:
        # conservative: low end of target vs high end of entry
        if s.target_low and s.entry_high and s.entry_high > 0:
            return round(100.0 * (s.target_low / s.entry_high - 1.0), 1)
        return None

    return [
        {
            "symbol": s.symbol, "stance": s.stance,
            "conviction": s.conviction, "confidence": s.confidence,
            "risk_verdict": s.risk_verdict, "horizon": s.horizon,
            "entry": [s.entry_low, s.entry_high],
            "target": [s.target_low, s.target_high],
            "invalidation": s.invalidation, "size_pct": s.size_pct,
            "upside_pct": _upside(s),
            "news_flag": flags.get(s.symbol),
            "thesis": s.thesis, "as_of": s.as_of, "signal_id": s.id,
        }
        for s in out
    ]


@app.get("/api/company/{symbol}")
def company(symbol: str):
    """Everything the detail pane needs: signals (all profiles), scores,
    quarterly financials, recent filings, price context. Balance-sheet
    facts are served when present in the XBRL raw; quarterly filings are
    P&L-only, so most quarters honestly return none."""
    from sqlalchemy import select

    from marketsense.db.models import (
        FinancialsQuarterly,
        PriceDaily,
        Score,
        Signal,
    )

    sym = symbol.upper()
    now = datetime.now(timezone.utc)
    with session() as db:
        signals = {}
        for s in db.scalars(select(Signal).where(Signal.symbol == sym)
                            .order_by(Signal.as_of.desc()).limit(20)):
            signals.setdefault(s.profile, {
                "stance": s.stance, "conviction": s.conviction,
                "confidence": s.confidence, "horizon": s.horizon,
                "entry": [s.entry_low, s.entry_high],
                "target": [s.target_low, s.target_high],
                "invalidation": s.invalidation, "size_pct": s.size_pct,
                "risk_verdict": s.risk_verdict, "thesis": s.thesis,
                "as_of": s.as_of})
        scores = {}
        for agent in ("a3", "a4", "a5", "a6"):
            row = db.scalars(select(Score).where(Score.agent == agent,
                                                 Score.symbol == sym)
                             .order_by(Score.as_of.desc()).limit(1)).first()
            if row:
                scores[agent] = {"score": row.score, "label": row.label,
                                 "confidence": row.confidence,
                                 "as_of": row.as_of,
                                 "components": row.components}
        fins = [
            {"period_end": q.period_end.date().isoformat(), "basis": q.basis,
             "revenue_cr": round(q.revenue / 1e7, 1) if q.revenue else None,
             "pat_cr": round(q.pat / 1e7, 1) if q.pat else None,
             "eps": q.eps_basic, "audited": q.audited,
             "filing_id": q.filing_id,
             # balance-sheet facts only exist in H1/annual instances
             "paid_up_capital": (q.raw or {}).get("PaidUpValueOfEquityShareCapital")}
            for q in db.scalars(
                select(FinancialsQuarterly)
                .where(FinancialsQuarterly.symbol == sym)
                .order_by(FinancialsQuarterly.period_end.desc()).limit(12))
        ]
        filings = pit_filings(db, as_of=now, symbol=sym,
                              since=now - timedelta(days=90), limit=15)
        prices = db.scalars(
            select(PriceDaily).where(PriceDaily.symbol == sym,
                                     PriceDaily.source == "bhavcopy")
            .order_by(PriceDaily.trade_date.desc()).limit(260)).all()
        closes = [{"d": p.trade_date.date().isoformat(), "c": p.close}
                  for p in reversed(prices[:90])]

        # ---- shareholding pattern series (promoter/public %, deltas) ----
        import re as _re

        from marketsense.db.models import Filing as _F

        _pr = _re.compile(r"PR_AND_PRGRP:\s*([\d.]+)")
        _pub = _re.compile(r"PUBLIC_VAL:\s*([\d.]+)")
        _asof = _re.compile(r"AS ON DATE\s*:\s*([0-9]{2}-[A-Za-z]{3}-[0-9]{4})")
        sh_rows = db.execute(
            select(_F.event_at, _F.subject, _F.description)
            .where(_F.feed == "shareholding_pattern", _F.symbol == sym)
            .order_by(_F.event_at)).all()
        shareholding = []
        for ts, subject, desc in sh_rows:
            text = (subject or "") + " " + (desc or "")
            pr, pub = _pr.search(text), _pub.search(text)
            aso = _asof.search(text)
            if pr:
                shareholding.append({
                    "as_of": aso.group(1) if aso else
                             (ts.date().isoformat() if ts else "?"),
                    "promoter_pct": float(pr.group(1)),
                    "public_pct": float(pub.group(1)) if pub else None})
        for i in range(1, len(shareholding)):
            shareholding[i]["promoter_delta"] = round(
                shareholding[i]["promoter_pct"]
                - shareholding[i - 1]["promoter_pct"], 2)

        # ---- ratios strip (only what the data honestly supports) ----
        ratios: dict = {}
        all_closes = [p.close for p in prices if p.close]
        if all_closes:
            ratios["price"] = all_closes[0]
            ratios["high_52w"] = max(all_closes)
            ratios["low_52w"] = min(all_closes)
        cons_q = [q for q in db.scalars(
            select(FinancialsQuarterly)
            .where(FinancialsQuarterly.symbol == sym,
                   FinancialsQuarterly.basis == "consolidated")
            .order_by(FinancialsQuarterly.period_end.desc()).limit(4))]
        if len(cons_q) == 4 and all(q.eps_basic for q in cons_q):
            ttm_eps = sum(q.eps_basic for q in cons_q)
            ratios["eps_ttm"] = round(ttm_eps, 2)
            if all_closes and ttm_eps:
                ratios["pe_ttm"] = round(all_closes[0] / ttm_eps, 1)
        raw0 = (cons_q[0].raw or {}) if cons_q else {}
        try:
            paidup = float(raw0.get("PaidUpValueOfEquityShareCapital", 0))
            face = float(raw0.get("FaceValueOfEquityShareCapital", 0))
            if paidup > 0 and face > 0 and all_closes:
                ratios["mcap_cr"] = round(paidup / face * all_closes[0] / 1e7)
                ratios["face_value"] = face
        except (TypeError, ValueError):
            pass

        # ---- industry peers with our scores ----
        from marketsense.db.models import Security as _S

        me = db.scalar(select(_S).where(_S.symbol == sym))
        industry = (me.extra or {}).get("industry") if me else None
        peers = []
        if industry:
            peer_syms = [s for (s,) in db.execute(
                select(_S.symbol).where(
                    _S.extra["industry"].as_string() == industry,
                    _S.symbol != sym))][:12]
            for ps in peer_syms:
                row = {"symbol": ps}
                for agent in ("a4", "a3"):
                    sc = db.scalars(select(Score).where(
                        Score.agent == agent, Score.symbol == ps)
                        .order_by(Score.as_of.desc()).limit(1)).first()
                    if sc:
                        row[agent] = round(sc.score)
                sig = db.scalars(select(Signal).where(Signal.symbol == ps)
                                 .order_by(Signal.as_of.desc()).limit(1)).first()
                if sig:
                    row["stance"] = sig.stance
                    row["conviction"] = sig.conviction
                if len(row) > 1:
                    peers.append(row)
            peers.sort(key=lambda r: -(r.get("conviction") or 0))
    if not (signals or scores or fins or closes):
        raise HTTPException(404, f"nothing known about {sym}")
    return {
        "symbol": sym, "signals": signals, "scores": scores,
        "financials": fins,
        "balance_sheet": "not yet parsed (H1/annual instances only; "
                         "quarterly XBRL is P&L-only)",
        "filings": [{"id": f.id, "feed": f.feed, "subject": f.subject,
                     "event_at": f.event_at, "link": f.link}
                    for f in filings],
        "prices": closes,
        "shareholding": shareholding[-8:],
        "ratios": ratios,
        "industry": industry,
        "peers": peers[:8],
    }


@app.get("/api/performance")
def performance():
    """§7.7 — observed signal performance by stance × window."""
    from marketsense.performance.tracker import summary

    return summary(session)


@app.get("/api/alerts")
def alerts(severity: str | None = None, limit: int = Query(50, le=200)):
    from marketsense.db.models import Alert
    from sqlalchemy import select

    with session() as db:
        q = select(Alert).order_by(Alert.observed_at.desc()).limit(limit)
        if severity:
            q = q.where(Alert.severity == severity)
        return [
            {"id": a.id, "severity": a.severity, "category": a.category,
             "symbol": a.symbol, "message": a.message,
             "evidence": a.evidence_ref, "channels": a.channels,
             "at": a.observed_at}
            for a in db.scalars(q)
        ]


@app.get("/api/stats")
def stats():
    with session() as db:
        return {
            "filings": db.scalar(select(func.count()).select_from(Filing)),
            "filings_by_source": dict(db.execute(
                text("select source, count(*) from filings group by 1")).all()),
            "resolved_pct": db.scalar(text(
                "select round(100.0 * count(security_id) / greatest(count(*),1), 1) "
                "from filings")),
            "events": db.scalar(select(func.count()).select_from(Outbox)),
            "documents": dict(db.execute(
                text("select fetch_status, count(*) from documents group by 1")).all()),
        }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    """Prometheus exposition, computed from the DB at scrape time."""
    now = datetime.now(timezone.utc)
    lines: list[str] = []

    def emit(name: str, value, labels: str = "") -> None:
        lines.append(f"marketsense_{name}{{{labels}}} {value}"
                     if labels else f"marketsense_{name} {value}")

    with session() as db:
        emit("filings_total", db.scalar(select(func.count()).select_from(Filing)))
        emit("outbox_events_total", db.scalar(select(func.count()).select_from(Outbox)))
        for s in db.scalars(select(FeedState)):
            lbl = f'feed="{s.feed}",priority="{s.priority}"'
            if s.last_polled_at:
                emit("feed_poll_age_seconds",
                     int((now - s.last_polled_at).total_seconds()), lbl)
            emit("feed_entries_new_total", s.entries_new, lbl)
            emit("feed_consecutive_errors", s.consecutive_errors, lbl)
        for status, n in db.execute(text(
            "select coalesce(status::text,'transport_error'), count(*) from http_audit "
            "where observed_at > now() - interval '1 hour' group by 1"
        )).all():
            emit("http_requests_last_hour", n, f'status="{status}"')
        # A2 health: per-engine counts + consumer lag
        for engine, n in db.execute(text(
            "select engine, count(*) from filing_classifications group by 1")).all():
            emit("a2_classifications_total", n, f'engine="{engine}"')
        lag = db.execute(text(
            "select coalesce((select max(id) from outbox where topic='filing.received'),0)"
            " - coalesce((select last_acked_id from consumer_offsets"
            "   where consumer='a2' and topic='filing.received'),0)")).scalar()
        emit("a2_consumer_lag_events", lag or 0)
    return "\n".join(lines) + "\n"
