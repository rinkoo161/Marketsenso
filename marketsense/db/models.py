"""Phase 1 schema.

The one structural rule, enforced here and checked by tests/test_pit.py:
every fact table carries BOTH

    event_at    — when the fact happened in the world (exchange timestamp,
                  broadcast time, filing date). May be NULL only when the
                  source genuinely gives no timestamp.
    observed_at — when THIS SYSTEM first saw it. Never NULL, set by us.

Backtests and any "as of" query filter on observed_at, never event_at —
that is what makes look-ahead leakage structurally impossible rather than
a convention someone forgets. See db/pit.py.

Scores/signals tables arrive in later phases but will follow the same
rule plus model_version (append-only).
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _observed_at() -> Mapped[datetime]:
    """The system-observation timestamp. server_default so even raw SQL
    inserts cannot produce a row without one."""
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


# ============================================================ securities

class Security(Base):
    """One row per listed instrument (main board + SME), keyed by ISIN
    where available. Renames land in SecurityAlias, not new rows."""

    __tablename__ = "securities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    isin: Mapped[str | None] = mapped_column(String(12), unique=True)
    company_name: Mapped[str] = mapped_column(Text)
    series: Mapped[str | None] = mapped_column(String(8))       # EQ, BE, SM, ST…
    is_sme: Mapped[bool] = mapped_column(Boolean, default=False)
    listing_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    face_value: Mapped[float | None]
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|suspended|delisted
    extra: Mapped[dict | None] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = _observed_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("symbol", "series", name="uq_symbol_series"),)


class SecurityAlias(Base):
    """Old symbols / old names so historical filings resolve. A rename is
    an alias row + an update to Security.symbol, never a second Security."""

    __tablename__ = "security_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"), index=True)
    alias: Mapped[str] = mapped_column(String(64), index=True)
    alias_type: Mapped[str] = mapped_column(String(16))  # old_symbol | old_name | variant
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # rename date
    observed_at: Mapped[datetime] = _observed_at()

    __table_args__ = (UniqueConstraint("alias", "alias_type", "security_id",
                                       name="uq_alias"),)


# =============================================================== filings

class Filing(Base):
    """One corporate disclosure from any feed. Dedup is two-layered:
    dedup_key (feed-scoped natural id when the feed provides one) and
    content_hash (always computable). Either matching = duplicate."""

    __tablename__ = "filings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feed: Mapped[str] = mapped_column(String(48), index=True)      # e.g. "announcements"
    dedup_key: Mapped[str | None] = mapped_column(String(256))     # NSE's own id when present
    content_hash: Mapped[str] = mapped_column(String(64))          # sha256 of canonical content
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)  # as the feed gave it
    security_id: Mapped[int | None] = mapped_column(ForeignKey("securities.id"), index=True)
    isin: Mapped[str | None] = mapped_column(String(12))
    subject: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(Text)
    attachment_url: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSONB)                # full source payload
    source: Mapped[str] = mapped_column(String(16), default="rss")  # rss | api_backfill
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime] = _observed_at()

    __table_args__ = (
        # content_hash is globally unique; dedup_key unique within a feed
        UniqueConstraint("content_hash", name="uq_filing_content_hash"),
        Index("uq_filing_dedup_key", "feed", "dedup_key", unique=True,
              postgresql_where=text("dedup_key IS NOT NULL")),
        Index("ix_filings_feed_event_at", "feed", "event_at"),
    )


class Document(Base):
    """A downloaded attachment (PDF, XBRL, …), content-addressed."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    local_path: Mapped[str | None] = mapped_column(Text)   # under settings().pdf_dir
    bytes: Mapped[int | None]
    content_type: Mapped[str | None] = mapped_column(String(128))
    fetch_status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending | fetched | failed | skipped
    fetch_error: Mapped[str | None] = mapped_column(Text)
    # Soak finding 2026-08-05 (#3): the archives host is EVENTUALLY
    # CONSISTENT — a freshly-broadcast XBRL 404s for the first few minutes
    # after its RSS item appears. So a 404 gets a bounded retry ladder
    # (attempts + next_attempt_at) instead of an instant permanent fail.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = _observed_at()


class FilingClassification(Base):
    """A2's output — append-only, versioned (§5: every score is versioned
    with the scoring-model version so history can be attributed)."""

    __tablename__ = "filing_classifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id"), index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    materiality: Mapped[int] = mapped_column(Integer)          # 0-10
    sentiment: Mapped[float]                                    # -1..+1
    confidence: Mapped[float]                                   # 0..1
    routine: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str | None] = mapped_column(Text)           # ≤40 words
    entities: Mapped[dict | None] = mapped_column(JSONB)        # echoed source text
    engine: Mapped[str] = mapped_column(String(16))             # rules|local|online
    rule_trace: Mapped[str | None] = mapped_column(Text)        # which rule fired
    model_version: Mapped[str] = mapped_column(String(64))      # prompt+model id
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = _observed_at()

    __table_args__ = (
        # one classification per filing per model version — re-runs with a
        # new prompt/model append rather than overwrite (honest history)
        UniqueConstraint("filing_id", "model_version", name="uq_classification_version"),
    )


