"""Classifier pipeline: cache, floors-over-model, LLM-unavailable
degradation, filing.classified emission. LLM stubbed — model quality is
the eval harness's job, not a unit test's."""
from __future__ import annotations

from sqlalchemy import func, select

import marketsense.agents.a2_docintel.classifier as clf
from marketsense.db.models import Filing, FilingClassification, Outbox


def _filing(db, subject, description="", feed="announcements", n=[0]):
    n[0] += 1
    f = Filing(feed=feed, content_hash=f"a2t{n[0]}".ljust(64, "0"),
               symbol="TESTCO", subject=subject, description=description,
               source="rss")
    db.add(f)
    db.flush()
    return f


def test_rules_final_no_llm_call(db_factory, monkeypatch):
    called = []
    monkeypatch.setattr(clf, "classify_json",
                        lambda *a: called.append(1) or None)
    with db_factory() as db:
        f = _filing(db, "Declaration of NAV", "Declaration of NAV")
        row = clf.classify_filing(db, f)
        db.commit()
    assert row.routine and row.engine == "rules"
    assert called == []  # the whole point of metadata-first


def test_classification_cached_per_model_version(db_factory, monkeypatch):
    monkeypatch.setattr(clf, "classify_json", lambda *a: None)
    with db_factory() as db:
        f = _filing(db, "Declaration of NAV", "Declaration of NAV")
        assert clf.classify_filing(db, f) is not None
        assert clf.classify_filing(db, f) is None  # second run: cache hit
        db.commit()
        assert db.scalar(select(func.count()).select_from(FilingClassification)) == 1


def test_model_cannot_undercut_floor(db_factory, monkeypatch):
    # model says auditor resignation is materiality 1 — floor wins
    monkeypatch.setattr(clf, "classify_json", lambda *a: (
        {"category": "auditor_resignation", "materiality": 1, "sentiment": -0.5,
         "confidence": 0.99, "summary": "auditor left", "entities": {}}, "local"))
    with db_factory() as db:
        # subject crafted to dodge the rules so the model path runs
        f = _filing(db, "Update", "unusual text with no rule match")
        row = clf.classify_filing(db, f)
        db.commit()
    assert row.category == "auditor_resignation"
    assert row.materiality >= 9


def test_llm_unavailable_degrades_confidence_not_neutral(db_factory, monkeypatch):
    monkeypatch.setattr(clf, "classify_json", lambda *a: None)
    with db_factory() as db:
        f = _filing(db, "Miscellaneous", "no rule matches this text either")
        row = clf.classify_filing(db, f)
        db.commit()
    assert row.category == "other"
    assert row.confidence <= 0.3          # §10: degrade confidence…
    assert "llm_unavailable" in row.rule_trace  # …and say why, in the trail


def test_emits_filing_classified_event(db_factory, monkeypatch):
    monkeypatch.setattr(clf, "classify_json", lambda *a: None)
    with db_factory() as db:
        f = _filing(db, "Declaration of NAV", "Declaration of NAV")
        row = clf.classify_filing(db, f)
        db.commit()
        evt = db.scalar(select(Outbox).where(Outbox.topic == "filing.classified"))
        assert evt is not None
        assert evt.payload["filing_id"] == f.id
        assert evt.payload["classification_id"] == row.id
        assert evt.payload["category"] == row.category
