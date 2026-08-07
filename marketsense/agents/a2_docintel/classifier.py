"""A2 classification pipeline.

Per filing, in order:
    1. Cache — one classification per (filing, MODEL_VERSION); re-runs skip.
    2. Rules (taxonomy.classify_by_rules). A confident or routine rule hit
       is FINAL — no model call. This is what keeps ~5k NAV declarations a
       day from burning tokens.
    3. LLM triage (Ollama-first per user decision) for the residue: no rule
       hit, or a low-confidence one. Strict JSON schema.
    4. Merge: category from the higher-confidence source; materiality =
       max(model, hard floor) — the deterministic floor ALWAYS survives
       (brief: auditor resignation ≥9 regardless of model output).
    5. Persist + emit filing.classified.

Where the numbers come from (brief §10, zero tolerance for fabricated
values): materiality/sentiment/confidence are the model's own scores —
that is what an LLM classifier IS. `entities` values are echoed source
text (the model quotes the filing, e.g. "₹540 crore"), never arithmetic
the model performed. Phase 3 computes real numbers from XBRL.

MODEL_VERSION stamps every row; changing prompt/model/rules bumps it and
re-runs APPEND rather than overwrite — honest history per §5.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from marketsense.agents.a2_docintel.taxonomy import (
    CATEGORIES,
    apply_floor,
    classify_by_rules,
)
from marketsense.bus import topics
from marketsense.bus.outbox import publish
from marketsense.core.logging import get_logger
from marketsense.db.models import Filing, FilingClassification
from marketsense.llm.client import classify_json

log = get_logger("a2")

# v2 (2026-08-07): feed priors extended to all periodic/structural feeds
# after the live backlog showed 43% of LLM traffic was compliance filings
# with "PERIOD END DATE" metadata. Bumping the version makes the consumer
# reclassify everything under v2; v1 rows remain (append-only history).
MODEL_VERSION = "a2-v2-rules+triage"

# Rule hits at or above this confidence skip the model entirely.
RULE_FINAL_CONFIDENCE = 0.75

_SCHEMA = {
    "category": {"type": "string", "enum": CATEGORIES},
    "materiality": {"type": "number", "min": 0, "max": 10},
    "sentiment": {"type": "number", "min": -1, "max": 1},
    "confidence": {"type": "number", "min": 0, "max": 1},
    "summary": {"type": "string", "max_words": 40},
    "entities": {"type": "object"},
}

_PROMPT = """You are classifying one NSE corporate disclosure for an Indian equity analyst.

Company: {company}
Feed: {feed}
Subject: {subject}
Text: {text}

Allowed categories — "category" MUST be one of these exact strings, copied
character-for-character (no other word is valid):
{categories}

Reply with ONLY a JSON object:
{{"category": "<one category>",
  "materiality": <0-10, how much this could move the stock; routine=0-1, results=4-6, major M&A/fraud/auditor exit=8-10>,
  "sentiment": <-1 to 1, expected price direction>,
  "confidence": <0-1, your certainty>,
  "summary": "<plain-English summary, 40 words max>",
  "entities": {{<numeric facts QUOTED VERBATIM from the text, e.g. "order_value": "Rs 540 crore"; empty object if none>}}}}