class FeedState(Base):
    """Poller bookkeeping per feed — schedule, validators, last outcome."""

    __tablename__ = "feed_state"

    feed: Mapped[str] = mapped_column(String(48), primary_key=True)
    priority: Mapped[str] = mapped_column(String(4))               # P0 | P1 | P2
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(32))    # ok|304|skipped|error:…
    entries_seen: Mapped[int] = mapped_column(BigInteger, default=0)
    entries_new: Mapped[int] = mapped_column(BigInteger, default=0)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)


# ============================================================ event bus

class Outbox(Base):
    """The event bus. id is the replay offset. Events are appended in the
    same transaction as the facts they describe — that is the point."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # offset
    topic: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ConsumerOffset(Base):
    """At-least-once: a consumer's high-water mark, advanced after handling."""

    __tablename__ = "consumer_offsets"

    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_acked_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DeadLetter(Base):
    """Events a consumer failed on MAX_ATTEMPTS times. Kept, never dropped."""

    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    consumer: Mapped[str] = mapped_column(String(64), index=True)
    outbox_id: Mapped[int] = mapped_column(BigInteger)
    topic: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    error: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ======================================================== phase 3: prices

class PriceDaily(Base):
    """One row per security per trading day. ~3.4M rows for 5y × 2.7k
    symbols — plain btree-indexed table per the Phase-1 decision (add
    partitioning only when a query is measured slow).

    source: 'bhavcopy' (NSE official EOD, includes delivery) or 'kite'
    (historical backfill). Bhavcopy wins on conflict — it is the
    exchange's own record."""

    __tablename__ = "price_daily"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    security_id: Mapped[int | None] = mapped_column(ForeignKey("securities.id"))
    trade_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    series: Mapped[str | None] = mapped_column(String(8))
    open: Mapped[float | None]
    high: Mapped[float | None]
    low: Mapped[float | None]
    close: Mapped[float | None]
    prev_close: Mapped[float | None]
    vwap: Mapped[float | None]
    volume: Mapped[float | None]
    turnover: Mapped[float | None]
    trades: Mapped[float | None]
    delivery_qty: Mapped[float | None]
    delivery_pct: Mapped[float | None]
    source: Mapped[str] = mapped_column(String(12), default="bhavcopy")
    observed_at: Mapped[datetime] = _observed_at()

    __table_args__ = (
        UniqueConstraint("symbol", "trade_date", name="uq_price_symbol_date"),
        Index("ix_price_security_date", "security_id", "trade_date"),
    )


# ==================================================== phase 3: financials

class FinancialsQuarterly(Base):
    """One reported quarter per company per basis. `raw` keeps EVERY
    extracted XBRL fact — the named columns are the analysis workhorses,
    but §10 traceability means never throwing source facts away.
    filing_id ties each row to the disclosure it came from."""

    __tablename__ = "financials_quarterly"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    security_id: Mapped[int | None] = mapped_column(ForeignKey("securities.id"), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    filing_id: Mapped[int | None] = mapped_column(ForeignKey("filings.id"))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    basis: Mapped[str] = mapped_column(String(16))   # consolidated | standalone
    audited: Mapped[bool | None]
    revenue: Mapped[float | None]
    other_income: Mapped[float | None]
    total_income: Mapped[float | None]
    expenses: Mapped[float | None]
    finance_costs: Mapped[float | None]
    depreciation: Mapped[float | None]
    pbt: Mapped[float | None]
    tax: Mapped[float | None]
    pat: Mapped[float | None]
    eps_basic: Mapped[float | None]
    raw: Mapped[dict | None] = mapped_column(JSONB)
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = _observed_at()

    __table_args__ = (
        UniqueConstraint("symbol", "period_end", "basis",
                         name="uq_fin_symbol_period_basis"),
    )


# ======================================================= phase 3: scores

class Score(Base):
    """§5: every score is append-only and versioned. One row per agent
    per symbol per computation; `components` carries the full evidence
    breakdown so A7 theses can cite exact inputs."""

    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    agent: Mapped[str] = mapped_column(String(16), index=True)   # a3|a4|a5
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    security_id: Mapped[int | None] = mapped_column(ForeignKey("securities.id"))
    score: Mapped[float]                                          # 0-100
    label: Mapped[str | None] = mapped_column(String(32))         # e.g. trend tag
    confidence: Mapped[float | None]
    components: Mapped[dict | None] = mapped_column(JSONB)
    model_version: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime] = _observed_at()

    __table_args__ = (
        Index("ix_scores_agent_symbol_asof", "agent", "symbol", "as_of"),
    )


