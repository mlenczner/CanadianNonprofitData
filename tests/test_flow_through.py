"""
Tests for analysis/flow_through.py (Part B of
docs/crowding-out-and-flow-through-spec.md).

Uses hand-crafted in-memory DuckDB fixtures, same pattern as
tests/test_t3010_qd_dedup.py / tests/test_crowding_out.py -- not a live
query against the real multi-GB database.

Run with:
    .venv/bin/python -m pytest tests/test_flow_through.py
"""

import os
import sys

import duckdb
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.flow_through import (
    flag_intermediaries,
    fetch_donee_gifts,
    build_chains,
)

QD_COLUMNS = (
    "BN", "FPE", '"Form ID"', '"#"', '"Donee BN"', '"Donee Name"', "Associated",
    "City", "Province", '"Total Gifts"', '"Gifts in Kind"',
    '"Political Activity Gift"', '"Political Activity Amount"',
    "filename", "source_year",
)


def _build_fixture(con, grants_rows, fin5050_rows, qd_rows, entities_rows):
    """grants_rows: (entity_id, source_dataset, fiscal_year, amount_cad).
    fin5050_rows: (BN, FPE, "5050", source_year).
    qd_rows: full raw_t3010_qd_dedup rows (see QD_COLUMNS).
    entities_rows: (entity_id, bn_root, province)."""
    con.execute("CREATE TABLE entities (entity_id INTEGER, bn_root VARCHAR, province VARCHAR)")
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

    con.execute('CREATE TABLE raw_t3010_fin (BN VARCHAR, FPE VARCHAR, "5050" VARCHAR, source_year INTEGER)')
    if fin5050_rows:
        con.executemany("INSERT INTO raw_t3010_fin VALUES (?, ?, ?, ?)", fin5050_rows)

    values_sql = ", ".join(
        "(" + ", ".join(f"'{v}'" if not isinstance(v, int) else str(v) for v in row) + ")"
        for row in qd_rows
    ) if qd_rows else None
    if values_sql:
        con.execute(f"""
            CREATE TABLE raw_t3010_qd_dedup AS SELECT * FROM (VALUES {values_sql})
            AS t({", ".join(QD_COLUMNS)})
        """)
    else:
        con.execute(f"""
            CREATE TABLE raw_t3010_qd_dedup (
                BN VARCHAR, FPE VARCHAR, "Form ID" VARCHAR, "#" VARCHAR,
                "Donee BN" VARCHAR, "Donee Name" VARCHAR, Associated VARCHAR,
                City VARCHAR, Province VARCHAR, "Total Gifts" VARCHAR,
                "Gifts in Kind" VARCHAR, "Political Activity Gift" VARCHAR,
                "Political Activity Amount" VARCHAR, filename VARCHAR, source_year INTEGER
            )
        """)


def _qd_row(bn, fpe, donee_bn, donee_name, total_gifts, source_year, num="1"):
    return (bn, fpe, "27", num, donee_bn, donee_name, "", "Toronto", "ON",
            str(total_gifts), "0", "0", "0", "qualified_donees.csv", source_year)


# ── Intermediary flagging / year overlap ────────────────────────────────

def test_intermediary_flagged_when_5050_overlaps_a_hop1_receipt_year():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[("federal_gc", 1, 2018, 500_000)],
        fin5050_rows=[("111111111", "2018-12-31", "200000", 2018)],
        qd_rows=[],
        entities_rows=[(1, "111111111", "ON")],
    )
    interm = flag_intermediaries(con, ("federal_gc",), 100_000)
    assert list(interm.bn_root) == ["111111111"]
    assert int(interm.iloc[0].fiscal_year) == 2018


def test_intermediary_not_flagged_when_5050_year_does_not_overlap_hop1_receipt():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[("federal_gc", 2, 2015, 500_000)],  # hop-1 receipt only in 2015
        fin5050_rows=[("222222222", "2020-12-31", "200000", 2020)],  # big regrant only in 2020, no overlap
        qd_rows=[],
        entities_rows=[(2, "222222222", "ON")],
    )
    interm = flag_intermediaries(con, ("federal_gc",), 100_000)
    assert len(interm) == 0, "a hop-2-shaped gift with no overlapping hop-1 year must not trigger the flag"


def test_intermediary_not_flagged_when_5050_below_threshold():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[("federal_gc", 3, 2018, 500_000)],
        fin5050_rows=[("333333333", "2018-12-31", "50000", 2018)],  # below $100k threshold
        qd_rows=[],
        entities_rows=[(3, "333333333", "ON")],
    )
    interm = flag_intermediaries(con, ("federal_gc",), 100_000)
    assert len(interm) == 0


# ── Donee BN resolution ───────────────────────────────────────────────────

