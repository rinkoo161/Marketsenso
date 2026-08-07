"""ms — the MarketSense CLI.

    ms run                          # start the ingest supervisor (foreground)
    ms filings RELIANCE --days 7    # recent filings for a symbol
    ms feeds                        # per-feed poller status
    ms budget                       # NSE budget/breaker state
    ms backfill --days 90           # announcements cold-start backfill
    ms verify --day 2026-08-03      # coverage vs NSE's own API
    ms master-sync                  # securities master refresh
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import typer

from marketsense.core.logging import setup_logging

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _init():
    setup_logging("WARNING")
    from marketsense.db.engine import session
    from marketsense.runtime import nse_client

    return session, nse_client()


@app.command()
def run() -> None:
    """Start the ingest supervisor (A1 + documents + hourly catch-up)."""
    from marketsense.supervisor import main

    main()


@app.command()
def filings(
    symbol: str = typer.Argument(..., help="Trading symbol, e.g. RELIANCE"),
    days: int = typer.Option(7, help="Look-back window"),
    feed: str = typer.Option(None, help="Restrict to one feed"),
) -> None:
    """Recent filings for a symbol — the Phase 1 acceptance query."""
    from datetime import timedelta

    from marketsense.db.pit import pit_filings

    session, _ = _init()
    now = datetime.now(timezone.utc)
    with session() as db:
        rows = pit_filings(db, as_of=now, symbol=symbol, feed=feed,
                           since=now - timedelta(days=days))
        if not rows:
            typer.echo(f"no filings for {symbol.upper()} in the last {days}d")
            raise typer.Exit()
        from marketsense.agents.a2_docintel.classifier import MODEL_VERSION
        from marketsense.db.models import FilingClassification as FC
        from sqlalchemy import select

        cls = {
            c.filing_id: c
            for c in db.scalars(select(FC).where(
                FC.filing_id.in_([f.id for f in rows]),
                FC.model_version == MODEL_VERSION))
        }
        for f in rows:
            ts = f.event_at.strftime("%d-%b %H:%M") if f.event_at else "        ?"
            c = cls.get(f.id)
            tag = f"{c.category[:18]:18} m{c.materiality}" if c else " " * 21
            typer.echo(f"{ts}  {tag}  [{f.feed:20}]  {(f.subject or '')[:70]}")


@app.command()
def pulse(
    hours: int = typer.Option(24, help="Look-back window"),
    min_mat: int = typer.Option(5, help="Minimum materiality"),
    limit: int = typer.Option(25),
) -> None:
    """High-materiality events — the Market Pulse view."""
    from datetime import timedelta

    from marketsense.db.pit import pit_classified

    session, _ = _init()
    now = datetime.now(timezone.utc)
    with session() as db:
        pairs = pit_classified(db, as_of=now, min_materiality=min_mat,
                               since=now - timedelta(hours=hours), limit=limit)
        if not pairs:
            typer.echo(f"nothing at materiality ≥{min_mat} in the last {hours}h")
            raise typer.Exit()
        for f, c in pairs:
            ts = f.event_at.strftime("%d-%b %H:%M") if f.event_at else "?"
            sent = "+" if c.sentiment > 0.15 else "-" if c.sentiment < -0.15 else "="
            typer.echo(f"m{c.materiality} {sent} {ts}  {(f.symbol or '?'):12} "
                       f"{c.category[:22]:22} {(c.summary or f.subject or '')[:70]}")


@app.command()
def feeds() -> None:
    """Per-feed poller status: last poll, last change, counts, errors."""
    from sqlalchemy import select

    from marketsense.db.models import FeedState

    session, _ = _init()
    with session() as db:
        states = {s.feed: s for s in db.scalars(select(FeedState))}
    from marketsense.agents.a1_ingestion.feeds import FEEDS

    typer.echo(f"{'feed':24}{'pri':5}{'last poll (UTC)':18}{'status':10}"
               f"{'seen':>8}{'new':>7}{'errs':>6}")
    for spec in FEEDS:
        s = states.get(spec.name)
        if s is None:
            typer.echo(f"{spec.name:24}{spec.priority:5}{'never':18}")
            continue
        last = s.last_polled_at.strftime("%d-%b %H:%M:%S") if s.last_polled_at else "never"
        typer.echo(
            f"{s.feed:24}{s.priority:5}{last:18}{(s.last_status or '')[:9]:10}"
            f"{s.entries_seen:>8}{s.entries_new:>7}{s.consecutive_errors:>6}"
        )


@app.command()
def budget() -> None:
    """NSE request budget and breaker state + last hour of audit stats."""
    from sqlalchemy import text

    session, client = _init()
    for k, v in client.budget.snapshot().items():
        typer.echo(f"{k:28}{v}")
    with session() as db:
        rows = db.execute(text(
            "select coalesce(status::text,'transport-error') s, count(*) "
            "from http_audit where observed_at > now() - interval '1 hour' "
            "group by 1 order by 2 desc"
        )).all()
    typer.echo("\nlast hour of requests:")
    for s, n in rows:
        typer.echo(f"  {s:20}{n}")


@app.command()
def backfill(
    days: int = typer.Option(90, help="Days to walk back"),
    symbol: str = typer.Option(None, help="Restrict to one symbol"),
) -> None:
    """Cold-start backfill of announcements from the NSE JSON API."""
    from marketsense.agents.a1_ingestion.backfill import backfill_announcements

    session, client = _init()
    setup_logging("INFO")
    stats = backfill_announcements(session, client, days=days, symbol=symbol)
    typer.echo(str(stats))


@app.command()
def verify(
    day: str = typer.Option(None, help="YYYY-MM-DD (default: yesterday)"),
    show_missing: int = typer.Option(10, help="How many gaps to list"),
) -> None:
    """Coverage vs NSE's own announcements API for one day."""
    from marketsense.agents.a1_ingestion.verify import verify_announcements

    session, client = _init()
    d = date.fromisoformat(day) if day else None
    r = verify_announcements(session, client, day=d)
    typer.echo(f"day={r['day']}  expected={r['expected']}  held={r['held']}  "
               f"coverage={r['coverage_pct']}%")
    for m in r["missing"][:show_missing]:
        typer.echo(f"  MISSING {m['an_dt']}  {m['symbol']}  {m['subject']}")