# ========================================================= phase 3: flow

class MarketFlow(Base):
    """FII/DII daily buy/sell (market-level context, ₹ crore)."""

    __tablename__ = "market_flow"

    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    category: Mapped[str] = mapped_column(String(8), primary_key=True)  # FII|DII
    buy_value: Mapped[float | None]
    sell_value: Mapped[float | None]
    net_value: Mapped[float | None]
    observed_at: Mapped[datetime] = _observed_at()


class LargeDeal(Base):
    """Bulk + block deals (daily CSVs)."""

    __tablename__ = "large_deals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    client: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(String(4))       # BUY | SELL
    qty: Mapped[float | None]
    price: Mapped[float | None]
    kind: Mapped[str] = mapped_column(String(8))       # bulk | block
    observed_at: Mapped[datetime] = _observed_at()

    __table_args__ = (
        UniqueConstraint("day", "symbol", "client", "side", "qty", "kind",
                         name="uq_large_deal"),
    )


class Surveillance(Base):
    """ASM/GSM membership snapshots — append-only by as_of date so A6 can
    ask 'was it under ASM on date X' honestly."""

    __tablename__ = "surveillance"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    framework: Mapped[str] = mapped_column(String(8))   # asm_lt | asm_st | gsm
    stage: Mapped[str | None] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = _observed_at()

    __table_args__ = (
        UniqueConstraint("as_of", "symbol", "framework", name="uq_surv"),
    )


# ======================================================= phase 4: signals

class Signal(Base):
    """A7's output — the only stance-bearing record in the system.
    Append-only; hysteresis works by comparing against the previous row.
    `thesis` embeds the full evidence trail (score ids, filing ids,
    metric values) — §10: every number traceable to a stored record."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    security_id: Mapped[int | None] = mapped_column(ForeignKey("securities.id"))
    profile: Mapped[str] = mapped_column(String(24))     # default|momentum|…
    stance: Mapped[str] = mapped_column(String(12))      # buy|accumulate|hold|reduce|exit|suppressed
    conviction: Mapped[float]                             # 0-100
    confidence: Mapped[float]
    horizon: Mapped[str | None] = mapped_column(String(16))
    entry_low: Mapped[float | None]
    entry_high: Mapped[float | None]
    target_low: Mapped[float | None]
    target_high: Mapped[float | None]
    invalidation: Mapped[float | None]
    size_pct: Mapped[float | None]                        # suggested % of capital
    thesis: Mapped[dict | None] = mapped_column(JSONB)
    risk_verdict: Mapped[str | None] = mapped_column(String(12))
    model_version: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime] = _observed_at()

    __table_args__ = (
        Index("ix_signals_symbol_asof", "symbol", "as_of"),
    )


# ================================================ phase 5: performance

class SignalPerformance(Base):
    """Forward returns per issued signal, measured as prices ARRIVE (the
    observed corpus — not reconstructed). One row per signal per window,
    written only once the window has fully elapsed."""

    __tablename__ = "signal_performance"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    stance: Mapped[str] = mapped_column(String(12))
    profile: Mapped[str] = mapped_column(String(24))
    conviction: Mapped[float]
    window: Mapped[str] = mapped_column(String(8))       # 1w | 4w | 12w
    entry_price: Mapped[float]                            # close on signal day
    exit_price: Mapped[float]
    ret: Mapped[float]                                    # simple return
    index_ret: Mapped[float | None]                       # Nifty 500 same window
    excess: Mapped[float | None]
    observed_at: Mapped[datetime] = _observed_at()

    __table_args__ = (
        UniqueConstraint("signal_id", "window", name="uq_perf_signal_window"),
    )


class Alert(Base):
    """A8's delivery log — every alert sent (or suppressed), append-only."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    severity: Mapped[str] = mapped_column(String(8))     # high | medium | low
    category: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text)
    evidence_ref: Mapped[dict | None] = mapped_column(JSONB)
    channels: Mapped[dict | None] = mapped_column(JSONB)  # {channel: status}
    observed_at: Mapped[datetime] = _observed_at()


# ============================================================ ops tables

class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    agent: Mapped[str] = mapped_column(String(48), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")
    # running | ok | error | skipped
    stats: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class HttpAudit(Base):
    """Every NSE request — §2.5's full audit log."""

    __tablename__ = "http_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[int | None]
    elapsed_ms: Mapped[int | None]
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    budget_tokens: Mapped[float | None]
    breaker_open: Mapped[bool | None]
    observed_at: Mapped[datetime] = _observed_at()


class Holiday(Base):
    """NSE trading holidays, refreshed from the holiday-master API so the
    static seed in core/clock.py is a bootstrap, not the truth."""

    __tablename__ = "holidays"

    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    segment: Mapped[str] = mapped_column(String(8), primary_key=True, default="CM")
    observed_at: Mapped[datetime] = _observed_at()
