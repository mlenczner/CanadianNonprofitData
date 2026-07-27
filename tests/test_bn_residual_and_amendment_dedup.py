"""
Regression tests for two correctness bugs found in analysis/build_entity_graph.py:

1. Amendment rows restate an agreement's value rather than adding to it, but
   grants_unified was built from every row in grants.csv with no dedup --
   inflating federal_gc totals substantially. Fixed by _latest_amendment_sql(),
   a SQL QUALIFY/ROW_NUMBER dedup to the latest amendment per (owner_org,
   ref_number), run before anything reads grants values (see load_raw() in
   build_entity_graph.py). ref_number alone is NOT a safe key -- 24,851 refs
   collide across departments (confirmed: e.g. GC-2016-Q4-00001 is six
   different grants from six different departments, all at amendment 0);
   dedupe by ref_number alone would silently discard genuinely distinct
   agreements rather than just superseded amendments.

2. Resolver.resolve()'s residual branch stored bn_root on a newly-created
   entity but never registered it in bn_to_entity, so a BN that only ever
   appeared in unmatched/residual records could never exact-match itself on a
   later record -- splitting one real organization into many entities purely
   because of name-spelling variance across sources (confirmed: 18,139 BN
   roots mapped to multiple entities, e.g. Prince Rupert Port Authority
   existed as 6 entities sharing one BN). Fixed by registering the BN when a
   residual entity is created or backfilled, while explicitly refusing to
   merge two different BNs that happen to share a normalized name+province.

   Related: grants.csv recipient names are often bilingual pipe-formatted
   ("English Name|Nom français"); normalize_name() now splits on "|" and
   keeps only the English half so both language variants collapse to one
   entity via the same name+province residual dedup.

Run with:
    .venv/bin/python tests/test_bn_residual_and_amendment_dedup.py
    .venv/bin/python -m pytest tests/
"""

import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.build_entity_graph import (
    Resolver, _latest_amendment_sql, build_entity_financials_by_year, display_name, normalize_name,
)


def resolve_link(resolver, name, bn, province):
    resolver.resolve("federal_gc", name, bn, province, allow_fuzzy=False)
    return resolver.links[-1]


# ── Bug 2a: residual BN registration ──────────────────────────────────────────

def test_residual_bn_gets_registered_for_later_exact_match():
    r = Resolver()
    link1 = resolve_link(r, "Prince Rupert Port Authority", "123456789RR0001", "BC")
    assert link1.match_method == "unmatched_new"
    assert "123456789" in r.bn_to_entity, "residual entity's BN was never registered"
    first_eid = link1.entity_id

    # Same BN, deliberately different name spelling/case -- must exact-BN-match
    # the same entity now, not create a second one via name-based residual dedup.
    link2 = resolve_link(r, "PRINCE RUPERT PORT AUTHORITY (PRPA)", "123456789RR0001", "BC")
    assert link2.match_method == "exact_bn"
    assert link2.entity_id == first_eid


def test_residual_bn_backfills_onto_existing_name_matched_entity():
    r = Resolver()
    # First occurrence has no BN.
    link1 = resolve_link(r, "Prince Rupert Port Authority", None, "BC")
    first_eid = link1.entity_id
    assert r.entities[first_eid - 1].bn_root is None

    # Second occurrence, same normalized name+province, now carries a BN --
    # should attach to the SAME entity (not create a new one) and register it.
    link2 = resolve_link(r, "Prince Rupert Port Authority", "123456789RR0001", "BC")
    assert link2.entity_id == first_eid
    assert r.entities[first_eid - 1].bn_root == "123456789"
    assert r.bn_to_entity["123456789"] == first_eid

    # A third occurrence with the same BN should now exact-match.
    link3 = resolve_link(r, "Prince Rupert Port Auth", "123456789RR0001", "BC")
    assert link3.match_method == "exact_bn"
    assert link3.entity_id == first_eid