@app.command(name="master-sync")
def master_sync() -> None:
    """Refresh the securities master + rename histories."""
    from marketsense.universe.securities_master import (
        sync_change_histories,
        sync_equity_lists,
    )

    session, client = _init()
    with session() as db:
        typer.echo(str(sync_equity_lists(db, client)))
        typer.echo(str(sync_change_histories(db, client)))


@app.command()
def signals(
    stance: str = typer.Option(None, help="Filter: buy/accumulate/hold/reduce/exit"),
    limit: int = typer.Option(20),
) -> None:
    """Latest conviction signals, ranked."""
    from sqlalchemy import select

    from marketsense.db.models import Signal

    session, _ = _init()
    with session() as db:
        q = (select(Signal).order_by(Signal.as_of.desc(), Signal.conviction.desc())
             .limit(500))
        rows = list(db.scalars(q))
        latest: dict[str, Signal] = {}
        for s in rows:
            latest.setdefault(s.symbol, s)
        out = [s for s in latest.values() if not stance or s.stance == stance]
        out.sort(key=lambda s: -s.conviction)
        for s in out[:limit]:
            lv = (f"entry {s.entry_low}-{s.entry_high} tgt {s.target_low}-"
                  f"{s.target_high} stop {s.invalidation}"
                  if s.entry_low else "")
            typer.echo(f"{s.stance:10} {s.conviction:5.1f} conf {s.confidence:.2f} "
                       f"{s.symbol:12} [{s.risk_verdict:9}] "
                       f"size {s.size_pct or '-':>4}% {s.horizon:6} {lv}")


@app.command(name="backfill-financials")
def backfill_financials(
    top: int = typer.Option(500, help="Top-N symbols by median turnover"),
    quarters: int = typer.Option(8, help="Quarters of history"),
) -> None:
    """Historical quarterly results (classic + integrated-filing eras)."""
    from marketsense.agents.a3_fundamental.backfill import backfill_universe

    session, client = _init()
    setup_logging("INFO")
    typer.echo(str(backfill_universe(session, client, top=top,
                                     quarters_back=quarters)))


@app.command()
def serve(port: int = typer.Option(None)) -> None:
    """Start the health/metrics API."""
    import uvicorn

    from marketsense.core.config import settings

    s = settings()
    uvicorn.run("marketsense.api.app:app", host=s.api_host, port=port or s.api_port)


if __name__ == "__main__":
    app()
