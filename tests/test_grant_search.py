"""Tests for analysis/grant_search.py. Uses a small fixture DuckDB, never the
real ~1.6GB nonprofit_network.duckdb (except the one skipif integration test)."""

import html.parser
import os
import re
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analysis import grant_search as gs

REAL_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nonprofit_network.duckdb")


def make_fixture_db(path):
    con = duckdb.connect(path)
    con.execute("""
        CREATE TABLE entities (entity_id INTEGER, bn_root VARCHAR, canonical_name VARCHAR,
                                city VARCHAR, province VARCHAR, entity_kind VARCHAR)
    """)
    con.executemany("INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?)", [
        (1, None, "Department X", None, None, "federal_dept"),
        (2, None, "Recipient A", "Toronto", "ON", "charity"),
        (3, None, "Recipient B", "Vancouver", "BC", "charity"),
        (4, None, "OTF", None, "ON", "funder_org"),
        (5, None, "Recipient C", "Montreal", "QC", "charity"),
    ])
    con.execute("""
        CREATE TABLE grants_unified (
            grant_id INTEGER, source_dataset VARCHAR, funder_entity_id INTEGER,
            recipient_entity_id INTEGER, amount_cad DOUBLE, fiscal_year INTEGER,
            program_name VARCHAR, description VARCHAR, source_ref VARCHAR
        )
    """)
    grants = [
        # Same normalized text, raw whitespace differs (double space vs single)
        # -- must collapse into ONE distinct-text group, not two.
        (1, "federal_gc", 1, 2, 100000.0, 2022, "Youth Program", "Program for  Youth", None),
        (2, "federal_gc", 1, 3, 50000.0, 2023, "Youth Program", "Program for Youth", None),
        # A genuinely different text. Carries a real source_ref -- exercises
        # C1's per-row open.canada.ca link.
        (3, "federal_gc", 1, 2, 20000.0, 2022, "Seniors Program", "Support for seniors housing",
         "dept-x|GC-2022-Q1-001"),
        # otf source, also indexable.
        (4, "otf", 4, 5, 5000.0, 2021, "Community Grant", "Local community centre funding", None),
        # No description -- must be excluded from grant-text search entirely.
        (5, "federal_gc", 1, 2, 1000.0, 2020, "No Text Program", None, None),
        # t3010_qualified_donee -- not a GRANT_TEXT_SOURCES source, must be excluded
        # even though it has a program_name (real data: this is always a constant
        # label with no description, per the module docstring).
        (6, "t3010_qualified_donee", 2, 3, 7000.0, 2022, "Qualified donee gift", None, None),
    ]
    # 35 grants sharing one text, to exercise the VISIBLE_ROWS/SCALE_CAP collapse.
    for i in range(35):
        grants.append((100 + i, "federal_gc", 1, 2, float(1000 - i), 2019 + (i % 3),
                        "Big Program", "A widely shared program description", None))
    con.executemany("INSERT INTO grants_unified VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", grants)
    con.close()


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "grant_fixture.duckdb")
    make_fixture_db(path)
    return path