def test_donee_bn_resolves_to_entity_id_via_bn_root():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[],
        fin5050_rows=[],
        qd_rows=[_qd_row("444444444", "2024-12-31", "555555555RR0001", "Some Donee Charity", 60000, 2024)],
        entities_rows=[(99, "555555555", "ON")],  # the donee itself resolves to an entity
    )
    gifts = fetch_donee_gifts(con, ["444444444"])
    assert len(gifts) == 1
    row = gifts.iloc[0]
    assert row.donee_bn_root == "555555555"
    assert row.donee_entity_id == 99


def test_donee_with_unresolvable_bn_still_appears_with_null_entity_id():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[],
        fin5050_rows=[],
        qd_rows=[_qd_row("444444444", "2024-12-31", "", "Unregistered Donee", 60000, 2024)],
        entities_rows=[],
    )
    gifts = fetch_donee_gifts(con, ["444444444"])
    assert len(gifts) == 1
    assert gifts.iloc[0].donee_bn_root is None
    assert pd.isna(gifts.iloc[0].donee_entity_id)


# ── Chain traversal / cycle detection ────────────────────────────────────

def test_cycle_a_regrants_to_b_regrants_to_a_is_flagged_and_traversal_stops():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[("federal_gc", 1, 2018, 500_000)],  # only A receives hop-1 money
        fin5050_rows=[
            ("111111111", "2018-12-31", "200000", 2018),  # A is an intermediary in 2018
            ("222222222", "2018-12-31", "150000", 2018),  # B is also an intermediary in 2018 (via its own gifts)
        ],
        qd_rows=[
            _qd_row("111111111", "2018-12-31", "222222222RR0001", "Org B", 150000, 2018, num="1"),
            _qd_row("222222222", "2018-12-31", "111111111RR0001", "Org A", 100000, 2018, num="2"),
        ],
        entities_rows=[(1, "111111111", "ON"), (2, "222222222", "ON")],
    )
    chains = build_chains(con, ("federal_gc",), 100_000, max_hop_depth=2)
    a_to_b = chains[(chains.intermediary_bn_root == "111111111") & (chains.donee_bn_root == "222222222")]
    b_to_a = chains[(chains.intermediary_bn_root == "222222222") & (chains.donee_bn_root == "111111111")]
    assert len(a_to_b) == 1 and not a_to_b.iloc[0].is_cycle
    assert len(b_to_a) == 1 and b_to_a.iloc[0].is_cycle, "B regranting back to A closes a cycle and must be flagged"
    # traversal must terminate -- no depth-3 edge re-entering A's own donees again
    assert len(chains) == 2


def test_traversal_capped_at_max_hop_depth():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[("federal_gc", 1, 2018, 500_000)],
        fin5050_rows=[
            ("111111111", "2018-12-31", "200000", 2018),
            ("222222222", "2018-12-31", "150000", 2018),
            ("333333333", "2018-12-31", "120000", 2018),
        ],
        qd_rows=[
            _qd_row("111111111", "2018-12-31", "222222222RR0001", "Org B", 150000, 2018, num="1"),
            _qd_row("222222222", "2018-12-31", "333333333RR0001", "Org C", 120000, 2018, num="2"),
            _qd_row("333333333", "2018-12-31", "444444444RR0001", "Org D", 50000, 2018, num="3"),
        ],
        entities_rows=[(1, "111111111", "ON"), (2, "222222222", "ON"), (3, "333333333", "ON")],
    )
    chains = build_chains(con, ("federal_gc",), 100_000, max_hop_depth=2)
    depths = sorted(chains.hop_depth.tolist())
    assert depths == [1, 2], \
        "with max_hop_depth=2, the A->B edge (depth 1) and B->C edge (depth 2) should appear, but not C->D (depth 3)"


# ── Aga Khan-shaped regression fixture ───────────────────────────────────

def test_aga_khan_shaped_fixture_produces_three_named_donee_edges():
    con = duckdb.connect(":memory:")
    _build_fixture(
        con,
        grants_rows=[("federal_gc", 1, 2024, 20_000_000)],
        fin5050_rows=[("119219814", "2024-12-31", "53400000", 2024)],
        qd_rows=[
            _qd_row("119219814", "2024-12-31", "134693465RR0001", "Aga Khan Foundation", 51_400_000, 2024, num="1"),
            _qd_row("119219814", "2024-12-31", "119395978RR0001", "FOCUS Humanitarian Assistance", 1_400_000, 2024, num="2"),
            _qd_row("119219814", "2024-12-31", "129374549RR0001", "Aga Khan Museum", 600_000, 2024, num="3"),
        ],
        entities_rows=[(1, "119219814", "ON")],
    )
    chains = build_chains(con, ("federal_gc",), 100_000, max_hop_depth=2)
    assert len(chains) == 3
    by_name = {r.donee_raw_name: r.hop2_amount for r in chains.itertuples()}
    assert by_name["Aga Khan Foundation"] == 51_400_000
    assert by_name["FOCUS Humanitarian Assistance"] == 1_400_000
    assert by_name["Aga Khan Museum"] == 600_000
    assert all(chains.hop_depth == 1)
    assert not chains.is_cycle.any()
