"""Screener.in cross-check adapter — §2.3's optional adapter, DISABLED BY
DEFAULT in the only sense that matters: nothing automatic calls it. It is
invoked solely by the `ms reconcile` CLI command for the Phase 3
acceptance gate, at a hard 2s/request politeness delay, stdlib parsing
(house style: no bs4 for one table).

Parses the consolidated (or standalone) quarterly table:
    Sales / Revenue row  → revenue (₹ crore, integers, rounded by Screener)
    Net Profit row       → pat
Verified live 2026-08-08: TITAN Jun-2026 = 21,356 / 1,777 — exact match
with our XBRL-derived values.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import httpx

from marketsense.core.logging import get_logger

log = get_logger("screener")

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_DELAY_S = 2.0
_last_call = [0.0]

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_MONTH_DAYS = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
               7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def _num(s: str) -> float | None:
    s = s.replace(",", "").replace(" ", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def fetch_quarterlies(symbol: str, *, consolidated: bool = True
                      ) -> dict[datetime, dict] | None:
    """{period_end: {revenue_cr, pat_cr}} or None (404 / no table)."""
    # politeness: hard spacing between requests, process-wide
    wait = _DELAY_S - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()

    suffix = "consolidated/" if consolidated else ""
    try:
        r = httpx.get(f"https://www.screener.in/company/{symbol}/{suffix}",
                      headers=_UA, timeout=30, follow_redirects=True)
    except httpx.HTTPError as e:
        log.warning("screener_fetch_failed", symbol=symbol, error=str(e)[:100])
        return None
    if r.status_code != 200:
        return None
    h = r.text
    qi = h.find('id="quarters"')
    if qi < 0:
        return None
    seg = h[qi:h.find("</table>", qi)]

    heads = [x.strip() for x in re.findall(r"<th[^>]*>([^<]*)</th>", seg)]
    periods: list[datetime | None] = []
    for head in heads[1:]:
        m = re.match(r"([A-Z][a-z]{2}) (\d{4})", head)
        if not m:
            periods.append(None)
            continue
        mon, year = _MONTHS[m.group(1)], int(m.group(2))
        day = 29 if (mon == 2 and year % 4 == 0) else _MONTH_DAYS[mon]
        periods.append(datetime(year, mon, day, tzinfo=timezone.utc))

    rows: dict[str, list[float | None]] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S):
        cells = [re.sub(r"<[^>]+>", " ", c) for c in
                 re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        clean = [re.sub(r"\s+", " ", c).replace(" ", " ").strip()
                 for c in cells]
        if not clean:
            continue
        name = clean[0].replace("&nbsp;", "").replace("+", "").strip()
        if re.match(r"^(Sales|Revenue)$", name):
            rows["revenue"] = [_num(c) for c in clean[1:]]
        elif name == "Net Profit":
            rows["pat"] = [_num(c) for c in clean[1:]]

    if "revenue" not in rows and "pat" not in rows:
        return None
    out: dict[datetime, dict] = {}
    for i, period in enumerate(periods):
        if period is None:
            continue
        entry = {}
        for key in ("revenue", "pat"):
            vals = rows.get(key)
            if vals and i < len(vals) and vals[i] is not None:
                entry[f"{key}_cr"] = vals[i]
        if entry:
            out[period] = entry
    return out or None
