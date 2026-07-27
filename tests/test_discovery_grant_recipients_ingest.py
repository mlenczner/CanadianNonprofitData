"""Regression tests for ingest/grant_recipients.py against a small real
DuckDB fixture (entities + grants_unified tables), mirroring the minimal
schema analysis/build_entity_graph.py actually produces. Covers: entity_kind
filtering (charity excluded by default -- see module docstring for why),
province filtering, and the per-entity grant-count/dollar aggregation."""
import duckdb

from discovery.ingest.grant_recipients import load_federal_grant_recipient_candidates


def _make_db(tmp_path):
    db_path = str(tmp_path / "test.duckdb")
    con = duckdb.connect(db_path)
    con.execute("""
        CREATE TABLE entities (
            entity_id INTEGER, bn_root VARCHAR, canonical_name VARCHAR,
            city VARCHAR, province VARCHAR, entity_kind VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE grants_unified (
            source_dataset VARCHAR, funder_entity_id INTEGER, recipient_entity_id INTEGER,
            amount_cad DOUBLE, fiscal_year INTEGER, program_name VARCHAR,
            description VARCHAR, source_ref VARCHAR
        )
    """)
    con.execute("""
        INSERT INTO entities VALUES
            (1, NULL, 'Ferme Agri-Valleyfield SENC', NULL, 'QC', 'other_org'),
            (2, '123456789', 'Refuge des Jeunes de Montreal', NULL, 'QC', 'other_org'),
            (3, '987654321', 'Ontario Non-Charity Org', NULL, 'ON', 'other_org'),
            (4, '111111111', 'Some Registered Charity', 'Montreal', 'QC', 'charity'),
            (5, NULL, 'Never Received Any Grant', NULL, 'QC', 'other_org')
    """)
    con.execute("""
        INSERT INTO grants_unified VALUES
            ('federal_gc', 100, 1, 50000.0, 2023, 'Prog A', NULL, NULL),
            ('federal_gc', 100, 2, 30000.0, 2022, 'Prog B', NULL, NULL),
            ('federal_gc', 100, 2, 20000.0, 2023, 'Prog C', NULL, NULL),
            ('federal_gc', 100, 3, 15000.0, 2023, 'Prog D', NULL, NULL),
            ('federal_gc', 100, 4, 99999.0, 2023, 'Prog E', NULL, NULL),
            ('canada_council', 200, 5, 5000.0, 2023, 'Prog F', NULL, NULL)
    """)
    con.close()
    return db_path


def test_only_federal_gc_recipients_are_included(tmp_path):
    db_path = _make_db(tmp_path)
    candidates = load_federal_grant_recipient_candidates(db_path=db_path)
    names = {c.legal_name for c in candidates}
    assert "Never Received Any Grant" not in names  # only a canada_council grant, not federal_gc


def test_charity_entity_kind_excluded_by_default(tmp_path):
    db_path = _make_db(tmp_path)
    candidates = load_federal_grant_recipient_candidates(db_path=db_path)
    names = {c.legal_name for c in candidates}
    assert "Some Registered Charity" not in names


def test_province_filter_restricts_to_quebec(tmp_path):
    db_path = _make_db(tmp_path)
    candidates = load_federal_grant_recipient_candidates(db_path=db_path, province="QC")
    names = {c.legal_name for c in candidates}
    assert "Ontario Non-Charity Org" not in names
    assert "Ferme Agri-Valleyfield SENC" in names


def test_grants_are_counted_and_summed_per_entity(tmp_path):
    db_path = _make_db(tmp_path)
    candidates = load_federal_grant_recipient_candidates(db_path=db_path, province="QC")
    by_name = {c.legal_name: c for c in candidates}
    refuge = by_name["Refuge des Jeunes de Montreal"]
    assert refuge.n_grants == 2
    assert refuge.total_amount_cad == 50000.0


def test_entity_kinds_param_can_include_charity(tmp_path):
    db_path = _make_db(tmp_path)
    candidates = load_federal_grant_recipient_candidates(
        db_path=db_path, entity_kinds=("other_org", "charity")
    )
    names = {c.legal_name for c in candidates}
    assert "Some Registered Charity" in names