class TagCollector(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


def assert_parses_cleanly(page_html):
    parser = TagCollector()
    parser.feed(page_html)
    assert "html" in parser.tags and "body" in parser.tags
    assert not re.search(r"%\([a-zA-Z_]+\)s", page_html)
    assert "Traceback" not in page_html
    assert "<html" in page_html and page_html.strip().startswith("<!DOCTYPE html>")


# ── fetch_distinct_grant_texts / normalization dedup ─────────────────────────

def test_whitespace_variants_collapse_into_one_distinct_text(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
    finally:
        con.close()
    youth = [r for r in records if r["program_name"] == "Youth Program"]
    assert len(youth) == 1
    assert youth[0]["n"] == 2
    assert youth[0]["total_amount"] == 150000.0


def test_null_description_and_non_text_sources_excluded(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
        total = gs.count_distinct_grant_texts(con)
    finally:
        con.close()
    program_names = {r["program_name"] for r in records}
    assert "No Text Program" not in program_names
    assert "Qualified donee gift" not in program_names
    # Youth (1 merged), Seniors, Community Grant, Big Program = 4 distinct texts.
    assert total == 4
    assert len(records) == 4


def test_otf_source_included(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
    finally:
        con.close()
    assert any(r["source_dataset"] == "otf" for r in records)


def test_limit_caps_returned_records(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con, limit=2)
    finally:
        con.close()
    assert len(records) == 2
    # Ordered by total_amount descending.
    assert records[0]["total_amount"] >= records[1]["total_amount"]


# ── fetch_grants_for_text / hash consistency ─────────────────────────────────

def test_fetch_grants_for_text_matches_whitespace_variants(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
        youth = next(r for r in records if r["program_name"] == "Youth Program")
        grants = gs.fetch_grants_for_text(con, youth["text_hash"][:16])
    finally:
        con.close()
    assert len(grants) == 2
    assert {g["amount_cad"] for g in grants} == {100000.0, 50000.0}


def test_index_and_bulk_fetch_use_identical_hash(db_path):
    """Regression test for the real bug found in this module: an earlier
    version grouped fetch_distinct_grant_texts by raw (unnormalized) text
    while text_hash was computed from normalized text, so some index records'
    hashes had zero matches when looked up via the per-text or bulk-grouped
    fetch -- confirmed on the real corpus as 347 of 40,000 requested detail
    pages silently not being written. Every record's hash must resolve."""
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
        grouped = gs.fetch_all_grants_grouped_by_hash(con)
    finally:
        con.close()
    for r in records:
        h = r["text_hash"][:16]
        assert h in grouped, f"index record {r['program_name']!r} hash not found in bulk-grouped fetch"
        assert len(grouped[h]) == r["n"]


# ── rendering ─────────────────────────────────────────────────────────────────

def test_render_grant_index_page_parses_and_has_filters(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
        total = gs.count_distinct_grant_texts(con)
    finally:
        con.close()
    page = gs.render_grant_index_page(records, total)
    assert_parses_cleanly(page)
    assert "data-src='federal_gc'" in page
    assert "data-src='otf'" in page
    assert "Youth Program" in page


def test_render_grant_index_page_shows_cap_note_when_capped(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con, limit=2)
        total = gs.count_distinct_grant_texts(con)
    finally:
        con.close()
    page = gs.render_grant_index_page(records, total)
    assert "Showing the top 2" in page
    assert f"{total}" in page


def test_render_grant_detail_page_has_stats_and_collapses_extra_rows(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
        big = next(r for r in records if r["program_name"] == "Big Program")
        page = gs.render_grant_detail_page(con, big["text_hash"][:16])
    finally:
        con.close()
    assert_parses_cleanly(page)
    assert "Big Program" in page
    assert "35" in page  # grant count stat
    assert "id='more-grants'" in page
    assert "Show 5 more grant" in page  # 35 - VISIBLE_ROWS(30) = 5


def test_render_grant_detail_page_no_extra_rows_when_under_visible_limit(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
        youth = next(r for r in records if r["program_name"] == "Youth Program")
        page = gs.render_grant_detail_page(con, youth["text_hash"][:16])
    finally:
        con.close()
    assert_parses_cleanly(page)
    assert "id='more-grants'" not in page  # the CSS rule for .more-rows is always present; only the element usage matters


def test_render_grant_detail_page_links_to_org_pages(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
        seniors = next(r for r in records if r["program_name"] == "Seniors Program")
        page = gs.render_grant_detail_page(con, seniors["text_hash"][:16])
    finally:
        con.close()
    assert "../orgs/department-x.html" in page
    assert "../orgs/recipient-a.html" in page


# ── live=True org-link fix (a real user-reported bug: org-name links from a
# live-served grant detail page used the static '.html' convention, which
# 404s against the live app's extensionless /orgs/<slug> routes) ──────────

def test_render_grant_detail_page_live_mode_drops_html_suffix(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
        seniors = next(r for r in records if r["program_name"] == "Seniors Program")
        page = gs.render_grant_detail_page(con, seniors["text_hash"][:16], live=True)
    finally:
        con.close()
    assert "../orgs/department-x'" in page or "../orgs/department-x\"" in page
    assert "../orgs/department-x.html" not in page
    assert "../orgs/recipient-a.html" not in page


def test_render_grant_detail_page_live_mode_uses_extensionless_footer_links(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
        seniors = next(r for r in records if r["program_name"] == "Seniors Program")
        page = gs.render_grant_detail_page(con, seniors["text_hash"][:16], live=True)
    finally:
        con.close()
    assert 'href="/grants"' in page
    assert 'href="/orgs"' in page
    assert 'href="index.html"' not in page
    assert 'href="../orgs/index.html"' not in page


def test_render_grant_list_table_static_mode_keeps_html_suffix_by_default():
    grants = [{"funder_name": "A", "recipient_name": "B", "amount_cad": 100.0, "fiscal_year": 2022,
               "source_ref": None}]
    table = gs.render_grant_list_table(grants)
    assert "../orgs/a.html" in table
    assert "../orgs/b.html" in table


def test_render_grant_list_table_live_mode_drops_html_suffix():
    grants = [{"funder_name": "A", "recipient_name": "B", "amount_cad": 100.0, "fiscal_year": 2022,
               "source_ref": None}]
    table = gs.render_grant_list_table(grants, live=True)
    assert "../orgs/a.html" not in table
    assert "../orgs/b.html" not in table
    assert "../orgs/a'" in table
    assert "../orgs/b'" in table


# ── C1: per-row open.canada.ca link ──────────────────────────────────────────

def test_render_grant_detail_page_row_links_to_open_canada_when_source_ref_present(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
        seniors = next(r for r in records if r["program_name"] == "Seniors Program")
        page = gs.render_grant_detail_page(con, seniors["text_hash"][:16])
    finally:
        con.close()
    from analysis.org_page import open_canada_record_url
    expected_url = open_canada_record_url("dept-x", "GC-2022-Q1-001")
    assert f"href='{expected_url}'" in page
    assert "View official record on open.canada.ca" in page


def test_render_grant_detail_page_row_omits_link_without_source_ref(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con)
        youth = next(r for r in records if r["program_name"] == "Youth Program")
        page = gs.render_grant_detail_page(con, youth["text_hash"][:16])
    finally:
        con.close()
    assert "open.canada.ca" not in page


def test_render_grant_list_table_link_only_on_rows_with_source_ref():
    grants = [
        {"funder_name": "A", "recipient_name": "B", "amount_cad": 100.0, "fiscal_year": 2022,
         "source_ref": "dept-x|GC-1"},
        {"funder_name": "A", "recipient_name": "B", "amount_cad": 200.0, "fiscal_year": 2023,
         "source_ref": None},
    ]
    table = gs.render_grant_list_table(grants)
    assert table.count("View official record on open.canada.ca") == 1


def test_render_grant_list_table_empty_input_returns_empty_string():
    assert gs.render_grant_list_table([]) == ""


def test_render_grant_detail_page_missing_hash_exits_nonzero(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        with pytest.raises(SystemExit) as exc_info:
            gs.render_grant_detail_page(con, "0000000000000000")
    finally:
        con.close()
    assert exc_info.value.code != 0


# ── batch build ────────────────────────────────────────────────────────────

def test_build_search_index_and_all_detail_pages_agree_on_count(db_path, tmp_path):
    out_dir = tmp_path / "grants"
    n_index, index_path = gs.build_search_index(db_path, out_dir=str(out_dir), limit=100)
    n_pages = gs.build_all_detail_pages(db_path, out_dir=str(out_dir), limit=100)
    assert n_index == 4  # matches test_null_description_and_non_text_sources_excluded
    assert n_pages == n_index
    assert os.path.exists(index_path)
    detail_files = [f for f in os.listdir(out_dir) if f.startswith("grant-")]
    assert len(detail_files) == n_index


# ── integration (real DB) ────────────────────────────────────────────────────

@pytest.mark.skipif(not os.path.exists(REAL_DB_PATH), reason="real database not built")
def test_integration_top_text_renders(tmp_path):
    con = duckdb.connect(REAL_DB_PATH, read_only=True)
    try:
        records = gs.fetch_distinct_grant_texts(con, limit=1)
        assert records
        page = gs.render_grant_detail_page(con, records[0]["text_hash"][:16])
    finally:
        con.close()
    assert_parses_cleanly(page)
