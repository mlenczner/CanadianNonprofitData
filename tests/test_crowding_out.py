"""
Tests for analysis/crowding_out.py (Part A of
docs/crowding-out-and-flow-through-spec.md).

Uses hand-crafted in-memory DuckDB fixtures, same pattern as
tests/test_t3010_qd_dedup.py / tests/test_bn_near_miss_and_merges.py for
build_entity_graph.py's table-building functions.

Run with:
    .venv/bin/python -m pytest tests/test_crowding_out.py
"""

import os
import sys

import duckdb
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.crowding_out import (
    compute_treatment_events,
    compute_never_treated_pool,
    match_controls,
    threshold_sensitivity,
    winsorized_median_pct,
    naive_contrast,
)


def _build_fixture(con, grants_rows, fin_rows, entities_rows):
    """grants_rows: list of (entity_id, source_dataset, fiscal_year, amount_cad).
    fin_rows: list of (BN, FPE, "4500", "4700", source_year).
    entities_rows: list of (entity_id, bn_root, province)."""
    con.execute("""
        CREATE TABLE entities (entity_id INTEGER, bn_root VARCHAR, province VARCHAR)
    """)
    if entities_rows:
        con.executemany("INSERT INTO entities VALUES (?, ?, ?)", entities_rows)

    con.execute("""
        CREATE TABLE grants_unified (
            grant_id INTEGER, source_dataset VARCHAR, funder_entity_id INTEGER,
            recipient_entity_id INTEGER, amount_cad DOUBLE, fiscal_year INTEGER
        )
    """)
    if grants_rows:
        con.executemany(
            "INSERT INTO grants_unified (source_dataset, recipient_entity_id, fiscal_year, amount_cad) "
            "VALUES (?, ?, ?, ?)",
            grants_rows,
        )

    con.execute("""
        CREATE TABLE raw_t3010_fin (
            BN VARCHAR, FPE VARCHAR, "4500" VARCHAR, "4700" VARCHAR, source_year INTEGER
        )
    """)
    if fin_rows:
        con.executemany('INSERT INTO raw_t3010_fin VALUES (?, ?, ?, ?, ?)', fin_rows)

    con.execute("""
        CREATE TABLE entity_financials_by_year (entity_id INTEGER, fiscal_year INTEGER, total_revenue DOUBLE)
    """)
    con.execute("""
        CREATE TABLE entity_links (
            entity_id INTEGER, source_dataset VARCHAR, raw_name VARCHAR,
            raw_bn VARCHAR, match_method VARCHAR, match_score DOUBLE
        )
    """)


def _fin_row(bn, year, donations, source_year, revenue=None):
    return (bn, f"{year}-12-31", str(donations) if donations is not None else None,
            str(revenue) if revenue is not None else None, source_year)


# ── Treatment detection ──────────────────────────────────────────────────

def test_prior_year_grant_disqualifies_a_later_year_as_first_treatment():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[
            ("federal_gc", 1, 2015, 10_000),   # small grant in 2015 -- first receipt year
            ("federal_gc", 1, 2017, 150_000),  # big grant in 2017, but NOT the first receipt year
        ],
        fin_rows=[],
        entities_rows=[(1, "111111111", "ON")],
    )
    events = compute_treatment_events(con, ("federal_gc",), 100_000, [2016, 2017, 2018])
    assert "111111111" not in events["bn_root"].tolist(), \
        "org's first receipt was 2015 (below threshold) -- 2017 must not qualify as its treatment year"


def test_org_with_no_prior_receipts_and_first_year_above_threshold_is_treated():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[("federal_gc", 2, 2017, 150_000)],
        fin_rows=[],
        entities_rows=[(2, "222222222", "ON")],
    )
    events = compute_treatment_events(con, ("federal_gc",), 100_000, [2016, 2017, 2018])
    rows = events[events.bn_root == "222222222"]
    assert len(rows) == 1 and int(rows.iloc[0].Y) == 2017


def test_threshold_is_a_same_year_sum_not_cumulative_across_years():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[
            ("federal_gc", 3, 2016, 60_000),
            ("federal_gc", 3, 2017, 60_000),  # cumulative 120k, but no single year hits 100k
        ],
        fin_rows=[],
        entities_rows=[(3, "333333333", "ON")],
    )
    events = compute_treatment_events(con, ("federal_gc",), 100_000, [2016, 2017, 2018])
    assert "333333333" not in events["bn_root"].tolist(), \
        "no single fiscal year crosses the threshold -- cumulative total must not count"