def test_residual_bn_collision_does_not_merge_different_orgs():
    r = Resolver()
    # Two genuinely different organizations that happen to share a normalized
    # name + province, each with its own real BN.
    link1 = resolve_link(r, "Community Food Bank", "111111111RR0001", "ON")
    link2 = resolve_link(r, "Community Food Bank", "222222222RR0001", "ON")
    assert link1.entity_id != link2.entity_id, "different BNs must not be merged into one entity"
    assert r.bn_to_entity["111111111"] == link1.entity_id
    assert r.bn_to_entity["222222222"] == link2.entity_id

    # A later record with the first BN must resolve back to entity 1 (exact_bn),
    # not the second, and vice versa.
    link3 = resolve_link(r, "Community Food Bank Assn", "111111111RR0001", "ON")
    assert link3.match_method == "exact_bn"
    assert link3.entity_id == link1.entity_id

    link4 = resolve_link(r, "Community Food Bank Assn", "222222222RR0001", "ON")
    assert link4.match_method == "exact_bn"
    assert link4.entity_id == link2.entity_id


def test_residual_no_bn_still_dedupes_by_name_and_province_as_before():
    r = Resolver()
    link1 = resolve_link(r, "Some Unmatched Org", None, "AB")
    link2 = resolve_link(r, "Some Unmatched Org", None, "AB")
    assert link1.entity_id == link2.entity_id


# ── Bug 2b: bilingual pipe-name normalization ────────────────────────────────

def test_normalize_name_splits_on_pipe_and_keeps_english_half():
    assert normalize_name("Ottawa Humane Society|Societe humaine d'Ottawa") == normalize_name("Ottawa Humane Society")


def test_pipe_formatted_bilingual_name_collapses_to_one_entity():
    r = Resolver()
    link1 = resolve_link(r, "Ottawa Humane Society", None, "ON")
    link2 = resolve_link(r, "Ottawa Humane Society|Societe humaine d'Ottawa", None, "ON")
    assert link1.entity_id == link2.entity_id


def test_normalize_name_without_pipe_is_unaffected():
    # "Society" is a legal-suffix stopword (LEGAL_SUFFIXES), stripped regardless
    # of the pipe logic -- this just confirms the pipe change didn't touch
    # ordinary (non-bilingual) names.
    assert normalize_name("Toronto Humane Society") == "TORONTO HUMANE"


# ── Related gap found during verification: canonical_name still stored the ──
# raw pipe string even though normalize_name() strips it for matching (e.g.
# Prince Rupert Port Authority's canonical_name was literally "Prince Rupert
# Port Authority|Administration portuaire de Prince Rupert"). display_name()
# applies the same pipe-split for display, without normalize_name()'s
# uppercasing/legal-suffix-stripping, since canonical_name is a display value.

def test_display_name_strips_pipe_but_preserves_case_and_suffixes():
    assert display_name("Prince Rupert Port Authority|Administration portuaire de Prince Rupert") == \
        "Prince Rupert Port Authority"


def test_display_name_without_pipe_is_unchanged():
    assert display_name("Ontario Trillium Foundation") == "Ontario Trillium Foundation"


def test_display_name_handles_none_and_empty():
    assert display_name(None) is None
    assert display_name("") == ""


def test_residual_entity_canonical_name_is_english_half_not_raw_pipe_string():
    r = Resolver()
    link = resolve_link(
        r, "Prince Rupert Port Authority|Administration portuaire de Prince Rupert", None, "BC"
    )
    stored = r.entities[link.entity_id - 1].canonical_name
    assert stored == "Prince Rupert Port Authority", (
        f"canonical_name still stores the raw bilingual pipe string: {stored!r}"
    )


def test_charity_canonical_name_is_english_half_not_raw_pipe_string():
    r = Resolver()
    eid = r.add_charity(
        "123456789", "Ottawa Humane Society|Societe humaine d'Ottawa", "Ottawa", "ON"
    )
    assert r.entities[eid - 1].canonical_name == "Ottawa Humane Society"


