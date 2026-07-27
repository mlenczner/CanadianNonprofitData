"""
Regression test for a real double-counting bug in analysis/build_entity_graph.py:
raw_t3010_qd (the T3010 Schedule 6 "gifts to qualified donees" extract)
carries genuine full-row duplicate lines *within the same source file* --
confirmed against a real rebuild: CanadaHelps' 2024 qualified-donee schedule
has 4,499 duplicate donee-gift lines out of 35,989 rows (e.g. its
$3,224,402 gift to the Canadian Red Cross Society appeared on two different
line numbers, '#' 31480 and 35979, with every other field byte-identical) --
a chunk of one filer's schedule appended twice in CRA's own published CSV,
not an ingestion bug. Pattern repeats across all 12 years: 22,351 duplicate
groups / 62,067 duplicate rows on a full-row-except-'#' key.

Fixed with _dedup_t3010_qd_sql(): a QUALIFY/ROW_NUMBER dedup to one row per
(every column except '#'), lowest '#' wins deterministically -- same
non-destructive pattern as _latest_amendment_sql() (raw_t3010_qd itself
stays untouched; the qualified-donee build loop reads raw_t3010_qd_dedup
instead).

Run with:
    .venv/bin/python -m pytest tests/test_t3010_qd_dedup.py
"""

import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.build_entity_graph import _dedup_t3010_qd_sql


QD_COLUMNS = (
    "BN", "FPE", '"Form ID"', '"#"', '"Donee BN"', '"Donee Name"', "Associated",
    "City", "Province", '"Total Gifts"', '"Gifts in Kind"',
    '"Political Activity Gift"', '"Political Activity Amount"',
    "filename", "source_year",
)


def _make_qd_table(con, rows):
    values_sql = ", ".join(
        "(" + ", ".join(f"'{v}'" if not isinstance(v, int) else str(v) for v in row) + ")"
        for row in rows
    )
    con.execute(f"""
        CREATE TABLE raw_t3010_qd AS SELECT * FROM (VALUES {values_sql})
        AS t({", ".join(QD_COLUMNS)})
    """)


def test_exact_full_row_duplicate_differing_only_in_line_number_is_collapsed():
    con = duckdb.connect(":memory:")
    # CanadaHelps -> Red Cross, same gift reported on two different lines.
    row = ("896568417RR0001", "2024-06-30", "27", "000031480", "119219814RR0001",
           "THE CANADIAN RED CROSS SOCIETY", "", "Toronto", "ON", "3224402", "0",
           "0", "0", "qualified_donees_2024.csv", 2024)
    dup_row = row[:3] + ("000035979",) + row[4:]
    _make_qd_table(con, [row, dup_row])
    con.execute(f"CREATE TABLE dedup AS {_dedup_t3010_qd_sql()}")
    rows = con.execute('SELECT "#" FROM dedup').fetchall()
    assert rows == [("000031480",)], "the lower '#' line should survive, the duplicate should be dropped"


def test_near_duplicate_differing_in_gifts_in_kind_is_not_collapsed():
    con = duckdb.connect(":memory:")
    row = ("896568417RR0001", "2024-06-30", "27", "000031480", "119219814RR0001",
           "THE CANADIAN RED CROSS SOCIETY", "", "Toronto", "ON", "3224402", "0",
           "0", "0", "qualified_donees_2024.csv", 2024)
    # Same everything except Gifts in Kind -- a genuinely different line, must survive.
    different_row = row[:10] + ("500",) + row[11:]
    _make_qd_table(con, [row, different_row])
    con.execute(f"CREATE TABLE dedup AS {_dedup_t3010_qd_sql()}")
    n = con.execute("SELECT COUNT(*) FROM dedup").fetchone()[0]
    assert n == 2, "rows differing in Gifts in Kind are not true duplicates and must both survive"


def test_dedup_does_not_touch_raw_table():
    con = duckdb.connect(":memory:")
    row = ("896568417RR0001", "2024-06-30", "27", "000031480", "119219814RR0001",
           "THE CANADIAN RED CROSS SOCIETY", "", "Toronto", "ON", "3224402", "0",
           "0", "0", "qualified_donees_2024.csv", 2024)
    dup_row = row[:3] + ("000035979",) + row[4:]
    _make_qd_table(con, [row, dup_row])
    con.execute(f"CREATE TABLE dedup AS {_dedup_t3010_qd_sql()}")
    n_raw = con.execute("SELECT COUNT(*) FROM raw_t3010_qd").fetchone()[0]
    assert n_raw == 2, "raw_t3010_qd must stay untouched -- dedup is a derived table only"


def test_dedup_key_spans_multiple_filers_independently():
    con = duckdb.connect(":memory:")
    row_a = ("896568417RR0001", "2024-06-30", "27", "1", "119219814RR0001",
              "THE CANADIAN RED CROSS SOCIETY", "", "Toronto", "ON", "3224402", "0",
              "0", "0", "qualified_donees_2024.csv", 2024)
    dup_a = row_a[:3] + ("2",) + row_a[4:]
    row_b = ("111111111RR0001", "2024-06-30", "9", "3", "222222222RR0001",
              "SOME OTHER CHARITY", "", "Ottawa", "ON", "500", "0",
              "0", "0", "qualified_donees_2024.csv", 2024)
    _make_qd_table(con, [row_a, dup_a, row_b])
    con.execute(f"CREATE TABLE dedup AS {_dedup_t3010_qd_sql()}")
    n = con.execute("SELECT COUNT(*) FROM dedup").fetchone()[0]
    assert n == 2, "one duplicate collapsed for filer A, filer B's single row untouched"
