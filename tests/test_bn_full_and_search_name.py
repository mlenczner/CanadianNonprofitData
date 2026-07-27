"""Regression tests for two new entities columns added in
analysis/build_entity_graph.py's build_entities_and_grants(): bn_full and
search_name. Both were previously only ever asserted as already-populated
fixture values in tests/test_org_page.py -- nothing exercised the actual
build-time SQL that computes them (_bn_full_update_sql()/
_search_name_update_sql()), including the "latest source_year wins"
tie-break bn_full depends on.

bn_full: the complete 15-char BN (RR/RC/etc. suffix), not just the 9-digit
bn_root -- sourced from raw_t3010_ident.BN, latest source_year row per
bn_root. Powers org_page.py's CRA-listing deep link (C2).

search_name: canonical_name folded through lower(strip_accents(...)) at
build time -- powers webapp.py's accent-insensitive live search (A4). Must
stay in exact lockstep with webapp.py's own query-time folding
(_fold_query()) or "ecole"/"école" stop returning the same results; that
consistency is checked at runtime here, not just by reading both docstrings.

Run with:
    .venv/bin/python -m pytest tests/test_bn_full_and_search_name.py
"""

import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analysis"))

from analysis.build_entity_graph import _bn_full_update_sql, _search_name_update_sql


def _make_entities_and_ident(con, entities, ident_rows):
    con.execute("""
        CREATE TABLE entities (
            entity_id INTEGER, bn_root VARCHAR, canonical_name VARCHAR,
            city VARCHAR, province VARCHAR, entity_kind VARCHAR
        )
    """)
    con.executemany("INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?)", entities)
    con.execute("""
        CREATE TABLE raw_t3010_ident (BN VARCHAR, source_year INTEGER)
    """)
    if ident_rows:
        con.executemany("INSERT INTO raw_t3010_ident VALUES (?, ?)", ident_rows)
    con.execute("ALTER TABLE entities ADD COLUMN bn_full VARCHAR")


# ── bn_full ──────────────────────────────────────────────────────────────

def test_bn_full_populated_from_matching_bn_root():
    con = duckdb.connect(":memory:")
    _make_entities_and_ident(
        con,
        [(1, "123456789", "Test Charity", "Toronto", "ON", "charity")],
        [("123456789RR0001", 2022)],
    )
    con.execute(_bn_full_update_sql())
    row = con.execute("SELECT bn_full FROM entities WHERE entity_id = 1").fetchone()
    assert row[0] == "123456789RR0001"


def test_bn_full_picks_latest_source_year_when_a_bn_root_has_multiple_filings():
    con = duckdb.connect(":memory:")
    _make_entities_and_ident(
        con,
        [(1, "123456789", "Test Charity", "Toronto", "ON", "charity")],
        [
            ("123456789RR0001", 2019),
            # Same bn_root filed under a different program-account suffix in
            # a later year -- the most recent filing's suffix should win,
            # not whichever row happened to load first.
            ("123456789RR0002", 2023),
            ("123456789RR0001", 2021),
        ],
    )
    con.execute(_bn_full_update_sql())
    row = con.execute("SELECT bn_full FROM entities WHERE entity_id = 1").fetchone()
    assert row[0] == "123456789RR0002", "the 2023 filing (latest source_year) should win, not 2019 or 2021"


def test_bn_full_null_when_entity_has_no_identification_row():
    con = duckdb.connect(":memory:")
    _make_entities_and_ident(
        con,
        [(1, "999999999", "No Filing Org", None, "BC", "other_org")],
        [("123456789RR0001", 2022)],  # unrelated bn_root
    )
    con.execute(_bn_full_update_sql())
    row = con.execute("SELECT bn_full FROM entities WHERE entity_id = 1").fetchone()
    assert row[0] is None


def test_bn_full_null_bn_root_entity_is_unaffected():
    # Residual/unmatched entities carry bn_root=NULL -- the UPDATE...FROM
    # join must not spuriously match them against an identification row.
    con = duckdb.connect(":memory:")
    _make_entities_and_ident(
        con,
        [(1, None, "Residual Org", "Calgary", "AB", "other_org")],
        [("123456789RR0001", 2022)],
    )
    con.execute(_bn_full_update_sql())
    row = con.execute("SELECT bn_full FROM entities WHERE entity_id = 1").fetchone()
    assert row[0] is None


def test_bn_full_strips_non_alphanumeric_characters_before_matching_root():
    # Some source rows carry a formatted BN (dash-separated); the 9-digit
    # root extraction must strip every non-alphanumeric char, not just the
    # first one (regexp_replace needs its 'g' flag -- confirmed as a real,
    # separate fix in locate_t3010_qd_receipt's identical extraction, see
    # AGENTS.md/the org_page.py diff).
    con = duckdb.connect(":memory:")
    _make_entities_and_ident(
        con,
        [(1, "123456789", "Test Charity", "Toronto", "ON", "charity")],
        [("123-456-789RR0001", 2022)],
    )
    con.execute(_bn_full_update_sql())
    row = con.execute("SELECT bn_full FROM entities WHERE entity_id = 1").fetchone()
    assert row[0] == "123-456-789RR0001"


# ── search_name ──────────────────────────────────────────────────────────

def test_search_name_lowercases_and_strips_accents():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE entities (entity_id INTEGER, canonical_name VARCHAR)
    """)
    con.execute("INSERT INTO entities VALUES (1, 'École Polytechnique')")
    con.execute("ALTER TABLE entities ADD COLUMN search_name VARCHAR")
    con.execute(_search_name_update_sql())
    row = con.execute("SELECT search_name FROM entities WHERE entity_id = 1").fetchone()
    assert row[0] == "ecole polytechnique"


def test_search_name_collapses_internal_whitespace():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE entities (entity_id INTEGER, canonical_name VARCHAR)")
    con.execute("INSERT INTO entities VALUES (1, 'Canadian   Red  Cross')")
    con.execute("ALTER TABLE entities ADD COLUMN search_name VARCHAR")
    con.execute(_search_name_update_sql())
    row = con.execute("SELECT search_name FROM entities WHERE entity_id = 1").fetchone()
    assert row[0] == "canadian red cross"


def test_search_name_matches_webapps_fold_query_expression_at_runtime():
    """webapp.py's _fold_query() folds an incoming search query independently
    of this column's build-time SQL -- both were hand-written to use the
    exact same expression, but nothing previously verified that at runtime.
    If either one drifts (e.g. someone tweaks the whitespace regex in only
    one place), accent-insensitive search silently breaks for queries that
    don't happen to need folding. Runs both expressions against the same
    sample strings in the same connection and asserts they agree, rather
    than trusting the docstrings stay in sync."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analysis"))
    from analysis import webapp as w

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE entities (entity_id INTEGER, canonical_name VARCHAR)")
    samples = [
        "École Polytechnique",
        "  Leading And Trailing Whitespace  ",
        "Internal   Double   Spaces",
        "MIXED Case Née Org",
        "Plain Ascii Name",
    ]
    con.executemany("INSERT INTO entities VALUES (?, ?)", list(enumerate(samples)))
    con.execute("ALTER TABLE entities ADD COLUMN search_name VARCHAR")
    con.execute(_search_name_update_sql())

    for i, raw in enumerate(samples):
        built_value = con.execute("SELECT search_name FROM entities WHERE entity_id = ?", [i]).fetchone()[0]
        folded_query = w._fold_query(con, raw)
        assert built_value == folded_query, (
            f"entities.search_name and webapp._fold_query() disagree for {raw!r}: "
            f"{built_value!r} != {folded_query!r}"
        )
