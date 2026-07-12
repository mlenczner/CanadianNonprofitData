"""Tests for analysis/org_page.py. Uses a small fixture DuckDB, never the
real 1.6GB nonprofit_network.duckdb (except the one skipif integration test)."""

import html.parser
import os
import re
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analysis import org_page as op

REAL_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nonprofit_network.duckdb")


def make_fixture_db(path):
    con = duckdb.connect(path)

    con.execute("""
        CREATE TABLE entities (
            entity_id INTEGER, bn_root VARCHAR, canonical_name VARCHAR,
            city VARCHAR, province VARCHAR, entity_kind VARCHAR
        )
    """)
    con.executemany("INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?)", [
        (1, "123456789", "Test Charity Inc", "Toronto", "ON", "charity"),
        (2, None, "Fuzzy Match Org", "Vancouver", "BC", "charity"),
        (3, "987654321", "Global Affairs Canada", None, None, "federal_dept"),
        (4, None, "Bilingual Org|Org Bilingue", "Montreal", "QC", "charity"),
        (5, None, "Big Regranter Foundation", None, "ON", "funder_org"),
        (6, "111111111", "Large Foundation", "Calgary", "AB", "charity"),
        (7, None, "Canada Council for the Arts", None, None, "funder_org"),
        (8, None, "Scale Cap Test Org", "Halifax", "NS", "charity"),
        (9, None, "Test Charity Two", "Ottawa", "ON", "charity"),
    ])

    con.execute("""
        CREATE TABLE entity_links (
            entity_id INTEGER, source_dataset VARCHAR, raw_name VARCHAR,
            raw_bn VARCHAR, match_method VARCHAR, match_score DOUBLE
        )
    """)
    con.executemany("INSERT INTO entity_links VALUES (?, ?, ?, ?, ?, ?)", [
        (1, "federal_gc", "Test Charity Inc", "123456789", "exact_bn", None),
        (2, "canada_council", "Fuzzy Match Organization Inc", None, "fuzzy_accept", 92.4),
        (2, "t3010_qualified_donee", "Fuzzy Match Org", None, "exact_bn", None),
        (3, "federal_gc", "Global Affairs Canada", "987654321", "exact_bn", None),
        (4, "canada_council", "Bilingual Org|Org Bilingue", None, "unmatched_new", None),
        (5, "canada_council", "Big Regranter Foundation", None, "exact_bn", None),
        (6, "t3010_qualified_donee", "Large Foundation", "111111111", "exact_bn", None),
        (7, "canada_council", "Canada Council for the Arts", None, "exact_bn", None),
        (8, "canada_council", "Scale Cap Test Org", None, "exact_bn", None),
    ])

    con.execute("""
        CREATE TABLE grants_unified (
            grant_id INTEGER, source_dataset VARCHAR, funder_entity_id INTEGER,
            recipient_entity_id INTEGER, amount_cad DOUBLE, fiscal_year INTEGER,
            program_name VARCHAR, description VARCHAR
        )
    """)
    grants = [
        (1, "federal_gc", 3, 1, 1200000.00, 2019, "Foo Program", "desc"),
        (2, "t3010_qualified_donee", 6, 2, 50000.00, 2021, None, None),
        (3, "canada_council", 7, 4, 15000.00, 2022, "Grant to Artists", None),
    ]
    N_SCALE = 305
    for i in range(N_SCALE):
        grants.append((
            100 + i, "canada_council", 5, 8, float(300000 - i * 100), 2015 + (i % 10),
            f"Regrant #{i}", None,
        ))
    con.executemany("INSERT INTO grants_unified VALUES (?, ?, ?, ?, ?, ?, ?, ?)", grants)

    con.execute("""
        CREATE TABLE entity_role_summary (
            entity_id INTEGER, canonical_name VARCHAR, entity_kind VARCHAR,
            total_given DOUBLE, total_received DOUBLE, n_grants_given INTEGER,
            n_grants_received INTEGER, given_share DOUBLE, role VARCHAR
        )
    """)
    scale_total = sum(float(300000 - i * 100) for i in range(N_SCALE))
    con.executemany("INSERT INTO entity_role_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        (1, "Test Charity Inc", "charity", 0, 1200000.00, 0, 1, 0.0, "primarily_recipient"),
        (2, "Fuzzy Match Org", "charity", 0, 50000.00, 0, 1, 0.0, "primarily_recipient"),
        (3, "Global Affairs Canada", "federal_dept", 1200000.00, 0, 1, 0, 1.0, "primarily_funder"),
        (4, "Bilingual Org|Org Bilingue", "charity", 0, 15000.00, 0, 1, 0.0, "primarily_recipient"),
        (5, "Big Regranter Foundation", "funder_org", scale_total, 0, N_SCALE, 0, 1.0, "primarily_funder"),
        (6, "Large Foundation", "charity", 50000.00, 0, 1, 0, 1.0, "primarily_funder"),
        (7, "Canada Council for the Arts", "funder_org", 15000.00, 0, 1, 0, 1.0, "primarily_funder"),
        (8, "Scale Cap Test Org", "charity", 0, scale_total, 0, N_SCALE, 0.0, "primarily_recipient"),
        (9, "Test Charity Two", "charity", 0, 0, 0, 0, None, "no_flows"),
    ])

    con.execute("""
        CREATE TABLE entity_financials (
            entity_id INTEGER, bn_full VARCHAR, fiscal_period_end DATE,
            total_revenue DOUBLE, total_expenditures DOUBLE,
            total_expenditures_incl_disbursements DOUBLE,
            total_gifts_to_qualified_donees DOUBLE,
            revenue_from_federal_gov DOUBLE, revenue_from_any_cdn_gov DOUBLE
        )
    """)
    con.execute("""
        INSERT INTO entity_financials VALUES
        (1, '123456789RR0001', '2022-12-31', 5000000, 4800000, 4800000, 100000, 1200000, 1200000)
    """)

    # Full amendment history for the one federal grant with a chain.
    con.execute("""
        CREATE TABLE raw_grants (
            owner_org VARCHAR, ref_number VARCHAR, amendment_number VARCHAR,
            agreement_value VARCHAR, recipient_legal_name VARCHAR,
            recipient_business_number VARCHAR, agreement_start_date VARCHAR,
            agreement_end_date VARCHAR, description_en VARCHAR, prog_name_en VARCHAR
        )
    """)
    con.executemany("INSERT INTO raw_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        ("Global Affairs Canada", "GC-2019-Q2-00001", "0", "1000000", "Test Charity Inc",
         "123456789", "2019-06-01", "2020-03-31", "desc v0", "Foo Program"),
        ("Global Affairs Canada", "GC-2019-Q2-00001", "1", "1400000", "Test Charity Inc",
         "123456789", "2019-06-01", "2020-03-31", "desc v1", "Foo Program"),
        ("Global Affairs Canada", "GC-2019-Q2-00001", "2", "1200000", "Test Charity Inc",
         "123456789", "2019-06-01", "2020-03-31", "desc", "Foo Program"),
    ])
    con.execute("""
        CREATE TABLE raw_grants_latest AS
        SELECT * FROM raw_grants WHERE amendment_number = '2'
    """)

    con.execute("""
        CREATE TABLE raw_t3010_qd (
            BN VARCHAR, FPE VARCHAR, "Donee BN" VARCHAR, "Donee Name" VARCHAR,
            City VARCHAR, Province VARCHAR, "Total Gifts" VARCHAR, source_year INTEGER
        )
    """)
    con.execute("""
        INSERT INTO raw_t3010_qd VALUES
        ('111111111RR0001', '2021-12-31', NULL, 'Fuzzy Match Org', 'Vancouver', 'BC', '50000', 2021)
    """)

    con.close()


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "fixture.duckdb")
    make_fixture_db(path)
    return path


