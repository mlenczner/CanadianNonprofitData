"""
Regression tests for the BN near-miss review queue and the shared entity-
merge mechanism in analysis/build_entity_graph.py (BN-hygiene spec item 2).

Motivating case (verified against the real DB): the Canadian Red Cross is
split across entities including bn_root 119219814 ($904M, the real BN) and
bn_root 011921981 ($502M, the same BN with a leading zero inserted and the
true last digit dropped -- a single-position substitution can't produce this
pair, but "insert one digit at the front, truncate the true trailing digit"
can, matching a very plausible real-world corruption where a BN got treated
as a number somewhere upstream and lost/gained a leading zero). A second,
unrelated pair, bn_root 119219814 vs 119218814, differs by one substituted
digit -- a plain typo.

_bn_substitution_candidates()/_bn_shift_candidates() generate exactly these
two corruption shapes (not general edit-distance-1-at-any-position, which
would also flag unrelated near-duplicate BNs from two different real
organizations far too often). build_bn_near_miss_review() combines that
candidate generation with a folded-name-similarity gate (FUZZY_ACCEPT) and
writes a review CSV -- no auto-merge, since a wrong BN merge is worse than a
split. _apply_entity_merges()/apply_bn_merge_overrides() are the separate,
human-gated mechanism that actually performs a confirmed merge, reading
data/bn_merge_overrides.csv.

Run with:
    .venv/bin/python -m pytest tests/test_bn_near_miss_and_merges.py
"""

import csv
import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.build_entity_graph import (
    _apply_entity_merges,
    _bn_shift_candidates,
    _bn_substitution_candidates,
    apply_bn_merge_overrides,
    build_bn_near_miss_review,
)


# ── candidate generation ────────────────────────────────────────────────────

def test_substitution_candidates_cover_single_digit_typo():
    real = "119219814"
    typo = "119218814"  # position 5: '9' -> '8'
    assert typo in set(_bn_substitution_candidates(real))


def test_substitution_candidates_are_all_same_length_and_differ_by_one_digit():
    root = "119219814"
    for cand in _bn_substitution_candidates(root):
        assert len(cand) == len(root)
        diff = sum(1 for a, b in zip(root, cand) if a != b)
        assert diff == 1


def test_shift_candidates_cover_leading_zero_insertion_and_trailing_drop():
    real = "119219814"
    mangled = "011921981"  # '0' + real[:8]
    assert mangled in set(_bn_shift_candidates(real))


def test_shift_candidates_are_symmetric_leading_and_trailing():
    root = "119219814"
    candidates = set(_bn_shift_candidates(root))
    assert "0" + root[:8] in candidates  # leading digit inserted, trailing dropped
    assert root[1:] + "0" in candidates  # leading digit dropped, trailing digit appended


# ── build_bn_near_miss_review() ─────────────────────────────────────────────

def _make_fixture_db(con, entities, role_summary=None):
    con.execute("""
        CREATE TABLE entities (
            entity_id INTEGER, bn_root VARCHAR, canonical_name VARCHAR,
            city VARCHAR, province VARCHAR, entity_kind VARCHAR,
            bn_full VARCHAR, search_name VARCHAR
        )
    """)
    con.executemany(
        "INSERT INTO entities (entity_id, bn_root, canonical_name, province, entity_kind, search_name) "
        "VALUES (?,?,?,?,?,?)",
        entities,
    )
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
            "INSERT INTO entity_role_summary "
            "(entity_id, total_given, total_received) VALUES (?,?,?)",
            role_summary,
        )


