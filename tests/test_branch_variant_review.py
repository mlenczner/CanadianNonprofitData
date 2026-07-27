"""
Regression tests for the branch-suffix name variant review queue in
analysis/build_entity_graph.py (BN-hygiene spec item 4).

A BN-less residual entity like "Canadian Red Cross – Ottawa" might be a
genuinely separate regional entry, or it might be the same organization's
local office/branch recorded under a BN-bearing entity like "THE CANADIAN
RED CROSS SOCIETY" -- name shape alone can't tell the two apart, so
build_branch_variant_review() only ever emits a review CSV
(analysis/output/branch_variant_review.csv), never an automatic merge.
Confirmed merges route through the same data/bn_merge_overrides.csv /
apply_bn_merge_overrides() mechanism build_bn_near_miss_review() uses.

Run with:
    .venv/bin/python -m pytest tests/test_branch_variant_review.py
"""

import csv
import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.build_entity_graph import build_branch_variant_review


def _make_db(con, entities, role_summary=None):
    con.execute("""
        CREATE TABLE entities (
            entity_id INTEGER, bn_root VARCHAR, canonical_name VARCHAR,
            city VARCHAR, province VARCHAR, entity_kind VARCHAR
        )
    """)
    con.executemany("INSERT INTO entities VALUES (?,?,?,?,?,?)", entities)
    con.execute("""
        CREATE TABLE entity_role_summary (
            entity_id INTEGER, canonical_name VARCHAR, entity_kind VARCHAR,
            total_given DOUBLE, total_received DOUBLE,
            n_grants_given INTEGER, n_grants_received INTEGER,
            given_share DOUBLE, role VARCHAR
        )
    """)
    if role_summary:
        con.executemany(
            "INSERT INTO entity_role_summary (entity_id, total_given, total_received) VALUES (?,?,?)",
            role_summary,
        )


def test_en_dash_branch_suffix_matches_bn_bearing_twin(tmp_path):
    con = duckdb.connect(":memory:")
    _make_db(con, [
        (1, "119219814", "THE CANADIAN RED CROSS SOCIETY", None, "ON", "charity"),
        (2, None, "Canadian Red Cross – Ottawa", None, None, "other_org"),
    ], role_summary=[(1, 904_000_000, 0), (2, 157_000_000, 0)])
    out_path = tmp_path / "branch_variant_review.csv"
    n = build_branch_variant_review(con, out_path=str(out_path))
    assert n == 1
    with open(out_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["branch_suffix"] == "Ottawa"
    assert rows[0]["bnless_entity_id"] == "2"
    assert rows[0]["bn_entity_id"] == "1"


def test_hyphen_with_surrounding_spaces_matches(tmp_path):
    con = duckdb.connect(":memory:")
    _make_db(con, [
        (1, "123456789", "Big Brothers Big Sisters", None, "ON", "other_org"),
        (2, None, "Big Brothers Big Sisters - Peterborough", None, None, "other_org"),
    ])
    out_path = tmp_path / "branch_variant_review.csv"
    n = build_branch_variant_review(con, out_path=str(out_path))
    assert n == 1


def test_parenthesized_place_suffix_matches(tmp_path):
    con = duckdb.connect(":memory:")
    _make_db(con, [
        (1, "123456789", "Boys and Girls Club", None, "MB", "other_org"),
        (2, None, "Boys and Girls Club (Winnipeg)", None, None, "other_org"),
    ])
    out_path = tmp_path / "branch_variant_review.csv"
    n = build_branch_variant_review(con, out_path=str(out_path))
    assert n == 1
    with open(out_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["branch_suffix"] == "Winnipeg"


def test_no_match_when_no_bn_bearing_twin(tmp_path):
    con = duckdb.connect(":memory:")
    _make_db(con, [
        (1, None, "Some Unrelated Org – Halifax", None, None, "other_org"),
    ])
    out_path = tmp_path / "branch_variant_review.csv"
    n = build_branch_variant_review(con, out_path=str(out_path))
    assert n == 0


def test_compound_hyphenated_word_without_spaces_is_not_split(tmp_path):
    # "Meals-on-Wheels" has no spaces around its hyphens -- must not be
    # treated as "Meals" + branch suffix "on-Wheels" even if an unrelated
    # BN-bearing entity happens to be named just "Meals".
    con = duckdb.connect(":memory:")
    _make_db(con, [
        (1, "123456789", "Meals", None, "AB", "other_org"),
        (2, None, "Meals-on-Wheels", None, None, "other_org"),
    ])
    out_path = tmp_path / "branch_variant_review.csv"
    n = build_branch_variant_review(con, out_path=str(out_path))
    assert n == 0


def test_no_suffix_shape_produces_no_candidate(tmp_path):
    con = duckdb.connect(":memory:")
    _make_db(con, [
        (1, "123456789", "Plain Name Org", None, "AB", "other_org"),
        (2, None, "Plain Name Org", None, None, "other_org"),
    ])
    out_path = tmp_path / "branch_variant_review.csv"
    n = build_branch_variant_review(con, out_path=str(out_path))
    assert n == 0