class TagCollector(html.parser.HTMLParser):
    """Minimal well-formedness check: html.parser chokes loudly on badly
    broken markup, which is enough to catch unresolved template tokens
    corrupting tag structure."""
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


def assert_parses_cleanly(page_html):
    parser = TagCollector()
    parser.feed(page_html)
    assert "html" in parser.tags and "body" in parser.tags
    # No leftover template placeholders / format-string leakage (CSS legitimately
    # has adjacent braces from nested rules, so check for actual token markers).
    assert not re.search(r"%\([a-zA-Z_]+\)s", page_html)
    assert "Traceback" not in page_html
    assert "<html" in page_html and page_html.strip().startswith("<!DOCTYPE html>")


class DrawerVisibilityChecker(html.parser.HTMLParser):
    """A drawer nested inside an inline display:none ancestor can never be
    shown, no matter what class JS later toggles onto it -- display:none on
    an ancestor always wins. This walks the actual tag nesting (not just
    substring-searching the HTML) to catch that, plus verifies every claim's
    data-drawer id resolves to exactly one (non-hidden) drawer element."""
    VOID_TAGS = {"meta", "link", "br", "img", "input", "hr"}

    def __init__(self):
        super().__init__()
        self.hidden_stack = []  # bool per open tag: does *this* tag have inline display:none
        self.hidden_depth = 0
        self.drawer_ids_seen = []
        self.hidden_drawer_ids = []
        self.claim_drawer_refs = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        style = d.get("style", "") or ""
        is_hidden = re.search(r"display\s*:\s*none", style) is not None
        classes = (d.get("class") or "").split()
        if "drawer" in classes:
            self.drawer_ids_seen.append(d.get("id"))
            if self.hidden_depth > 0:
                self.hidden_drawer_ids.append(d.get("id"))
        if "claim" in classes and d.get("data-drawer"):
            self.claim_drawer_refs.append(d["data-drawer"])
        if tag in self.VOID_TAGS:
            return
        if is_hidden:
            self.hidden_depth += 1
        self.hidden_stack.append(is_hidden)

    def handle_endtag(self, tag):
        if tag in self.VOID_TAGS or not self.hidden_stack:
            return
        if self.hidden_stack.pop():
            self.hidden_depth -= 1