def test_near_miss_review_catches_the_red_cross_leading_zero_pair(tmp_path):
    con = duckdb.connect(":memory:")
    _make_fixture_db(con, [
        (1, "119219814", "THE CANADIAN RED CROSS SOCIETY", "ON", "charity",
         "the canadian red cross society"),
        (2, "011921981", "CANADIAN RED CROSS SOCIETY", "ON", "other_org",
         "canadian red cross society"),
    ], role_summary=[(1, 904_000_000, 0), (2, 502_000_000, 0)])
    out_path = tmp_path / "bn_near_miss_review.csv"
    n = build_bn_near_miss_review(con, out_path=str(out_path))
    assert n == 1
    with open(out_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    ids = {rows[0]["entity_id_a"], rows[0]["entity_id_b"]}
    assert ids == {"1", "2"}
    assert rows[0]["edit_type"] in ("insertion_deletion",)


def test_near_miss_review_catches_the_red_cross_typo_pair(tmp_path):
    con = duckdb.connect(":memory:")
    _make_fixture_db(con, [
        (1, "119219814", "THE CANADIAN RED CROSS SOCIETY", "ON", "charity",
         "the canadian red cross society"),
        (3, "119218814", "The Canadian Red Cross Society", "ON", "other_org",
         "the canadian red cross society"),
    ], role_summary=[(1, 904_000_000, 0), (3, 218_000_000, 0)])
    out_path = tmp_path / "bn_near_miss_review.csv"
    n = build_bn_near_miss_review(con, out_path=str(out_path))
    assert n == 1
    with open(out_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["edit_type"] == "substitution"


def test_near_miss_review_matches_across_a_bilingual_name_suffix(tmp_path):
    # Confirmed real bug: the real Canadian Red Cross charity's
    # canonical_name carries a bilingual "/ La Societe..." suffix that its
    # corrupted-BN duplicate's name lacks. Scoring the two raw canonical
    # names (or search_names, which only fold case/accents/whitespace and
    # don't touch this) directly gives ~61 -- well under FUZZY_ACCEPT --
    # even though the English half is a 100% match. name_variants() (the
    # same bilingual "/"-splitting helper add_charity() already uses to
    # index fuzzy candidates) must be applied here too, taking the best
    # score across every variant pairing.
    con = duckdb.connect(":memory:")
    _make_fixture_db(con, [
        (1, "119219814", "THE CANADIAN RED CROSS SOCIETY / LA SOCIETE CANADIENNE DE LA CROIX-ROUGE",
         "NB", "charity", "the canadian red cross society / la societe canadienne de la croix-rouge"),
        (2, "119218814", "THE CANADIAN RED CROSS SOCIETY", "ON", "other_org",
         "the canadian red cross society"),
    ], role_summary=[(1, 904_000_000, 0), (2, 217_000_000, 0)])
    out_path = tmp_path / "bn_near_miss_review.csv"
    n = build_bn_near_miss_review(con, out_path=str(out_path))
    assert n == 1
    with open(out_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert float(rows[0]["name_similarity_score"]) == 100.0


def test_near_miss_review_does_not_flag_dissimilar_names_even_if_bn_is_close(tmp_path):
    con = duckdb.connect(":memory:")
    _make_fixture_db(con, [
        (1, "119219814", "THE CANADIAN RED CROSS SOCIETY", "ON", "charity",
         "the canadian red cross society"),
        (2, "119218814", "Totally Unrelated Nonprofit Society", "BC", "other_org",
         "totally unrelated nonprofit society"),
    ])
    out_path = tmp_path / "bn_near_miss_review.csv"
    n = build_bn_near_miss_review(con, out_path=str(out_path))
    assert n == 0


def test_near_miss_review_does_not_flag_bns_further_than_one_edit_apart(tmp_path):
    con = duckdb.connect(":memory:")
    _make_fixture_db(con, [
        (1, "119219814", "THE CANADIAN RED CROSS SOCIETY", "ON", "charity",
         "the canadian red cross society"),
        (2, "999999999", "THE CANADIAN RED CROSS SOCIETY", "ON", "other_org",
         "the canadian red cross society"),
    ])
    out_path = tmp_path / "bn_near_miss_review.csv"
    n = build_bn_near_miss_review(con, out_path=str(out_path))
    assert n == 0


def test_near_miss_review_reports_no_duplicate_pairs(tmp_path):
    # Each unordered pair should appear once, not twice (A near-misses B and
    # B near-misses A are the same finding).
    con = duckdb.connect(":memory:")
    _make_fixture_db(con, [
        (1, "119219814", "THE CANADIAN RED CROSS SOCIETY", "ON", "charity",
         "the canadian red cross society"),
        (3, "119218814", "The Canadian Red Cross Society", "ON", "other_org",
         "the canadian red cross society"),
    ])
    out_path = tmp_path / "bn_near_miss_review.csv"
    n = build_bn_near_miss_review(con, out_path=str(out_path))
    assert n == 1


# ── _apply_entity_merges() / apply_bn_merge_overrides() ────────────────────

def _make_merge_fixture_db(con):
    con.execute("""
        CREATE TABLE entities (
            entity_id INTEGER, bn_root VARCHAR, canonical_name VARCHAR,
            city VARCHAR, province VARCHAR, entity_kind VARCHAR
        )
    """)
    con.executemany("INSERT INTO entities VALUES (?,?,?,?,?,?)", [
        (1, "119219814", "THE CANADIAN RED CROSS SOCIETY", None, "ON", "charity"),
        (2, "011921981", "CANADIAN RED CROSS SOCIETY", None, "ON", "other_org"),
    ])
    con.execute("""
        CREATE TABLE grants_unified (
            grant_id INTEGER, source_dataset VARCHAR, funder_entity_id INTEGER,
            recipient_entity_id INTEGER, amount_cad DOUBLE, fiscal_year INTEGER,
            program_name VARCHAR, description VARCHAR, source_ref VARCHAR
        )
    """)
    con.executemany("INSERT INTO grants_unified VALUES (?,?,?,?,?,?,?,?,?)", [
        (1, "t3010_qualified_donee", 99, 2, 502_000_000.0, 2024, "Qualified donee gift", None, None),
    ])
    con.execute("""
        CREATE TABLE entity_links (
            entity_id INTEGER, source_dataset VARCHAR, raw_name VARCHAR,
            raw_bn VARCHAR, match_method VARCHAR, match_score DOUBLE
        )
    """)
    con.executemany("INSERT INTO entity_links VALUES (?,?,?,?,?,?)", [
        (2, "t3010_qualified_donee", "Canadian Red Cross Society", "011921981RR0001", "exact_bn", 100.0),
    ])


def test_apply_entity_merges_remaps_grants_and_links_and_removes_merged_entity():
    con = duckdb.connect(":memory:")
    _make_merge_fixture_db(con)
    n = _apply_entity_merges(con, [(1, 2)])
    assert n == 1
    remaining = con.execute("SELECT entity_id FROM entities ORDER BY 1").fetchall()
    assert remaining == [(1,)]
    grant_recipient = con.execute("SELECT recipient_entity_id FROM grants_unified WHERE grant_id = 1").fetchone()[0]
    assert grant_recipient == 1
    link_entity = con.execute("SELECT entity_id FROM entity_links WHERE raw_bn = '011921981RR0001'").fetchone()[0]
    assert link_entity == 1


def test_apply_bn_merge_overrides_reads_csv_and_applies_merge(tmp_path):
    con = duckdb.connect(":memory:")
    _make_merge_fixture_db(con)
    overrides_path = tmp_path / "bn_merge_overrides.csv"
    with open(overrides_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["entity_id_keep", "entity_id_merge", "note"])
        w.writerow([1, 2, "Red Cross BN typo -- leading zero inserted, last digit dropped"])
    n = apply_bn_merge_overrides(con, overrides_path=str(overrides_path))
    assert n == 1
    remaining = con.execute("SELECT entity_id FROM entities ORDER BY 1").fetchall()
    assert remaining == [(1,)]


def test_apply_bn_merge_overrides_is_a_noop_when_file_absent(tmp_path):
    con = duckdb.connect(":memory:")
    _make_merge_fixture_db(con)
    n = apply_bn_merge_overrides(con, overrides_path=str(tmp_path / "does_not_exist.csv"))
    assert n == 0
    remaining = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert remaining == 2
