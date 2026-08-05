"""Document queue semantics — written after the 2026-08-05 soak found a
404 attachment head-of-line-blocking the whole queue. The distinction
under test: 'deferred' (budget) stops the cycle, 'exhausted' (404) fails
ONE document and keeps draining."""
from __future__ import annotations

from sqlalchemy import select

from marketsense.agents.a1_ingestion.documents import DocumentFetcher
from marketsense.db.models import Document, Filing
from marketsense.net.nse_client import FetchResult, NSEUnavailable


class ScriptedClient:
    """NSEClient stand-in: url → FetchResult | exception to raise."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        action = self.script[url]
        if isinstance(action, Exception):
            raise action
        return action


def _seed_docs(db_factory, urls):
    with db_factory() as db:
        f = Filing(feed="announcements", content_hash="d" * 64, source="rss")
        db.add(f)
        db.flush()
        for u in urls:
            db.add(Document(filing_id=f.id, url=u))
        db.commit()


def _ok(url, body=b"%PDF-fake"):
    return FetchResult(200, body, None, None, not_modified=False, url=url, elapsed_ms=1)


def test_404_schedules_retry_and_queue_keeps_draining(db_factory, tmp_path, monkeypatch):
    """Soak finding #3: fresh XBRL 404s transiently (archives host is
    eventually consistent) → first exhaustion schedules a retry, it does
    NOT permanently fail, and it does NOT block the queue behind it."""
    from marketsense.core import config as cfg

    monkeypatch.setattr(type(cfg.settings()), "pdf_dir", tmp_path, raising=False)
    urls = ["https://x/a.pdf", "https://x/gone.pdf", "https://x/b.pdf"]
    _seed_docs(db_factory, urls)
    client = ScriptedClient({
        urls[0]: _ok(urls[0]),
        urls[1]: NSEUnavailable("404 after retries", kind="exhausted"),
        urls[2]: _ok(urls[2], b"%PDF-other"),
    })
    stats = DocumentFetcher(client, db_factory).drain()
    assert stats == {"fetched": 2, "failed": 0, "skipped_budget": 0, "retried": 1}
    with db_factory() as db:
        by_url = {d.url: d for d in db.scalars(select(Document))}
        assert by_url[urls[1]].fetch_status == "pending"       # retry, not dead
        assert by_url[urls[1]].attempts == 1
        assert by_url[urls[1]].next_attempt_at is not None     # backoff armed
        assert by_url[urls[2]].fetch_status == "fetched"  # NOT blocked behind the 404


def test_exhausting_the_ladder_fails_permanently(db_factory):
    from datetime import datetime, timedelta, timezone

    from marketsense.agents.a1_ingestion.documents import MAX_ATTEMPTS

    urls = ["https://x/dead.pdf"]
    _seed_docs(db_factory, urls)
    client = ScriptedClient({urls[0]: NSEUnavailable("404", kind="exhausted")})
    fetcher = DocumentFetcher(client, db_factory)
    for _ in range(MAX_ATTEMPTS):
        fetcher.drain()
        # collapse the backoff so the next drain sees the doc as due
        with db_factory() as db:
            d = db.scalar(select(Document))
            if d.next_attempt_at:
                d.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
    with db_factory() as db:
        d = db.scalar(select(Document))
        assert d.fetch_status == "failed"
        assert d.attempts == MAX_ATTEMPTS
    assert len(client.calls) == MAX_ATTEMPTS


def test_budget_deferral_stops_cycle_and_leaves_pending(db_factory):
    urls = ["https://x/c.pdf", "https://x/d.pdf"]
    _seed_docs(db_factory, urls)
    client = ScriptedClient({
        urls[0]: NSEUnavailable("budget exhausted", kind="deferred"),
        urls[1]: _ok(urls[1]),
    })
    stats = DocumentFetcher(client, db_factory).drain()
    assert stats == {"fetched": 0, "failed": 0, "skipped_budget": 1, "retried": 0}
    assert client.calls == [urls[0]]  # never touched the second
    with db_factory() as db:
        assert all(d.fetch_status == "pending" for d in db.scalars(select(Document)))


def test_backoff_gate_prevents_immediate_retry(db_factory):
    """A doc waiting out its backoff is invisible to the next drain —
    no budget burned re-asking for a file that just 404'd."""
    urls = ["https://x/gone2.pdf"]
    _seed_docs(db_factory, urls)
    client = ScriptedClient({urls[0]: NSEUnavailable("404", kind="exhausted")})
    DocumentFetcher(client, db_factory).drain()
    stats2 = DocumentFetcher(client, db_factory).drain()
    assert stats2 == {"fetched": 0, "failed": 0, "skipped_budget": 0, "retried": 0}
    assert len(client.calls) == 1  # backoff gate held