def assert_drawers_are_reachable(page_html):
    checker = DrawerVisibilityChecker()
    checker.feed(page_html)
    assert not checker.hidden_drawer_ids, (
        f"drawer(s) nested inside an inline display:none ancestor -- can never "
        f"open regardless of the 'open' class: {checker.hidden_drawer_ids}"
    )
    counts = {}
    for did in checker.drawer_ids_seen:
        counts[did] = counts.get(did, 0) + 1
    for ref in checker.claim_drawer_refs:
        assert counts.get(ref) == 1, (
            f"claim references data-drawer={ref!r}, which resolves to "
            f"{counts.get(ref, 0)} drawer element(s) instead of exactly 1"
        )


# ── name lookup ──────────────────────────────────────────────────────────────

def test_exact_name_hit_builds(db_path, tmp_path, capsys):
    out = tmp_path / "out.html"
    op.main(["Test Charity Inc", "--db", db_path, "--out", str(out)])
    assert out.exists()
    captured = capsys.readouterr()
    assert "Wrote" in captured.out


def test_ambiguous_prefix_exits_nonzero_and_lists_candidates(db_path, capsys):
    with pytest.raises(SystemExit) as exc_info:
        op.main(["Test Charity", "--db", db_path])
    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "entity_id=1" in err or "entity_id=9" in err
    assert "Test Charity Inc" in err
    assert "Test Charity Two" in err