# ── Bug 1: amendment dedup SQL ────────────────────────────────────────────────
# Dedup key is (owner_org, ref_number), NOT ref_number alone: ref_number is not
# globally unique -- refs collide across departments (confirmed: 24,851 refs,
# e.g. GC-2016-Q4-00001 is six different grants from six different departments,
# all at amendment 0). Ref-only dedup would collapse those into one arbitrary
# row each, silently discarding genuinely distinct agreements.

def test_latest_amendment_dedup_keeps_max_amendment_per_dept_and_ref():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE raw_grants AS SELECT * FROM (VALUES
            ('deptA', 'R1', '0', 100.0),
            ('deptA', 'R1', '1', 150.0),
            ('deptA', 'R1', '2', 175.0),
            ('deptA', 'R2', '', 50.0),
            ('deptA', 'R3', '3', 30.0)
        ) AS t(owner_org, ref_number, amendment_number, agreement_value)
    """)
    con.execute(f"CREATE TABLE latest AS {_latest_amendment_sql('raw_grants')}")
    rows = con.execute(
        "SELECT owner_org, ref_number, amendment_number, agreement_value FROM latest ORDER BY ref_number"
    ).fetchall()
    assert rows == [
        ("deptA", "R1", "2", 175.0),  # only the highest amendment_number for R1 survives
        ("deptA", "R2", "", 50.0),    # blank amendment_number treated as 0 (original), single row unaffected
        ("deptA", "R3", "3", 30.0),
    ]
    total = con.execute("SELECT SUM(agreement_value) FROM latest").fetchone()[0]
    assert total == 175.0 + 50.0 + 30.0, "deduped total must not include superseded amendment rows"


def test_latest_amendment_dedup_row_count_matches_distinct_dept_ref_pairs():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE raw_grants AS SELECT * FROM (VALUES
            ('deptA', 'A-1', '0', 10.0), ('deptA', 'A-1', '1', 20.0),
            ('deptA', 'B-1', '0', 5.0),
            ('deptA', 'C-1', NULL, 7.0)
        ) AS t(owner_org, ref_number, amendment_number, agreement_value)
    """)
    con.execute(f"CREATE TABLE latest AS {_latest_amendment_sql('raw_grants')}")
    n = con.execute("SELECT COUNT(*) FROM latest").fetchone()[0]
    distinct_pairs = con.execute(
        "SELECT COUNT(DISTINCT (owner_org, ref_number)) FROM raw_grants"
    ).fetchone()[0]
    assert n == distinct_pairs == 3


def test_latest_amendment_dedup_keeps_both_sides_of_a_ref_number_collision():
    # The confirmed real-world failure mode: the SAME ref_number used by
    # different departments for genuinely different grants. Ref-only dedup
    # would arbitrarily keep just one; both must survive since (dept, ref)
    # is the real key.
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE raw_grants AS SELECT * FROM (VALUES
            ('dept-one', 'GC-2016-Q4-00001', '0', 1000.0),
            ('dept-two', 'GC-2016-Q4-00001', '0', 2000.0)
        ) AS t(owner_org, ref_number, amendment_number, agreement_value)
    """)
    con.execute(f"CREATE TABLE latest AS {_latest_amendment_sql('raw_grants')}")
    rows = con.execute(
        "SELECT owner_org, ref_number, agreement_value FROM latest ORDER BY owner_org"
    ).fetchall()
    assert rows == [
        ("dept-one", "GC-2016-Q4-00001", 1000.0),
        ("dept-two", "GC-2016-Q4-00001", 2000.0),
    ], "a colliding ref_number shared by two departments must not collapse to one row"


def test_latest_amendment_dedup_trims_owner_org_and_ref_number():
    # Confirmed real-world case: at least one ref has a trailing space, which
    # would otherwise be treated as a different key than its untrimmed twin.
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE raw_grants AS SELECT * FROM (VALUES
            ('deptA', 'GC-2016-Q4-00001 ', '0', 10.0),
            ('deptA', 'GC-2016-Q4-00001', '1', 20.0)
        ) AS t(owner_org, ref_number, amendment_number, agreement_value)
    """)
    con.execute(f"CREATE TABLE latest AS {_latest_amendment_sql('raw_grants')}")
    n = con.execute("SELECT COUNT(*) FROM latest").fetchone()[0]
    assert n == 1, "untrimmed whitespace must not be treated as a distinct ref"
    value = con.execute("SELECT agreement_value FROM latest").fetchone()[0]
    assert value == 20.0, "the higher amendment_number must win once trimmed to the same key"


