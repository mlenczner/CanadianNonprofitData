"""
Regression tests for the NULL-province residual-merge gap in
analysis/build_entity_graph.py (BN-hygiene spec item 3).

Resolver.resolve()'s residual branch dedupes unmatched records on
(normalize_name(name), province) -- confirmed real gap, documented in
AGENTS.md open issue #6: "Fédération acadienne de la Nouvelle-Écosse" (after
the HTML-cleanup fix, both variants fold to a byte-identical normalize_name()
key) still ends up as 2 entities post-rebuild, because one side's raw record
carried province=NS (and a BN) while the other's was entirely blank -- the
residual dedup key can't bridge a NULL province against a filled one. Same
pattern hits "Fédération culturelle acadienne de la Nouvelle-Écosse".

build_null_province_residual_merges() is a post-process over the already-
built entities table (not fixable inside Resolver.resolve() itself, since
the two records can arrive in either order): it merges a fully-blank
(bn_root IS NULL AND province IS NULL) other_org entity into its
normalize_name()-identical other_org twin, ONLY when that twin carries a
bn_root or a province and there's exactly one such twin -- never merges two
BN-bearing entities (build_bn_near_miss_review()'s territory instead), and
skips a generic-looking short name to avoid merging two unrelated small orgs
that just happen to share a name.

Run with:
    .venv/bin/python -m pytest tests/test_null_province_residual_merge.py
"""

import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.build_entity_graph import build_null_province_residual_merges


def _make_db(con, entities, grants=None, links=None):
    con.execute("""
        CREATE TABLE entities (
            entity_id INTEGER, bn_root VARCHAR, canonical_name VARCHAR,
            city VARCHAR, province VARCHAR, entity_kind VARCHAR
        )
    """)
    con.executemany("INSERT INTO entities VALUES (?,?,?,?,?,?)", entities)
    con.execute("""
        CREATE TABLE grants_unified (
            grant_id INTEGER, source_dataset VARCHAR, funder_entity_id INTEGER,
            recipient_entity_id INTEGER, amount_cad DOUBLE, fiscal_year INTEGER,
            program_name VARCHAR, description VARCHAR, source_ref VARCHAR
        )
    """)
    if grants:
        con.executemany("INSERT INTO grants_unified VALUES (?,?,?,?,?,?,?,?,?)", grants)
    con.execute("""
        CREATE TABLE entity_links (
            entity_id INTEGER, source_dataset VARCHAR, raw_name VARCHAR,
            raw_bn VARCHAR, match_method VARCHAR, match_score DOUBLE
        )
    """)
    if links:
        con.executemany("INSERT INTO entity_links VALUES (?,?,?,?,?,?)", links)


def test_merges_the_federation_acadienne_pair():
    con = duckdb.connect(":memory:")
    _make_db(con, [
        (1, "132162041", "Fédération acadienne de la Nouvelle-Écosse", None, "NS", "other_org"),
        (2, None, "Fédération acadienne de la Nouvelle-Écosse", None, None, "other_org"),
    ])
    n = build_null_province_residual_merges(con)
    assert n == 1
    remaining = con.execute("SELECT entity_id FROM entities ORDER BY 1").fetchall()
    assert remaining == [(1,)]


def test_merges_case_variant_with_no_bn_but_a_province():
    con = duckdb.connect(":memory:")
    _make_db(con, [
        (1, None, "Fédération culturelle acadienne de la Nouvelle-Écosse", None, "NS", "other_org"),
        (2, None, "FÉDÉRATION CULTURELLE ACADIENNE DE LA NOUVELLE-ÉCOSSE", None, None, "other_org"),
    ])
    n = build_null_province_residual_merges(con)
    assert n == 1
    remaining = con.execute("SELECT entity_id FROM entities ORDER BY 1").fetchall()
    assert remaining == [(1,)]


def test_does_not_merge_two_bn_bearing_entities_with_the_same_name():
    con = duckdb.connect(":memory:")
    _make_db(con, [
        (1, "132162041", "Some Real Nonprofit Organization", None, "NS", "other_org"),
        (2, "999999999", "Some Real Nonprofit Organization", None, "NB", "other_org"),
    ])
    n = build_null_province_residual_merges(con)
    assert n == 0
    remaining = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert remaining == 2


def test_skips_a_generic_short_name():
    con = duckdb.connect(":memory:")
    _make_db(con, [
        (1, "132162041", "ABC Club", None, "NS", "other_org"),
        (2, None, "ABC Club", None, None, "other_org"),
    ])
    n = build_null_province_residual_merges(con)
    assert n == 0
    remaining = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert remaining == 2


def test_skips_when_ambiguous_between_two_distinct_twins():
    con = duckdb.connect(":memory:")
    _make_db(con, [
        (1, "111111111", "Some Real Nonprofit Organization", None, "NS", "other_org"),
        (2, "222222222", "Some Real Nonprofit Organization", None, "NB", "other_org"),
        (3, None, "Some Real Nonprofit Organization", None, None, "other_org"),
    ])
    n = build_null_province_residual_merges(con)
    assert n == 0
    remaining = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert remaining == 3


def test_does_not_merge_into_a_charity_entity_of_the_same_name():
    # A charity's identity is BN-anchored during resolve() itself; a residual
    # other_org sharing its exact name (e.g. a non-NFP recipient type that
    # was never even fuzzy-matched against charities) should not silently
    # absorb into the charity here.
    con = duckdb.connect(":memory:")
    _make_db(con, [
        (1, "132162041", "Canadian Diabetes Association", "Toronto", "ON", "charity"),
        (2, None, "Canadian Diabetes Association", None, None, "other_org"),
    ])
    n = build_null_province_residual_merges(con)
    assert n == 0
    remaining = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert remaining == 2


def test_remaps_grants_and_links_on_merge():
    con = duckdb.connect(":memory:")
    _make_db(
        con,
        [
            (1, "132162041", "Fédération acadienne de la Nouvelle-Écosse", None, "NS", "other_org"),
            (2, None, "Fédération acadienne de la Nouvelle-Écosse", None, None, "other_org"),
        ],
        grants=[(1, "federal_gc", 99, 2, 5000.0, 2023, "Some Program", None, "dept|ref-1")],
        links=[(2, "federal_gc", "Federation acadienne", None, "unmatched_new", None)],
    )
    build_null_province_residual_merges(con)
    grant_recipient = con.execute("SELECT recipient_entity_id FROM grants_unified WHERE grant_id = 1").fetchone()[0]
    assert grant_recipient == 1
    link_entity = con.execute("SELECT entity_id FROM entity_links").fetchone()[0]
    assert link_entity == 1
