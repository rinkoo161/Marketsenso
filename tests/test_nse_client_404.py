"""404 fail-fast — one budget token, no in-process retries (soak finding,
2026-08-05 midday: 3x retry on 404 tripled request burn for zero gain)."""
from __future__ import annotations

import pytest

from marketsense.net.budget import RequestBudget
from marketsense.net.nse_client import NSEClient, NSEUnavailable


class Resp:
    status_code = 404
    content = b""
    headers: dict = {}


def test_404_costs_one_token_and_raises_exhausted(monkeypatch):
    budget = RequestBudget(per_minute=30)
    client = NSEClient(budget=budget)

    calls = []

    def fake_get(url, headers=None, timeout=None, impersonate=None):
        calls.append(url)
        return Resp()

    import marketsense.net.nse_client as mod

    monkeypatch.setattr(mod.creq, "get", fake_get)
    monkeypatch.setattr(mod.NSEClient, "_sleep_backoff", staticmethod(lambda a: None))

    with pytest.raises(NSEUnavailable) as exc:
        client.get("https://nsearchives.nseindia.com/corporate/xbrl/missing.xml")
    assert exc.value.kind == "exhausted"
    assert len(calls) == 1  # exactly one attempt, not MAX_RETRIES
    snap = budget.snapshot()
    assert snap["granted"] == 1
    assert snap["breaker_open"] is False  # a 404 is an answer, not an outage
