"""Rule-layer tests — subjects taken from the live corpus (2026-08-05/07),
not invented, per the evidence standards."""
from marketsense.agents.a2_docintel.taxonomy import (
    CATEGORIES,
    MATERIALITY_FLOORS,
    apply_floor,
    classify_by_rules,
)


def test_routine_noise_is_final_and_cheap():
    for subj in ["Declaration of NAV", "Trading Window closure",
                 "Copies of Newspaper Publication",
                 "Analysts/Institutional Investor Meet/Con. Call Updates",
                 "Certificate under Regulation 74 (5)"]:
        hit = classify_by_rules("announcements", subj, subj)
        assert hit is not None and hit.routine, subj
        assert hit.materiality <= 1


def test_auditor_resignation_floor_is_nine():
    hit = classify_by_rules("announcements",
                            "Resignation of Statutory Auditor", "")
    assert hit.category == "auditor_resignation"
    assert apply_floor(hit.category, hit.materiality) >= 9
    # the floor survives a model trying to lowball it
    assert apply_floor("auditor_resignation", 2) == 9


def test_non_statutory_auditor_change_is_not_m9():
    """Live bug (pulse view, 2026-08-08): internal/secretarial auditor
    changes drew the statutory m9 floor."""
    for text in ["Resignation and appointment of new Internal Auditor",
                 "Resignation of Secretarial Auditor of the company",
                 "Cost Auditor resignation for FY 2026-27"]:
        hit = classify_by_rules("announcements", "Change in Auditor", text)
        assert hit.category == "management_change", text
        assert hit.materiality <= 4
    # statutory mentioned alongside internal → still the red flag
    hit = classify_by_rules(
        "announcements", "Change in Auditor",
        "Resignation of Statutory Auditor; internal auditor unchanged")
    assert hit.category == "auditor_resignation"


def test_board_meeting_intimation_is_not_results():
    """Live bug: intimation body text ('to consider and approve the
    financial results') matched the results rule and scored 5."""
    hit = classify_by_rules(
        "announcements", "Board Meeting Intimation",
        "Intimation of Board Meeting to consider and approve the "
        "Unaudited Financial Results for the quarter ended June 30, 2026")
    assert hit.routine
    assert hit.materiality <= 1


def test_outcome_with_results_IS_results():
    hit = classify_by_rules(
        "announcements", "Outcome of Board Meeting",
        "approved the Unaudited Financial Results for the quarter")
    assert hit.category == "results"


def test_rating_direction_read_from_text():
    down = classify_by_rules("announcements", "Credit Rating",
                             "CRISIL has downgraded the rating")
    up = classify_by_rules("announcements", "Credit Rating",
                           "ICRA upgrade of long term rating")
    assert down.category == up.category == "credit_rating_change"
    assert down.sentiment < 0 < up.sentiment


def test_feed_priors_cover_structured_feeds():
    assert classify_by_rules("insider_trading", "", "").category == "insider_trade"
    enc = classify_by_rules(
        "encumbrance", None,
        "NAME OF THE PROMOTER(S) / PACS WHOSE SHARES HAVE BEEN ENCUMBERED : X")
    assert enc.category == "pledge_creation_release"


def test_order_win_positive_sentiment():
    hit = classify_by_rules("announcements", "Award of Contract",
                            "received a work order worth Rs 540 crore")
    assert hit.category == "order_win"
    assert hit.sentiment > 0


def test_no_rule_returns_none_for_llm():
    assert classify_by_rules("announcements", "Miscellaneous update",
                             "completely unremarkable text") is None


def test_floors_reference_real_categories():
    for cat in MATERIALITY_FLOORS:
        assert cat in CATEGORIES
