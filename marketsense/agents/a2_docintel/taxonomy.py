"""A2 taxonomy + the deterministic rule layer.

The brief's §A2 taxonomy, verbatim, as the single category vocabulary.
The rule layer runs BEFORE any LLM and has two jobs:

1. Classify the bulk cheaply. Live corpus evidence (2026-08-05/07 soak,
   12.7k filings): the majority of announcements are routine — NAV
   declarations, trading-window closures, newspaper-ad publications,
   Reg 74(5) certificates, investor-meet intimations. These are classified
   here with materiality ≤1 and NEVER reach a model.

2. Enforce hard floors the model cannot override (brief: "a deterministic
   rule layer for high-signal patterns — auditor resignation → materiality
   ≥9 regardless of model output"). Floors are applied AFTER the LLM too:
   the merge in classifier.py takes max(rule_floor, model_materiality).

Rules match on feed + subject + description via compiled regexes. A rule
hit yields (category, materiality, sentiment, confidence, routine). When
several match, highest-materiality wins — a results filing that also
mentions a dividend is 'results' only if nothing scarier matched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- categories
# Brief §A2, verbatim. Do not extend casually — A7's event overlay and the
# eval set key on these names.
CATEGORIES = [
    "order_win", "capex", "capacity_expansion", "ma", "demerger",
    "fundraise", "debt_raise", "credit_rating_change", "results", "guidance",
    "dividend_bonus_split_buyback", "management_change", "auditor_resignation",
    "regulatory_action", "litigation", "insider_trade",
    "pledge_creation_release", "plant_shutdown", "fire_accident",
    "clarification_to_rumour", "other",
]

# Hard materiality FLOORS (survive any model output; classifier.py merges
# with max()). Values per the brief where stated, judgement elsewhere —
# each is a floor, not a score: the model may push HIGHER, never lower.
MATERIALITY_FLOORS = {
    "auditor_resignation": 9,
    "regulatory_action": 7,
    "plant_shutdown": 7,
    "fire_accident": 6,
    "credit_rating_change": 6,
    "ma": 6,
    "demerger": 6,
    "clarification_to_rumour": 5,
    "order_win": 4,
    "fundraise": 4,
    "pledge_creation_release": 4,
}


@dataclass(frozen=True)
class RuleHit:
    category: str
    materiality: int          # rule's own estimate (also acts as the floor)
    sentiment: float          # -1..+1 prior; 0 when direction needs reading
    confidence: float         # how sure the RULE is, 0..1
    routine: bool = False     # known-noise; never escalate to an LLM
    rule: str = ""            # which rule fired (evidence trail)


def _r(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


# ---------------------------------------------------------------- routine noise
# Checked FIRST. These are the classes that diluted the resolution metric in
# Phase 1 and would otherwise burn LLM calls ~5k times/day.
_ROUTINE = [
    (_r(r"declaration of nav|net asset value"), "nav_declaration"),
    (_r(r"trading window|window closure"), "trading_window"),
    (_r(r"newspaper (publication|advertisement)|copies of newspaper"), "newspaper_ad"),
    (_r(r"regulation 74 ?\(5\)|74\(5\) of sebi"), "reg74_certificate"),
    (_r(r"(analyst|institutional investor)s? meet|con\.? ?call|earnings call|investor presentation"), "investor_meet"),
    # NB: "record date / book closure" is deliberately NOT routine — those
    # notices usually announce the dividend/bonus they set the date for
    # (set-2 eval: two dividend announcements were swallowed as routine).
    # The dividend rule below catches them; bare date notices go to the LLM.
    (_r(r"loss of share certificate|duplicate share certificate|issue of duplicate"), "share_certificate"),
    # An INTIMATION of a board meeting (usually "to consider and approve the
    # financial results") is not the results — without this line the body
    # text matches the results rule and scores 5. The outcome filing is the
    # signal; the intimation is calendar noise.
    (_r(r"(prior )?intimation of board meeting|board meeting intimation|"
        r"intimation.{0,30}(of )?board meeting|board meeting.{0,20}scheduled"), "board_meeting_intimation"),
    (_r(r"change in company secretary and compliance officer|compliance certificate"), "compliance_routine"),
    (_r(r"monitoring agency report"), "monitoring_agency"),
    (_r(r"spdi|large corporate|initial disclosure|annual disclosure"), "disclosure_routine"),
]

# ---------------------------------------------------------------- signal rules
# (pattern, category, materiality, sentiment, confidence)
_RULES: list[tuple[re.Pattern, str, int, float, float]] = [
    # -- the non-negotiable floors first
    # STATUTORY auditor only. Live finding (2026-08-08 pulse view): internal/
    # secretarial/cost auditor changes also contain "resign…auditor" and were
    # all scoring the m9 floor — Ola Electric's new INTERNAL auditor is not a
    # governance red flag. The negative lookbehind-ish guard is done in
    # classify_by_rules (_auditor_qualifier) because regex alone can't see
    # "internal" appearing far from the match.
    (_r(r"resignation.{0,40}(statutory )?auditor|auditor.{0,30}resign"),
     "auditor_resignation", 9, -0.9, 0.95),
    # "order" alone is poison next to "SEBI (LODR)" citations — a company
    # reporting "General Updates under SEBI (LODR) 2015 on Order Received"
    # is winning BUSINESS, not being raided (eval miss 2026-08-08). Only
    # unambiguous enforcement markers fire this rule.
    (_r(r"(sebi|nclt|nclat|enforcement directorate|\bed\b|income tax|gst).{0,60}"
        r"(order (passed|dated)|final order|interim order|penalt|search|raid|"
        r"show.?cause|investigation|prohibit|debar)"),
     "regulatory_action", 7, -0.7, 0.8),
    (_r(r"corporate insolvency|\bcirp\b|insolvency resolution|liquidation order"),
     "litigation", 7, -0.7, 0.85),
    (_r(r"(plant|factory|unit|operations?).{0,30}(shut ?down|suspension|halt|closure)"),
     "plant_shutdown", 7, -0.7, 0.8),
    (_r(r"\bfire\b|accident|explosion|mishap"), "fire_accident", 6, -0.6, 0.7),

    # -- ratings: direction matters, read it here when possible
    (_r(r"(credit )?rating.{0,30}(downgrade|revised downward|negative)"),
     "credit_rating_change", 7, -0.8, 0.9),
    (_r(r"(credit )?rating.{0,30}(upgrade|revised upward|positive)"),
     "credit_rating_change", 6, 0.8, 0.9),
    (_r(r"(credit )?rating|icra|crisil|care ratings|india ratings"),
     "credit_rating_change", 6, 0.0, 0.6),

    # -- growth events
    (_r(r"(order|contract|loi|letter of (intent|award)|work order).{0,60}"
        r"(received|award|bagg|secured|won|worth)"),
     "order_win", 5, 0.7, 0.8),
    (_r(r"capacity (expansion|addition)|new (plant|facility|unit)|greenfield|brownfield"),
     "capacity_expansion", 5, 0.6, 0.7),
    (_r(r"\bcapex\b|capital expenditure"), "capex", 5, 0.5, 0.7),
    (_r(r"(scheme of )?(amalgamation|merger|acquisition|acquire|takeover|slump sale)"),
     "ma", 6, 0.3, 0.8),
    (_r(r"demerger|de-merger|spin.?off|scheme of arrangement"), "demerger", 6, 0.2, 0.8),

    # -- capital events
    (_r(r"\bqip\b|qualified institutional|preferential (issue|allotment)|rights issue|warrants?\b.{0,30}(issue|allot|convert)|fund.?rais"),
     "fundraise", 5, 0.1, 0.8),
    # CP-NI/CP-RI are the offer_documents feed's commercial-paper filings —
    # debt, not equity fundraise (eval miss 2026-08-08)
    (_r(r"(ncd|non.?convertible debenture|commercial paper|\bCP-(NI|RI)\b|"
        r"bond issue|debt.{0,20}(raise|issue))"),
     "debt_raise", 3, 0.0, 0.7),
    (_r(r"buy.?back"), "dividend_bonus_split_buyback", 6, 0.6, 0.85),
    (_r(r"dividend|bonus (issue|share)|stock split|sub.?division of (equity )?shares"),
     "dividend_bonus_split_buyback", 4, 0.5, 0.8),

    # -- results & guidance
    (_r(r"(un)?audited financial results|financial results|statement of (standalone|consolidated)"),
     "results", 5, 0.0, 0.85),
    (_r(r"guidance|outlook (revised|raised|lowered)"), "guidance", 6, 0.0, 0.7),

    # -- governance & people
    (_r(r"(resignation|appointment|cessation|steps? down).{0,50}"
        r"(managing director|\bmd\b|\bceo\b|\bcfo\b|chairman|whole.?time director|key managerial)"),
     "management_change", 5, -0.2, 0.75),
    (_r(r"litigation|arbitral|arbitration|legal proceedings|court (order|case)|writ petition"),
     "litigation", 5, -0.5, 0.7),
    (_r(r"clarification.{0,40}(rumou?r|news (item|report)|price movement)"),
     "clarification_to_rumour", 5, 0.0, 0.8),

    # -- ownership signals
    (_r(r"insider trading|sast|substantial acquisition|disclosure under regulation (29|31)|trading plan"),
     "insider_trade", 3, 0.0, 0.7),
    (_r(r"(pledge|encumbrance).{0,30}(creat|invok|releas|revok)|reason for encumbrance"),
     "pledge_creation_release", 4, -0.4, 0.8),
]

# Feed-level priors — when the feed itself IS the classification.
# (category, materiality, sentiment, confidence, routine)
# The routine=True rows were found live (2026-08-07): periodic compliance
# XBRL feeds whose metadata is just "PERIOD END DATE: …" were burning an
# LLM call each to conclude 'other' — 43% of all model traffic. A periodic
# disclosure is not an event; its DATA feeds A3/A5 in Phase 3, but its
# classification is routine by construction.
_FEED_CATEGORY = {
    "insider_trading": ("insider_trade", 3, 0.0, 0.9, False),
    "encumbrance": ("pledge_creation_release", 5, -0.4, 0.9, False),
    "financial_results": ("results", 5, 0.0, 0.9, False),
    "integrated_financials": ("results", 4, 0.0, 0.85, False),
    "sast_reg29": ("insider_trade", 3, 0.0, 0.85, False),
    # Reg 31 is the ENCUMBRANCE disclosure regulation — eval miss class of
    # 2026-08-08 (6 rows): it was mapped to insider_trade.
    "sast_reg31": ("pledge_creation_release", 4, -0.3, 0.85, False),
    # Daily_Buyback.xml carries DAILY PROGRESS reports of ongoing buybacks,
    # not buyback announcements — m2 routine (set-2 eval: 7 rows at m6 were
    # the single largest Spearman drag). The announcement itself arrives via
    # the announcements feed and scores through the buyback text rule.
    "buyback": ("dividend_bonus_split_buyback", 2, 0.2, 0.9, True),
    "offer_documents": ("fundraise", 4, 0.1, 0.7, False),
    "corporate_actions": ("dividend_bonus_split_buyback", 3, 0.3, 0.7, False),
    # periodic/structural disclosures — routine, no LLM, ever
    "related_party": ("other", 2, 0.0, 0.85, True),
    "shareholding_pattern": ("other", 2, 0.0, 0.85, True),
    "secretarial_compliance": ("other", 1, 0.0, 0.9, True),
    "annual_reports": ("other", 2, 0.0, 0.9, True),
    "deviation_variation": ("other", 2, -0.1, 0.8, True),
    "board_meetings": ("other", 1, 0.0, 0.85, True),
    "voting_results": ("other", 1, 0.0, 0.85, True),
    "investor_complaints": ("other", 1, 0.0, 0.9, True),
    "share_transfers": ("other", 1, 0.0, 0.9, True),
    "brsr": ("other", 1, 0.0, 0.9, True),
    "unitholding_patterns": ("other", 1, 0.0, 0.9, True),
    "corporate_governance": ("other", 1, 0.0, 0.9, True),
    "circulars": ("other", 1, 0.0, 0.85, True),
}


_NON_STATUTORY_AUDITOR = _r(r"(internal|secretarial|cost) auditor")
_STATUTORY_AUDITOR = _r(r"statutory auditor")


# Feeds whose SEMANTICS are the feed itself — the free-text rules must not
# see them at all. Eval evidence (2026-08-08): SAST Reg 29 metadata contains
# "substantial acquisition" (→ false 'ma', 7 rows), board-meeting intimations
# are titled "Financial Results" (→ false 'results', 7 rows), and NSE
# circulars about MF scheme mergers / F&O dividend adjustments / NCD
# suspensions hit ma/dividend/regulatory rules (6 rows). The text rules are
# written for free-text announcements; these feeds bypass them.
FEED_AUTHORITATIVE = {
    "sast_reg29", "sast_reg31", "insider_trading", "encumbrance",
    "board_meetings", "circulars", "shareholding_pattern", "related_party",
    "secretarial_compliance", "investor_complaints", "share_transfers",
    "brsr", "unitholding_patterns", "corporate_governance", "voting_results",
    "annual_reports", "buyback",
}

# A SEBI-regulation CITATION ("under SEBI (Prohibition of Insider Trading)
# Regulations…") is how half of routine compliance prose starts — it is not
# enforcement. Set-2 eval: a trading-code notice and a shareholder letter
# both drew regulatory_action m7 from citation text alone.
_REG_CITATION = _r(r"sebi \(")
_REG_ENFORCEMENT = _r(r"order (passed|dated)|final order|interim order|penalt|"
                      r"show.?cause|search|raid|investigation|debar")


def _auditor_qualifier(hit: RuleHit, text: str) -> RuleHit:
    """Downgrade auditor_resignation when the auditor in question is
    internal/secretarial/cost and NOT statutory — those are ordinary
    personnel changes, not the m9 red flag."""
    if hit.category != "auditor_resignation":
        return hit
    if _NON_STATUTORY_AUDITOR.search(text) and not _STATUTORY_AUDITOR.search(text):
        return RuleHit("management_change", 3, -0.1, hit.confidence,
                       rule=hit.rule + "|non_statutory_auditor")
    return hit


def _regulatory_qualifier(hit: RuleHit, text: str) -> RuleHit | None:
    """Kill regulatory_action hits that come from regulation CITATIONS
    ('SEBI (…) Regulations') with no independent enforcement marker.
    Returns None to drop the hit entirely (other rules may still apply)."""
    if hit.category != "regulatory_action":
        return hit
    if _REG_CITATION.search(text) and not _REG_ENFORCEMENT.search(text):
        return None
    return hit


def classify_by_rules(feed: str, subject: str | None, description: str | None) -> RuleHit | None:
    """Deterministic classification. None = no rule fired → LLM territory."""
    text = " ".join(x for x in (subject, description) if x)
    if not text and feed not in _FEED_CATEGORY:
        return None

    # routine noise first — cheap exit for the bulk
    for pat, name in _ROUTINE:
        if pat.search(text):
            return RuleHit("other", 1, 0.0, 0.9, routine=True, rule=f"routine:{name}")

    # structured feeds: the feed IS the classification; text rules skipped
    if feed in FEED_AUTHORITATIVE and feed in _FEED_CATEGORY:
        cat, mat, sent, conf, routine = _FEED_CATEGORY[feed]
        return RuleHit(cat, mat, sent, conf, routine=routine,
                       rule=f"feed_authoritative:{feed}")

    best: RuleHit | None = None
    for pat, cat, mat, sent, conf in _RULES:
        if pat.search(text):
            hit = _auditor_qualifier(
                RuleHit(cat, mat, sent, conf, rule=f"pattern:{pat.pattern[:40]}"), text)
            hit = _regulatory_qualifier(hit, text)
            if hit is None:
                continue
            if best is None or hit.materiality > best.materiality:
                best = hit
    if best:
        return best

    if feed in _FEED_CATEGORY:
        cat, mat, sent, conf, routine = _FEED_CATEGORY[feed]
        return RuleHit(cat, mat, sent, conf, routine=routine, rule=f"feed:{feed}")
    return None


def apply_floor(category: str, materiality: int) -> int:
    """max(model output, hard floor) — the floor always survives."""
    return max(materiality, MATERIALITY_FLOORS.get(category, 0))