def test_threshold_crossing_is_inclusive_ge_not_strictly_greater():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[("federal_gc", 4, 2018, 100_000)],  # exactly at threshold
        fin_rows=[],
        entities_rows=[(4, "444444444", "ON")],
    )
    events = compute_treatment_events(con, ("federal_gc",), 100_000, [2016, 2017, 2018])
    assert "444444444" in events["bn_root"].tolist(), "exactly $100k must qualify (>=, not >)"


def test_federal_junk_fiscal_years_are_excluded_from_treatment_detection():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[
            ("federal_gc", 5, 1899, 500_000),   # known-bad federal_gc year, must be dropped
            ("federal_gc", 5, 2017, 150_000),   # real first receipt once 1899 is dropped
        ],
        fin_rows=[],
        entities_rows=[(5, "555555555", "ON")],
    )
    events = compute_treatment_events(con, ("federal_gc",), 100_000, [2016, 2017, 2018])
    rows = events[events.bn_root == "555555555"]
    assert len(rows) == 1 and int(rows.iloc[0].Y) == 2017, \
        "the bogus 1899 row must not count as an earlier receipt"


def test_otf_is_not_filtered_by_federal_junk_years_since_2002_is_real_otf_data():
    # otf genuinely has multi-thousand-row years in 1999-2004 (confirmed against
    # the real DB) -- the junk-year exclusion must stay scoped to federal_gc.
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[
            ("otf", 6, 2002, 500_000),  # a real otf receipt in a year that's junk *for federal_gc only*
        ],
        fin_rows=[],
        entities_rows=[(6, "666666666", "ON")],
    )
    events = compute_treatment_events(con, ("otf",), 100_000, list(range(1999, 2005)))
    assert "666666666" in events["bn_root"].tolist(), \
        "otf's real 2002 receipt must not be dropped by federal_gc's junk-year filter"


# ── Donations panel dedup ────────────────────────────────────────────────

def test_multiple_filings_same_year_dedup_to_latest_source_year():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[("federal_gc", 7, 2018, 150_000)],
        fin_rows=[
            _fin_row("777777777", 2015, 1000, source_year=2015),
            _fin_row("777777777", 2015, 9999, source_year=2016),  # amended/refiled -- later source_year wins
            _fin_row("777777777", 2016, 1100, source_year=2016),
            _fin_row("777777777", 2017, 1200, source_year=2017),
            _fin_row("777777777", 2019, 2000, source_year=2019),
            _fin_row("777777777", 2020, 2100, source_year=2020),
        ],
        entities_rows=[(7, "777777777", "ON")],
    )
    compute_treatment_events(con, ("federal_gc",), 100_000, [2018])
    from analysis.crowding_out import _window_stats_sql
    stats = con.execute(_window_stats_sql("SELECT bn_root, Y FROM (SELECT '777777777' AS bn_root, 2018 AS Y)")).fetchdf()
    row = stats[stats.bn_root == "777777777"].iloc[0]
    assert row.pre_mean == (9999 + 1100 + 1200) / 3, \
        "the 2015 dedup must keep source_year=2016's value (9999), not source_year=2015's (1000)"


# ── Window / min-observation rule ────────────────────────────────────────

def test_pre_window_with_only_one_observed_year_is_dropped():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[("federal_gc", 8, 2018, 150_000)],
        fin_rows=[
            _fin_row("888888888", 2017, 1000, source_year=2017),  # only 1 pre-year observed (2015, 2016 missing)
            _fin_row("888888888", 2019, 1100, source_year=2019),
            _fin_row("888888888", 2020, 1200, source_year=2020),
            _fin_row("888888888", 2021, 1300, source_year=2021),
        ],
        entities_rows=[(8, "888888888", "ON")],
    )
    _, treated_stats, _ = naive_contrast(con, ("federal_gc",), 100_000, [2018])
    assert "888888888" not in treated_stats["bn_root"].tolist(), \
        "only 1 pre-year observed (< MIN_OBS_PER_WINDOW=2) -- org must be dropped"


def test_pre_and_post_window_with_two_observed_years_each_is_kept():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[("federal_gc", 9, 2018, 150_000)],
        fin_rows=[
            _fin_row("999999999", 2016, 1000, source_year=2016),
            _fin_row("999999999", 2017, 1000, source_year=2017),
            _fin_row("999999999", 2019, 1500, source_year=2019),
            _fin_row("999999999", 2020, 1500, source_year=2020),
        ],
        entities_rows=[(9, "999999999", "ON")],
    )
    _, treated_stats, _ = naive_contrast(con, ("federal_gc",), 100_000, [2018])
    rows = treated_stats[treated_stats.bn_root == "999999999"]
    assert len(rows) == 1
    assert rows.iloc[0].pre_n == 2 and rows.iloc[0].post_n == 2
    assert abs(rows.iloc[0]["pct_change"] - 0.5) < 1e-9