# ── entity_financials_by_year dedup (org-page funding timeline feature) ──────
# raw_t3010_fin is documented as one row per BN per filing year (source_year),
# not per (bn_root, fiscal_year) -- a late-filed or refiled return can put two
# different source_year rows on the same FPE-derived fiscal year. Unlike
# entity_financials (latest source_year per bn_root only), this table keeps
# every fiscal year, so it needs its own dedup keyed on (bn_root, fiscal_year).

def test_entity_financials_by_year_dedupes_duplicate_source_year_for_same_fiscal_year():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE entities AS SELECT * FROM (VALUES
            (1, '123456789')
        ) AS t(entity_id, bn_root)
    """)
    con.execute("""
        CREATE TABLE raw_t3010_fin AS SELECT * FROM (VALUES
            ('123456789RR0001', '2017-12-31', 2017, '500000', '100000', '50000'),
            ('123456789RR0001', '2017-12-31', 2018, '600000', '150000', '60000')
        ) AS t(BN, FPE, source_year, "4700", "4570", "4510")
    """)
    build_entity_financials_by_year(con)
    rows = con.execute("""
        SELECT entity_id, fiscal_year, total_revenue, gov_revenue, foundation_revenue
        FROM entity_financials_by_year
    """).fetchall()
    # Both raw rows land on fiscal_year=2017 (same FPE); only the higher
    # source_year (a later/refiled return) survives, and its values win --
    # not a merge or average of the two.
    assert rows == [(1, 2017, 600000.0, 150000.0, 60000.0)]


TESTS = [
    test_residual_bn_gets_registered_for_later_exact_match,
    test_residual_bn_backfills_onto_existing_name_matched_entity,
    test_residual_bn_collision_does_not_merge_different_orgs,
    test_residual_no_bn_still_dedupes_by_name_and_province_as_before,
    test_normalize_name_splits_on_pipe_and_keeps_english_half,
    test_pipe_formatted_bilingual_name_collapses_to_one_entity,
    test_normalize_name_without_pipe_is_unaffected,
    test_display_name_strips_pipe_but_preserves_case_and_suffixes,
    test_display_name_without_pipe_is_unchanged,
    test_display_name_handles_none_and_empty,
    test_residual_entity_canonical_name_is_english_half_not_raw_pipe_string,
    test_charity_canonical_name_is_english_half_not_raw_pipe_string,
    test_latest_amendment_dedup_keeps_max_amendment_per_dept_and_ref,
    test_latest_amendment_dedup_row_count_matches_distinct_dept_ref_pairs,
    test_latest_amendment_dedup_keeps_both_sides_of_a_ref_number_collision,
    test_latest_amendment_dedup_trims_owner_org_and_ref_number,
    test_entity_financials_by_year_dedupes_duplicate_source_year_for_same_fiscal_year,
]


def main():
    failures = []
    for test in TESTS:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except AssertionError as e:
            failures.append(test.__name__)
            print(f"  FAIL  {test.__name__}: {e}")
    print(f"\n{len(TESTS) - len(failures)}/{len(TESTS)} passed")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