def test_list_flag_prints_and_exits_zero_without_building(db_path, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc_info:
        op.main(["Test Charity", "--db", db_path, "--list"])
    assert exc_info.value.code == 0
    assert not (tmp_path / "docs").exists()


# ── page content ─────────────────────────────────────────────────────────────

def test_page_parses_and_has_canonical_name_and_totals(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1)
    finally:
        con.close()
    assert_parses_cleanly(page)
    assert_drawers_are_reachable(page)
    assert "Test Charity Inc" in page
    assert op.fmt_money(1200000.00) in page  # total received, formatted


def test_fuzzy_linked_receipt_contains_raw_name_and_score(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 2)
    finally:
        con.close()
    assert_parses_cleanly(page)
    assert "Fuzzy Match Organization Inc" in page
    assert "92.4" in page


def test_amendment_chain_receipt_shows_all_three_values_in_order(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1)
    finally:
        con.close()
    # Scope to the amendment-chain drawer itself -- an unrelated stat (e.g. a
    # revenue figure) could coincidentally contain one of these substrings
    # elsewhere on the page, so don't assert ordering against the full page.
    m = re.search(r"<p><b>Amendment chain</b>.*?</p>", page)
    assert m is not None, "amendment chain receipt not found in page"
    chain_html = m.group(0)
    v0, v1, v2 = op.fmt_money(1000000), op.fmt_money(1400000), op.fmt_money(1200000)
    i0, i1, i2 = chain_html.find(v0), chain_html.find(v1), chain_html.find(v2)
    assert i0 != -1 and i1 != -1 and i2 != -1
    assert i0 < i1 < i2


def test_bilingual_name_english_in_header_full_string_in_receipt(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 4)
    finally:
        con.close()
    header = page[: page.find("</header>")]
    assert "Bilingual Org" in header
    assert "Org Bilingue" not in header
    assert "Bilingual Org|Org Bilingue" in page


def test_scale_cap_embeds_300_rows_and_rollup_note(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 8)
    finally:
        con.close()
    # "Regrant #" also appears once per row inside its receipt drawer, so count
    # table rows specifically (one <td class='num'> per grants-table row, none
    # inside a drawer) rather than raw substring occurrences of "Regrant #".
    assert page.count("<td class='num'>") == op.SCALE_CAP
    assert "300 largest of 305" in page
    assert "5 grants" in page or "remaining 5" in page
    assert_drawers_are_reachable(page)


def test_drawers_not_trapped_in_hidden_wrapper(db_path):
    # Regression test: an earlier version wrapped every drawer in
    # <div style="display:none">, which permanently hides all of them --
    # toggling the .open class on a descendant can never override a
    # display:none ancestor, so every claim click was a silent no-op. Cover
    # every claim shape: identity (h1), stat (div), and grant-row (table).
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1)  # identity + stat + amendment-chain claims
    finally:
        con.close()
    assert_drawers_are_reachable(page)
    assert 'style="display:none"' not in page and "style='display:none'" not in page


def test_zero_grants_given_section_omitted(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1)
    finally:
        con.close()
    assert "Grants given" not in page


# ── helper functions ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (1_200_000_000, "$1.2B"),
    (15_000_000_000, "$15B"),
    (1_400_000, "$1.4M"),
    (50_000, "$50,000"),
    (None, "—"),
])
def test_fmt_money(value, expected):
    assert op.fmt_money(value) == expected


def test_slugify():
    assert op.slugify("The Salvation Army") == "the-salvation-army"
    assert op.slugify("Prince Rupert Port Authority") == "prince-rupert-port-authority"
    assert op.slugify("A & B / C") == "a-b-c"


def test_english_name():
    assert op.english_name("English Name|Nom français") == "English Name"
    assert op.english_name("No Pipe Here") == "No Pipe Here"
    assert op.english_name(None) == ""


# ── integration (real DB) ────────────────────────────────────────────────────

@pytest.mark.skipif(not os.path.exists(REAL_DB_PATH), reason="real database not built")
def test_integration_builds_top_entity_page(tmp_path):
    con = duckdb.connect(REAL_DB_PATH, read_only=True)
    try:
        top = con.execute("""
            SELECT entity_id FROM entity_role_summary
            ORDER BY COALESCE(total_given, 0) + COALESCE(total_received, 0) DESC LIMIT 1
        """).fetchone()
        entity_id = top[0]
        page = op.render_page(con, entity_id)
    finally:
        con.close()
    assert_parses_cleanly(page)
