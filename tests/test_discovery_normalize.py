"""Regression tests for discovery/normalize.py -- specifically that it reuses
analysis.build_entity_graph's normalize_name()/BN helpers rather than
diverging from them, and that the one deliberate extension (ENR suffix)
behaves as intended.
"""
from analysis.build_entity_graph import normalize_name
from discovery.normalize import normalize_org_name, normalize_for_scoring, normalize_postal, fsa


def test_normalize_org_name_matches_base_normalizer_when_no_enr_suffix():
    # Same tokenization as normalize_name() for names that don't touch the one
    # deliberate divergence (ENR) -- if this drifts, the two pipelines start
    # producing different match keys for the same org name.
    assert normalize_org_name("Fondation Jeunesse Inc.") == normalize_name("Fondation Jeunesse Inc.")


def test_normalize_org_name_strips_enr_suffix():
    assert normalize_org_name("Jean Tremblay Enr.") == "JEAN TREMBLAY"


def test_normalize_org_name_splits_bilingual_pipe_like_base_normalizer():
    assert normalize_org_name("English Name|Nom francais") == normalize_org_name("English Name")


def test_normalize_postal_strips_space_and_uppercases():
    assert normalize_postal("h2x 1y4") == "H2X1Y4"


def test_normalize_postal_rejects_malformed_input():
    assert normalize_postal("not a postal code") is None
    assert normalize_postal(None) is None


def test_fsa_takes_first_three_chars():
    assert fsa("H2X1Y4") == "H2X"
    assert fsa(None) is None


# ── normalize_for_scoring (match.py-only, not normalize_org_name) ────────────

def test_normalize_for_scoring_does_not_affect_normalize_org_name():
    # normalize_for_scoring is a separate, more aggressive layer -- the base
    # normalizer (used for anything other than fuzzy score computation) must
    # be completely unaffected by it.
    assert normalize_org_name("Centre de la Petite Enfance Origami") == "CENTRE PETITE ENFANCE ORIGAMI"


def test_normalize_for_scoring_strips_generic_category_words():
    # The real false-positive this was built to fix: two different daycare
    # centres sharing only the generic "Centre de la Petite Enfance" prefix.
    a = normalize_for_scoring("Centre de la Petite Enfance Origami")
    b = normalize_for_scoring("Centre de la Petite Enfance de McGill")
    assert a == "ORIGAMI"
    assert b == "MCGILL"


def test_normalize_for_scoring_drops_elision_remnant_single_letters():
    # normalize_org_name's punctuation-to-space step turns "L'Ecole" into two
    # tokens ("L", "ECOLE") -- the leftover "L" is noise, not a real shared
    # word, and normalize_for_scoring must drop it (unlike normalize_org_name,
    # which still leaves it in per the test above).
    assert "L" in normalize_org_name("L'Ecole du Batiment").split()  # base normalizer keeps it (documented current behavior)
    assert "L" not in normalize_for_scoring("L'Ecole du Batiment").split()
    assert normalize_for_scoring("L'Ecole du Batiment") == "BATIMENT"


def test_normalize_for_scoring_strips_connector_words_not_in_base_suffixes():
    # DES/POUR/EN/AU/AUX aren't in the base LEGAL_SUFFIXES set (only
    # DE/DU/LA/LE/LES/ET/OF/THE are) but carry the same zero distinguishing
    # value for matching purposes.
    result = normalize_for_scoring("Amis des Enfants pour la Vie")
    for connector in ("DES", "POUR"):
        assert connector not in result.split()
