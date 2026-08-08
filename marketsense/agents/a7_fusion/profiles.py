"""Versioned weight profiles (§3 A7). Changing any number here MUST bump
PROFILES_VERSION — signals carry it, and historical attribution depends
on knowing which weights produced which stance."""

# p2 (2026-08-08): + short profile (user request)
# p3 (2026-08-09): per-horizon level geometry — short and positional were
# sharing identical ATR targets/stops, which the user correctly flagged
# as invalid (a 5-day target cannot equal a 6-month target)
PROFILES_VERSION = "p3"

# Level geometry per profile: stop = stop_atr × ATR below close;
# target zone = [t_low, t_high] × risk (risk = close − stop).
# Short: tight 1.5×ATR leash, modest 1.2–2R take-profit — day-scale
# noise kills wide stops and 3-month targets alike.
# Positional: 3×ATR room to hold through weeks, 2–3.5R targets.
LEVELS = {
    "short":   {"stop_atr": 1.5, "t_low": 1.2, "t_high": 2.0},
    "default": {"stop_atr": 3.0, "t_low": 2.0, "t_high": 3.5},
}
# profiles without an explicit entry inherit by horizon character
_LEVELS_FALLBACK = {
    "swing": "short", "event_driven": "short", "momentum": "default",
    "value": "default", "quality": "default", "positional": "default",
}


def levels_for(profile: str) -> dict:
    return LEVELS.get(profile) or LEVELS[_LEVELS_FALLBACK.get(profile, "default")]

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
