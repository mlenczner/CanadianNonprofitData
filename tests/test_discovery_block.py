"""Regression tests for discovery/block.py's postal -> FSA -> city cascade,
including the spec's explicit rule that a record with no usable blocking key
at all must be routed to needs_review rather than silently dropped or
defaulted to a match/no-match verdict.
"""
from collections import namedtuple

from discovery.block import build_name_prefix_index, candidates_for, name_prefix, NO_BLOCKING_KEY
from discovery.ingest.base import DiscoveryRecord

FakeCraRecord = namedtuple("FakeCraRecord", ["legal_name", "city", "postal_code"])


def _disc(postal_code=None, city=None, legal_name="Org"):
    return DiscoveryRecord(
        source_id="1", jurisdiction="QC", discovery_source="req", legal_name=legal_name,
        trade_names=(), address="1 Rue Test", postal_code=postal_code, city=city,
        legal_form="Personne morale sans but lucratif", source_snapshot_date="2026-07-14",
    )


CRA_RECORDS = [
    FakeCraRecord("Org A", "Montreal", "H2X1Y4"),
    FakeCraRecord("Org B", "Montreal", "H3A1B2"),
    FakeCraRecord("Org C", "Westmount", None),
]


def test_exact_postal_match_wins_first():
    candidates, level = candidates_for(_disc(postal_code="H2X1Y4"), CRA_RECORDS)
    assert level == "postal"
    assert [c.legal_name for c in candidates] == ["Org A"]


def test_falls_back_to_fsa_when_no_postal_hit():
    candidates, level = candidates_for(_disc(postal_code="H2X9Z9"), CRA_RECORDS)
    assert level == "fsa"
    assert [c.legal_name for c in candidates] == ["Org A"]


def test_falls_back_to_city_when_no_postal_or_fsa():
    candidates, level = candidates_for(_disc(postal_code=None, city="Westmount"), CRA_RECORDS)
    assert level == "city"
    assert [c.legal_name for c in candidates] == ["Org C"]


def test_no_usable_blocking_key_routes_to_review_not_no_match():
    candidates, level = candidates_for(_disc(postal_code=None, city=None), CRA_RECORDS)
    assert level == NO_BLOCKING_KEY
    assert candidates == []


# ── name-prefix tier: for candidate pools with no postal/city at all ────────
# (e.g. discovery/ingest/grant_recipients.py's other_org entities, which
# analysis.build_entity_graph.py's residual branch only ever gives a
# province, never a city or postal code).

GRANT_CANDIDATES = [
    FakeCraRecord("Refuge des Jeunes de Montreal", None, None),
    FakeCraRecord("Centre d'Action Benevole", None, None),
]


def test_name_prefix_tier_not_used_when_index_not_supplied():
    # Default candidates_for() call (no name_prefix_idx passed) must behave
    # exactly as before this tier was added -- a pool with no postal/city
    # goes straight to NO_BLOCKING_KEY, not silently falling into name-prefix
    # matching every existing caller never asked for.
    candidates, level = candidates_for(_disc(legal_name="Refuge des Jeunes"), GRANT_CANDIDATES)
    assert level == NO_BLOCKING_KEY
    assert candidates == []


def test_name_prefix_tier_used_as_last_resort_when_supplied():
    idx = build_name_prefix_index(GRANT_CANDIDATES)
    candidates, level = candidates_for(
        _disc(legal_name="Refuge des Jeunes"), GRANT_CANDIDATES, name_prefix_idx=idx
    )
    assert level == "name_prefix"
    assert [c.legal_name for c in candidates] == ["Refuge des Jeunes de Montreal"]


def test_postal_still_wins_over_name_prefix_when_both_available():
    idx = build_name_prefix_index(CRA_RECORDS)
    candidates, level = candidates_for(
        _disc(postal_code="H2X1Y4", legal_name="Org"), CRA_RECORDS, name_prefix_idx=idx
    )
    assert level == "postal"


def test_name_prefix_miss_still_falls_through_to_no_blocking_key():
    idx = build_name_prefix_index(GRANT_CANDIDATES)
    candidates, level = candidates_for(
        _disc(legal_name="Completely Unrelated Org"), GRANT_CANDIDATES, name_prefix_idx=idx
    )
    assert level == NO_BLOCKING_KEY
    assert candidates == []


def test_name_prefix_is_first_four_normalized_chars():
    # Same prefix length/definition as analysis.build_entity_graph.name_prefix()
    # -- not a coincidence, deliberately mirrored (see block.py's module docstring).
    assert name_prefix("Refuge des Jeunes de Montreal") == "REFU"
    assert len(name_prefix("Refuge des Jeunes de Montreal")) == 4


def test_name_prefix_handles_none_and_empty():
    assert name_prefix(None) is None
    assert name_prefix("") is None
