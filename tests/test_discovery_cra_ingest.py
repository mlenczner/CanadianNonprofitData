"""Regression tests for ingest/cra.py's pure _pick_latest_per_bn() helper --
deliberately does not touch nonprofit_network.duckdb (may not exist yet, or
may be locked by a concurrent build_entity_graph.py rebuild), same reasoning
as why load_cra_records() itself only ever reads read-only."""
from discovery.ingest.cra import CraCharityRecord, _in_quebec, _in_montreal, _pick_latest_per_bn


def test_latest_year_wins_per_bn_root():
    rows = [
        ("123456789RR0001", "Old Name", "Montreal", "QC", "H2X1Y4", "Charitable", "PC", 2020),
        ("123456789RR0001", "New Name", "Montreal", "QC", "H2X1Y4", "Charitable", "PC", 2024),
    ]
    out = _pick_latest_per_bn(rows)
    assert len(out) == 1
    assert out[0].legal_name == "New Name"
    assert out[0].source_year == 2024


def test_status_is_active_only_for_latest_year_present_in_the_batch():
    rows = [
        ("123456789RR0001", "Still Registered", "Montreal", "QC", "H2X1Y4", "Charitable", "PC", 2024),
        ("987654321RR0001", "Deregistered Before 2024", "Montreal", "QC", "H3A1B2", "Charitable", "PC", 2019),
    ]
    out = {r.bn_root: r for r in _pick_latest_per_bn(rows)}
    assert out["123456789"].status == "active"
    assert out["987654321"].status == "revoked_or_inactive"


def test_postal_code_is_normalized():
    rows = [("123456789RR0001", "Org", "Montreal", "QC", "h2x 1y4", "Charitable", "PC", 2024)]
    out = _pick_latest_per_bn(rows)
    assert out[0].postal_code == "H2X1Y4"


def test_rows_with_no_valid_bn_are_skipped():
    rows = [("not-a-bn", "Org", "Montreal", "QC", "H2X1Y4", "Charitable", "PC", 2024)]
    assert _pick_latest_per_bn(rows) == []


def _rec(city, province, postal):
    return CraCharityRecord(
        bn_root="123456789", legal_name="Org", city=city, province=province,
        postal_code=postal, category="Charitable", designation="PC", status="active", source_year=2024,
    )


# ── region filters (province-wide quebec vs. the original montreal-only pilot) ──

def test_in_quebec_checks_province_field_not_city():
    # Quebec City is nowhere near Montreal island, but still province QC.
    assert _in_quebec(_rec("Quebec City", "QC", "G1A1A1"))
    assert not _in_quebec(_rec("Montreal", "ON", "H2X1Y4"))  # province mismatch wins even with a Montreal city string


def test_in_montreal_still_excludes_the_rest_of_quebec():
    # The original narrower filter must still reject a real Quebec charity
    # that's simply outside the Montreal-island allowlist/FSA fallback.
    assert not _in_montreal(_rec("Quebec City", "QC", "G1A1A1"))
    assert _in_montreal(_rec("Montreal", "QC", "H2X1Y4"))
