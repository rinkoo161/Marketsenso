"""Versioned weight profiles (§3 A7). Changing any number here MUST bump
PROFILES_VERSION — signals carry it, and historical attribution depends
on knowing which weights produced which stance."""

PROFILES_VERSION = "p2"  # p2 (2026-08-08): + short profile (user request)

# weights over (fundamental, technical, flow, event) — must sum to 100
PROFILES: dict[str, dict[str, float]] = {
    "default":      {"fundamental": 30, "technical": 25, "flow": 20, "event": 25},
    # Short-horizon (1-5 trading days) on EOD data: event + momentum
    # dominate, fundamentals are near-irrelevant at this timescale.
    # TRUE intraday needs live ticks — that arrives via the ltp-monitor
    # integration (their live layer reacting to our event flags), not
    # from an EOD pipeline pretending.
    "short":        {"fundamental": 0,  "technical": 35, "flow": 20, "event": 45},
    "value":        {"fundamental": 50, "technical": 10, "flow": 15, "event": 25},
    "momentum":     {"fundamental": 10, "technical": 45, "flow": 25, "event": 20},
    "event_driven": {"fundamental": 15, "technical": 15, "flow": 20, "event": 50},
    "quality":      {"fundamental": 45, "technical": 15, "flow": 25, "event": 15},
    "swing":        {"fundamental": 5,  "technical": 50, "flow": 30, "event": 15},
    "positional":   {"fundamental": 35, "technical": 30, "flow": 15, "event": 20},
}

HORIZON = {
    "default": "3-6m", "short": "1-5d", "value": "6-18m", "momentum": "2-8w",
    "event_driven": "1-6w", "quality": "6-18m", "swing": "1-3w",
    "positional": "2-6m",
}

for name, w in PROFILES.items():
    assert abs(sum(w.values()) - 100.0) < 1e-9, f"profile {name} must sum to 100"