# ── Never-treated pool ───────────────────────────────────────────────────

def test_never_treated_pool_excludes_any_org_with_any_receipt():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[("federal_gc", 10, 2016, 5_000)],  # below threshold, but a real receipt
        fin_rows=[
            _fin_row("101010101", 2016, 1000, source_year=2016),
            _fin_row("202020202", 2016, 1000, source_year=2016),  # never received anything
        ],
        entities_rows=[(10, "101010101", "ON"), (11, "202020202", "ON")],
    )
    pool = compute_never_treated_pool(con, ("federal_gc",))
    assert "202020202" in pool
    assert "101010101" not in pool, \
        "an org with any receipt (even below threshold) is not never-treated"


# ── Matched controls ──────────────────────────────────────────────────────

def test_matched_control_never_crosses_province():
    treated_cov = pd.DataFrame([
        {"bn_root": "T1", "Y": 2018, "province": "ON", "revenue_decile": 5,
         "trend_tercile": 1, "revenue_y_minus_1": 100_000},
    ])
    control_cov = pd.DataFrame([
        {"bn_root": "C1", "Y": 2018, "province": "ON", "revenue_decile": 5,
         "trend_tercile": 1, "revenue_y_minus_1": 100_000},
        {"bn_root": "C2", "Y": 2018, "province": "QC", "revenue_decile": 5,
         "trend_tercile": 1, "revenue_y_minus_1": 100_000},  # perfect match except province
    ])
    matches = match_controls(treated_cov, control_cov, max_controls=5)
    assert set(matches.control_bn_root) == {"C1"}, \
        "a QC control must never be matched to an ON treated org regardless of decile/trend fit"


def test_matched_control_relaxes_tiers_when_no_full_match_exists():
    treated_cov = pd.DataFrame([
        {"bn_root": "T1", "Y": 2018, "province": "ON", "revenue_decile": 5,
         "trend_tercile": 1, "revenue_y_minus_1": 100_000},
    ])
    control_cov = pd.DataFrame([
        # same province + decile, but different trend tercile -- no full-tier match exists
        {"bn_root": "C1", "Y": 2018, "province": "ON", "revenue_decile": 5,
         "trend_tercile": 2, "revenue_y_minus_1": 90_000},
    ])
    matches = match_controls(treated_cov, control_cov, max_controls=5)
    assert list(matches.control_bn_root) == ["C1"]
    assert matches.iloc[0].match_tier == "province+decile"


# ── Robustness helpers (pure pandas) ─────────────────────────────────────

def test_winsorized_median_pct_returns_none_for_empty_input():
    empty = pd.DataFrame({"pct_change": []})
    assert winsorized_median_pct(empty) is None


def test_winsorized_median_pct_clips_extreme_values_before_taking_median():
    df = pd.DataFrame({"pct_change": [0.05, 0.06, 0.07, 0.08, 100.0]})
    lo, hi = df["pct_change"].quantile([0.01, 0.99])
    expected = round(100 * df["pct_change"].clip(lo, hi).median(), 1)
    assert winsorized_median_pct(df, pct=0.01) == expected
    assert df["pct_change"].clip(lo, hi).max() < 100.0, "the outlier must have been pulled down"


def test_threshold_sensitivity_higher_threshold_yields_fewer_or_equal_treated():
    con = duckdb.connect(":memory:")
    fin_rows = []
    for bn in ("110000000", "120000000"):
        for year, val in [(2014, 1000), (2015, 1000), (2016, 1000), (2018, 1200), (2019, 1200), (2020, 1200)]:
            fin_rows.append(_fin_row(bn, year, val, source_year=year))
    _build_fixture(
        con,
        grants_rows=[
            ("federal_gc", 12, 2017, 60_000),   # qualifies only at the $50k threshold
            ("federal_gc", 13, 2017, 150_000),  # qualifies at $50k and $100k, not $250k
        ],
        fin_rows=fin_rows,
        entities_rows=[(12, "110000000", "ON"), (13, "120000000", "ON")],
    )
    table = threshold_sensitivity(con, ("federal_gc",), [2017], thresholds=(50_000, 100_000, 250_000))
    counts = dict(zip(table.threshold, table.total_treated_n))
    assert counts[50_000] >= counts[100_000] >= counts[250_000]
    assert counts[50_000] == 2
    assert counts[100_000] == 1
    assert counts[250_000] == 0
