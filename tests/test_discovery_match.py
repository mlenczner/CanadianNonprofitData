"""Regression tests for discovery/match.py: trade-name matching (the
"operates as X, registered as Y" case the spec calls out) and 1:many
runner-up retention."""
from collections import namedtuple

from discovery.ingest.base import DiscoveryRecord
from discovery.match import best_match, score_names

FakeCandidate = namedtuple("FakeCandidate", ["legal_name"])


def _disc(legal_name, trade_names=()):
    return DiscoveryRecord(
        source_id="1", jurisdiction="QC", discovery_source="req", legal_name=legal_name,
        trade_names=trade_names, address="1 Rue Test", postal_code="H2X1Y4", city="Montreal",
        legal_form="Personne morale sans but lucratif", source_snapshot_date="2026-07-14",
    )


def test_score_names_catches_trade_name_not_just_legal_name():
    # Registered under a bland legal name, operates under a name close to the
    # CRA registry entry -- only the trade name should score high.
    score = score_names("Refuge des Jeunes de Montreal", "9876543 Quebec Inc.",
                         ("Refuge des Jeunes de Montreal",))
    assert score == 100


def test_score_names_returns_max_across_legal_and_trade_names():
    score_legal_only = score_names("Refuge des Jeunes de Montreal", "Refuge des Jeunes de Montreal", ())
    score_with_bad_trade = score_names(
        "Refuge des Jeunes de Montreal", "Refuge des Jeunes de Montreal", ("Something Unrelated",)
    )
    assert score_legal_only == score_with_bad_trade == 100


def test_best_match_retains_runner_up_for_close_calls():
    candidates = [FakeCandidate("Refuge des Jeunes de Montreal"), FakeCandidate("Refuge des Jeunes de Laval")]
    result = best_match(_disc("Refuge des Jeunes de Montreal"), candidates, name_of=lambda c: c.legal_name)
    assert result.best.legal_name == "Refuge des Jeunes de Montreal"
    assert result.best_score == 100
    assert result.runner_up is not None
    assert result.runner_up_score is not None
    assert result.runner_up_score < result.best_score


def test_best_match_returns_none_for_empty_candidates():
    assert best_match(_disc("Any Org"), []) is None


# ── regression: real false positives found in the spec-step-7 validation ────
# score_names used to be token_set_ratio, which scored these confirmed-
# unrelated organizations high enough to land in or above the needs_review
# band (75-90) purely from sharing one generic word. Real numbers (before ->
# after) are in docs/montreal-discovery-spec.md's Deviations note.

def test_score_names_does_not_match_on_a_shared_city_name_alone():
    # "THE ROYAL MONTREAL CURLING CLUB" vs "FONDATION DE LA MODE DE MONTREAL"
    # share only "MONTREAL" -- scored 76.2 under the old token_set_ratio.
    score = score_names("FONDATION DE LA MODE DE MONTREAL", "THE ROYAL MONTREAL CURLING CLUB", ())
    assert score < 75


def test_score_names_does_not_match_on_shared_generic_category_words():
    # Two different daycare centres sharing only "Centre de la Petite
    # Enfance" -- scored 84.0 even under token_sort_ratio before
    # normalize_for_scoring's category-word stripping was added.
    score = score_names(
        "CENTRE DE LA PETITE ENFANCE DE MCGILL / MCGILL CHILDCARE CENTRE",
        "CENTRE DE LA PETITE ENFANCE ORIGAMI", (),
    )
    assert score < 75


def test_score_names_matches_own_language_half_of_a_bilingual_candidate():
    # A French-only discovery name against its own bilingual CRA record
    # (English clause + French clause joined by "/") -- must still score
    # high even though the candidate has extra, non-matching English text.
    score = score_names(
        "HOUSE OF PRAYER FOR ALL NATIONS/MAISON DE PRIERE POUR TOUTESLES NATIONS",
        "MAISON DE PRIERE POUR TOUTES LES NATIONS", (),
    )
    assert score >= 85