Quote entity values exactly as written in the text. Do not compute or convert numbers."""


def classify_filing(db, filing: Filing, allow_llm: bool = True,
                    prefer_online: bool = False) -> FilingClassification | None:
    """Classify one filing. Returns the row, or None if already done at
    this MODEL_VERSION. Commits are the caller's job.

    allow_llm=False is the BACKLOG mode (2026-08-07): on this 8GB machine
    Ollama runs at ~0.16 tok/s under memory pressure (40-80s per call),
    so model-classifying a 12.7k backlog would take days. Historical
    events therefore classify rules-only with honestly lowered confidence
    (rule_trace notes 'backlog_no_llm'); the model serves live traffic,
    where tens of calls a day are affordable even at that speed."""
    exists = db.scalar(
        select(FilingClassification.id).where(
            FilingClassification.filing_id == filing.id,
            FilingClassification.model_version == MODEL_VERSION,
        )
    )
    if exists:
        return None

    subject = filing.subject or ""
    description = filing.description or ""
    hit = classify_by_rules(filing.feed, subject, description)

    row: FilingClassification
    if hit and (hit.routine or hit.confidence >= RULE_FINAL_CONFIDENCE):
        # rules are final — no model call
        row = FilingClassification(
            filing_id=filing.id, category=hit.category,
            materiality=apply_floor(hit.category, hit.materiality),
            sentiment=hit.sentiment, confidence=hit.confidence,
            routine=hit.routine, summary=None, entities=None,
            engine="rules", rule_trace=hit.rule,
            model_version=MODEL_VERSION, event_at=filing.event_at,
        )
    elif not allow_llm:
        # backlog mode: rules or an honest low-confidence 'other'
        if hit:
            row = FilingClassification(
                filing_id=filing.id, category=hit.category,
                materiality=apply_floor(hit.category, hit.materiality),
                sentiment=hit.sentiment, confidence=hit.confidence * 0.8,
                routine=hit.routine, engine="rules",
                rule_trace=hit.rule + "|backlog_no_llm",
                model_version=MODEL_VERSION, event_at=filing.event_at,
            )
        else:
            row = FilingClassification(
                filing_id=filing.id, category="other", materiality=2,
                sentiment=0.0, confidence=0.2, routine=False, engine="rules",
                rule_trace="no_rule|backlog_no_llm",
                model_version=MODEL_VERSION, event_at=filing.event_at,
            )
    else:
        row = _classify_with_model(filing, subject, description, hit, db=db,
                                   prefer_online=prefer_online)

    db.add(row)
    db.flush()
    publish(
        db,
        topics.FILING_CLASSIFIED,
        {
            "filing_id": filing.id,
            "classification_id": row.id,
            "symbol": filing.symbol,
            "security_id": filing.security_id,
            "category": row.category,
            "materiality": row.materiality,
            "sentiment": row.sentiment,
            "confidence": row.confidence,
            "routine": row.routine,
            "event_at": filing.event_at.isoformat() if filing.event_at else None,
        },
    )
    return row


def _classify_with_model(filing: Filing, subject: str, description: str, hit,
                         db=None, prefer_online: bool = False):
    company = (filing.raw or {}).get("company_title") or filing.symbol or "unknown"
    text = description[:1500]
    # Metadata too thin to judge → pull the PDF's opening pages. This is
    # the ONLY path that touches documents; ~90% of filings never get here.
    if db is not None and len(subject) + len(text) < 120:
        from marketsense.agents.a2_docintel.extract import text_for_filing

        pdf_text = text_for_filing(db, filing)
        if pdf_text:
            text = (text + "\n" + pdf_text)[:1500]
    prompt = _PROMPT.format(
        company=company, feed=filing.feed, subject=subject[:300],
        text=text or "(no further text)",
        categories="\n".join(f'  "{c}"' for c in CATEGORIES),
    )
    result = classify_json(prompt, _SCHEMA, prefer_online=prefer_online)

    if result is None:
        # No usable model → the rule hit (if any) or an honest low-confidence
        # 'other'. Stale/missing input degrades CONFIDENCE, never silently
        # defaults to neutral-with-high-confidence (§10).
        if hit:
            return FilingClassification(
                filing_id=filing.id, category=hit.category,
                materiality=apply_floor(hit.category, hit.materiality),
                sentiment=hit.sentiment, confidence=hit.confidence * 0.8,
                routine=hit.routine, engine="rules",
                rule_trace=hit.rule + "|llm_unavailable",
                model_version=MODEL_VERSION, event_at=filing.event_at,
            )
        return FilingClassification(
            filing_id=filing.id, category="other", materiality=2,
            sentiment=0.0, confidence=0.2, routine=False, engine="rules",
            rule_trace="no_rule|llm_unavailable",
            model_version=MODEL_VERSION, event_at=filing.event_at,
        )

    obj, engine = result
    category = obj["category"]
    materiality = int(round(obj["materiality"]))
    # merge with the rule layer: floors always survive; a higher-confidence
    # rule keeps its category
    if hit and hit.confidence > obj["confidence"]:
        category = hit.category
        materiality = max(materiality, hit.materiality)
    materiality = apply_floor(category, materiality)

    entities = obj.get("entities") or None
    if entities is not None and not isinstance(entities, dict):
        entities = None

    return FilingClassification(
        filing_id=filing.id, category=category, materiality=materiality,
        sentiment=float(obj["sentiment"]), confidence=float(obj["confidence"]),
        routine=False, summary=(obj.get("summary") or "")[:500] or None,
        entities=entities, engine=engine,
        rule_trace=hit.rule if hit else None,
        model_version=MODEL_VERSION, event_at=filing.event_at,
    )
