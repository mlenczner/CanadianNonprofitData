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
            city VARCHAR, province VARCHAR, entity_kind VARCHAR,
            bn_full VARCHAR, search_name VARCHAR
        )
    """)
    con.executemany("INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        (1, "123456789", "Test Charity Inc", "Toronto", "ON", "charity", "123456789RR0001", "test charity inc"),
        (2, None, "Fuzzy Match Org", "Vancouver", "BC", "charity", None, "fuzzy match org"),
        (3, "987654321", "Global Affairs Canada", None, None, "federal_dept", None, "global affairs canada"),
        (4, None, "Bilingual Org|Org Bilingue", "Montreal", "QC", "charity", None, "bilingual org|org bilingue"),
        (5, None, "Big Regranter Foundation", None, "ON", "funder_org", None, "big regranter foundation"),
        (6, "111111111", "Large Foundation", "Calgary", "AB", "charity", "111111111RR0001", "large foundation"),
        (7, None, "Canada Council for the Arts", None, None, "funder_org", None, "canada council for the arts"),
        (8, None, "Scale Cap Test Org", "Halifax", "NS", "charity", None, "scale cap test org"),
        (9, None, "Test Charity Two", "Ottawa", "ON", "charity", None, "test charity two"),
        (10, "222222222", "Multi Year Grant Charity", "Winnipeg", "MB", "charity", "222222222RR0001", "multi year grant charity"),
        (11, None, "École Polytechnique", "Montreal", "QC", "charity", None, "ecole polytechnique"),
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
        (10, "federal_gc", "Multi Year Grant Charity", "222222222", "exact_bn", None),
    ])

    con.execute("""
        CREATE TABLE grants_unified (
            grant_id INTEGER, source_dataset VARCHAR, funder_entity_id INTEGER,
            recipient_entity_id INTEGER, amount_cad DOUBLE, fiscal_year INTEGER,
            program_name VARCHAR, description VARCHAR, source_ref VARCHAR
        )
    """)
    grants = [
        # source_ref left NULL here on purpose -- exercises the pre-issue-#4
        # best-effort fallback path in locate_federal_receipt() (see
        # test_amendment_chain_receipt_shows_all_three_values_in_order).
        (1, "federal_gc", 3, 1, 1200000.00, 2019, "Foo Program", "desc", None),
        (2, "t3010_qualified_donee", 6, 2, 50000.00, 2021, None, None, None),
        (3, "canada_council", 7, 4, 15000.00, 2022, "Grant to Artists", None, None),
        # Entity 10: a genuinely multi-year federal_gc agreement (2019-06-01
        # to 2021-05-31, spanning FY2019/FY2020/FY2021 under month_cutover=4)
        # -- every other fixture federal_gc row is single-fiscal-year, so
        # this is the only row exercising prorate_agreement_by_fiscal_year
        # through fetch_federal_gc_prorated_by_year. grants_unified still
        # attributes the naive (unprorated) fiscal_year=2019, matching real
        # build_entity_graph.py output today. FY2022's canada_council row has
        # no entity_financials_by_year coverage (see below), so that year
        # exercises the old-style fallback within the same entity's chart.
        (11, "federal_gc", 3, 10, 1200000.00, 2019, "Multi Year Program", "multi-year desc",
         "Global Affairs Canada|GC-2019-MULTI-00001"),
        (12, "canada_council", 7, 10, 50000.00, 2022, "Some Grant", None, None),
    ]
    N_SCALE = 305
    for i in range(N_SCALE):
        grants.append((
            100 + i, "canada_council", 5, 8, float(300000 - i * 100), 2015 + (i % 10),
            f"Regrant #{i}", None, None,
        ))
    con.executemany("INSERT INTO grants_unified VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", grants)

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
        (10, "Multi Year Grant Charity", "charity", 0, 1250000.00, 0, 2, 0.0, "primarily_recipient"),
        (11, "École Polytechnique", "charity", 0, 75000.00, 0, 1, 0.0, "primarily_recipient"),
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
        # Entity 10's multi-year agreement -- amendment_number '2' so it's
        # picked up by the raw_grants_latest filter below, same as the chain
        # above (no amendment history of its own needed for this one).
        ("Global Affairs Canada", "GC-2019-MULTI-00001", "2", "1200000", "Multi Year Grant Charity",
         "222222222", "2019-06-01", "2021-05-31", "multi-year desc", "Multi Year Program"),
    ])
    con.execute("""
        CREATE TABLE raw_grants_latest AS
        SELECT * FROM raw_grants WHERE amendment_number = '2'
    """)

    con.execute("""
        CREATE TABLE entity_financials_by_year (
            entity_id INTEGER, fiscal_period_end DATE, fiscal_year INTEGER,
            total_revenue DOUBLE, gov_revenue DOUBLE, foundation_revenue DOUBLE
        )
    """)
    con.executemany("INSERT INTO entity_financials_by_year VALUES (?, ?, ?, ?, ?, ?)", [
        # FY2019: declared 300000, identified (prorated federal share) 400000 -- 133%.
        (10, "2019-12-31", 2019, 500000, 300000, None),
        # FY2020: declared 500000, identified (prorated federal share) 400000 -- 80%.
        # No grants_unified row naively attributes fiscal_year=2020 to entity
        # 10 at all -- this year only has a bar because pro-rating surfaces
        # money the naive (unprorated) attribution would have missed entirely.
        (10, "2020-12-31", 2020, 500000, 500000, None),
        # Deliberately no FY2021 row (the agreement's third spanned year) and
        # no FY2022 row (entity 10's canada_council grant year) -- FY2022
        # exercises the old-style fallback; FY2021 exercises a prorated
        # amount landing on a year that's out of scope for the chart
        # entirely (see test_multi_year_charity_mixes_new_and_old_style_bars).
    ])

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
    # locate_t3010_qd_receipt() reads raw_t3010_qd_dedup, not raw_t3010_qd
    # directly (A2's T3010 dedup fix) -- no intentional duplicates in this
    # fixture, so the deduped table is just a copy.
    con.execute("CREATE TABLE raw_t3010_qd_dedup AS SELECT * FROM raw_t3010_qd")

    con.close()


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "fixture.duckdb")
    make_fixture_db(path)
    return path


VOID_TAGS = {"meta", "link", "br", "img", "input", "hr"}


class TagCollector(html.parser.HTMLParser):
    """Well-formedness check covering two failure modes: html.parser chokes
    loudly on badly broken markup (catches unresolved template tokens), and
    this also tracks a real open-tag stack (void-tag-aware) to catch
    mismatched tags html.parser itself stays silent about -- regression test
    for a real bug: a missing </h1> in render_page's header caused a stray
    </div> later to close <div class="wrap"> early, silently un-nesting
    every section after the header (stats, timeline, grants tables, drawers,
    footer) from .wrap's padded container. Every existing test here already
    called assert_parses_cleanly and none caught it, because "html"/"body"
    appearing somewhere in the tag list said nothing about whether the tree
    was actually balanced."""
    def __init__(self):
        super().__init__()
        self.tags = []
        self.stack = []
        self.mismatches = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.mismatches.append((tag, self.getpos(), list(self.stack)))


def assert_parses_cleanly(page_html):
    parser = TagCollector()
    parser.feed(page_html)
    assert "html" in parser.tags and "body" in parser.tags
    # No leftover template placeholders / format-string leakage (CSS legitimately
    # has adjacent braces from nested rules, so check for actual token markers).
    assert not re.search(r"%\([a-zA-Z_]+\)s", page_html)
    assert "Traceback" not in page_html
    assert "<html" in page_html and page_html.strip().startswith("<!DOCTYPE html>")
    assert not parser.mismatches, f"mismatched/unbalanced tags: {parser.mismatches[:3]}"
    assert not parser.stack, f"unclosed tags at end of document: {parser.stack}"


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


def test_page_contains_draft_disclaimer(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1)
    finally:
        con.close()
    assert "[DRAFT]" in page  # <title> prefix
    assert op.DRAFT_BANNER_TEXT in page  # sticky banner
    assert op.DRAFT_FULL_TEXT in page  # full canonical text in the footer
    assert "draft-watermark" in page  # screenshot-proofing watermark


# ── discovery badge: confirmed non-charity nonprofit identity (REQ / ────────
# Corporations Canada), see analysis.org_page.load_discovery_index() ────────

def test_discovery_match_shows_badge_and_reachable_drawer(db_path):
    discovery_index = {
        1: {"discovery_source": "req", "legal_name": "Test Charity Inc",
            "jurisdiction": "QC", "matched_grant_entity_name": "Test Charity Inc"},
    }
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1, discovery_index=discovery_index)
    finally:
        con.close()
    assert_parses_cleanly(page)
    assert_drawers_are_reachable(page)
    assert "Confirmed Quebec nonprofit" in page
    assert "Registre des entreprises du Qu" in page  # drawer explanation


def test_corporations_canada_source_gets_its_own_badge_text(db_path):
    discovery_index = {
        1: {"discovery_source": "corporations_canada", "legal_name": "Test Charity Inc",
            "jurisdiction": "ON", "matched_grant_entity_name": "Test Charity Inc"},
    }
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1, discovery_index=discovery_index)
    finally:
        con.close()
    assert "Confirmed federally-incorporated nonprofit" in page
    assert "Corporations Canada" in page


def test_no_discovery_index_omits_badge_entirely(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1)  # discovery_index defaults to None
    finally:
        con.close()
    assert "drawer-discovery" not in page
    assert "not a registered charity" not in page


def test_discovery_index_present_but_entity_not_in_it_omits_badge(db_path):
    discovery_index = {999999: {"discovery_source": "req", "legal_name": "Someone Else",
                                 "jurisdiction": "QC", "matched_grant_entity_name": "Someone Else"}}
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1, discovery_index=discovery_index)
    finally:
        con.close()
    assert "drawer-discovery" not in page


def _write_discovery_csv(path, rows):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


DISCOVERY_ROW_DEFAULTS = {
    "source_id": "1", "jurisdiction": "QC", "discovery_source": "req", "legal_name": "Org",
    "trade_names": "", "address": "", "postal": "", "city": "", "legal_form": "",
    "charity_status": "non_charity_nonprofit", "matched_bn": "", "matched_cra_name": "",
    "charity_match_score": "", "charity_runner_up_score": "", "charity_match_method": "",
    "social_status": "", "social_signal": "", "social_match_score": "",
    "federal_grant_status": "federal_grant_match", "matched_grant_entity_id": "42",
    "matched_grant_entity_name": "Org", "federal_grant_match_score": "100.0",
    "federal_grant_runner_up_score": "", "federal_grants_received": "3", "federal_dollars_received": "5000.0",
    "review_flag": "", "discovery_snapshot_date": "", "cra_snapshot_date": "", "grants_snapshot_date": "",
}


def test_load_discovery_index_reads_both_source_files(tmp_path):
    req_row = dict(DISCOVERY_ROW_DEFAULTS, source_id="1", discovery_source="req",
                   legal_name="Quebec Org", matched_grant_entity_id="42")
    cc_row = dict(DISCOVERY_ROW_DEFAULTS, source_id="2", discovery_source="corporations_canada",
                  legal_name="Federal Org", matched_grant_entity_id="43")
    _write_discovery_csv(tmp_path / "quebec_discovery_flagged.csv", [req_row])
    _write_discovery_csv(tmp_path / "corporations_canada_discovery_flagged.csv", [cc_row])

    index = op.load_discovery_index(str(tmp_path))
    assert index[42]["discovery_source"] == "req"
    assert index[42]["legal_name"] == "Quebec Org"
    assert index[42]["discovery_sources"] == {"req"}
    assert index[43]["discovery_source"] == "corporations_canada"
    assert index[43]["legal_name"] == "Federal Org"
    assert index[43]["discovery_sources"] == {"corporations_canada"}


def test_load_discovery_index_entity_confirmed_by_both_sources_is_not_dropped(tmp_path):
    # Real, confirmed case: 235 entities independently matched by both REQ
    # and Corporations Canada. Neither source should silently overwrite the
    # other -- both must be recorded, and the entity counted once, not twice.
    req_row = dict(DISCOVERY_ROW_DEFAULTS, source_id="1", discovery_source="req",
                   legal_name="Quebec Name", matched_grant_entity_id="42")
    cc_row = dict(DISCOVERY_ROW_DEFAULTS, source_id="2", discovery_source="corporations_canada",
                  legal_name="Federal Name", matched_grant_entity_id="42")
    _write_discovery_csv(tmp_path / "quebec_discovery_flagged.csv", [req_row])
    _write_discovery_csv(tmp_path / "corporations_canada_discovery_flagged.csv", [cc_row])

    index = op.load_discovery_index(str(tmp_path))
    assert len(index) == 1  # one entity, not two
    assert index[42]["discovery_sources"] == {"req", "corporations_canada"}
    assert index[42]["discovery_source"] == "req"  # first-confirmed wins for the single-source badge


def test_load_discovery_index_excludes_needs_review_and_no_match(tmp_path):
    rows = [
        dict(DISCOVERY_ROW_DEFAULTS, source_id="1", matched_grant_entity_id="42",
             federal_grant_status="federal_grant_match"),
        dict(DISCOVERY_ROW_DEFAULTS, source_id="2", matched_grant_entity_id="43",
             federal_grant_status="needs_review"),
        dict(DISCOVERY_ROW_DEFAULTS, source_id="3", matched_grant_entity_id="44",
             federal_grant_status="no_match", matched_grant_entity_name=""),
    ]
    _write_discovery_csv(tmp_path / "quebec_discovery_flagged.csv", rows)
    index = op.load_discovery_index(str(tmp_path))
    assert list(index.keys()) == [42]


def test_load_discovery_index_excludes_registered_charity_rows(tmp_path):
    # A registered_charity row's entity already shows "Registered charity" via
    # KIND_LABELS -- see load_discovery_index()'s docstring for why this
    # first pass doesn't also badge that case.
    row = dict(DISCOVERY_ROW_DEFAULTS, charity_status="registered_charity", matched_grant_entity_id="42")
    _write_discovery_csv(tmp_path / "quebec_discovery_flagged.csv", [row])
    index = op.load_discovery_index(str(tmp_path))
    assert index == {}


def test_load_discovery_index_missing_files_returns_empty_dict_not_error(tmp_path):
    empty_dir = tmp_path / "nonexistent"
    assert op.load_discovery_index(str(empty_dir)) == {}


# ── fetch_discovery_summary: aggregate stats for /hidden-nonprofits ─────────

def test_fetch_discovery_summary_empty_index_returns_zero_state(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        summary = op.fetch_discovery_summary(con, {})
    finally:
        con.close()
    assert summary == {"total_orgs": 0, "total_dollars": 0.0, "by_source": {}, "top_examples": []}


def test_fetch_discovery_summary_computes_totals_live_not_from_csv(db_path):
    # entity 1's total_received (1,200,000) comes from entity_role_summary,
    # not from any value stored in discovery_index -- confirms the summary
    # is computed live against the database, not trusted from the CSV.
    discovery_index = {
        1: {"discovery_source": "req", "discovery_sources": {"req"}, "legal_name": "Test Charity Inc",
            "jurisdiction": "QC", "matched_grant_entity_name": "Test Charity Inc"},
    }
    con = duckdb.connect(db_path, read_only=True)
    try:
        summary = op.fetch_discovery_summary(con, discovery_index)
    finally:
        con.close()
    assert summary["total_orgs"] == 1
    assert summary["total_dollars"] == 1200000.00
    assert summary["by_source"] == {"req": {"count": 1, "dollars": 1200000.00}}
    assert summary["top_examples"][0]["canonical_name"] == "Test Charity Inc"


def test_fetch_discovery_summary_splits_by_source_and_ranks_examples(db_path):
    discovery_index = {
        1: {"discovery_source": "req", "discovery_sources": {"req"}, "legal_name": "Test Charity Inc",
            "jurisdiction": "QC", "matched_grant_entity_name": "Test Charity Inc"},
        6: {"discovery_source": "corporations_canada", "discovery_sources": {"corporations_canada"},
            "legal_name": "Large Foundation", "jurisdiction": "AB",
            "matched_grant_entity_name": "Large Foundation"},
    }
    con = duckdb.connect(db_path, read_only=True)
    try:
        summary = op.fetch_discovery_summary(con, discovery_index, top_n=1)
    finally:
        con.close()
    assert set(summary["by_source"].keys()) == {"req", "corporations_canada"}
    # entity 1 (1,200,000) outranks entity 6 (0 received, it's primarily_funder) -- top_n=1 keeps just it.
    assert len(summary["top_examples"]) == 1
    assert summary["top_examples"][0]["canonical_name"] == "Test Charity Inc"


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


# ── Part C: official-source deep links ──────────────────────────────────────

def test_open_canada_record_url_uses_three_segment_pattern_with_current_suffix():
    # Confirmed against a real browser session (2026-07-18): the spec's own
    # two-segment guess ({owner_org},{ref_number}) loads an empty page shell
    # with no data -- the real pattern needs a trailing ",current" segment.
    url = op.open_canada_record_url("wd-deo", "GC-WD-DEO-2021-2022-Q1-704")
    assert url == "https://search.open.canada.ca/grants/record/wd-deo,GC-WD-DEO-2021-2022-Q1-704,current"


def test_open_canada_record_url_encodes_spaces_and_slashes_in_ref_number():
    url = op.open_canada_record_url("dept a", "REF/1 2")
    assert url == "https://search.open.canada.ca/grants/record/dept%20a,REF%2F1%202,current"


def test_open_canada_record_url_returns_none_without_ref_number():
    assert op.open_canada_record_url("wd-deo", None) is None
    assert op.open_canada_record_url(None, "GC-1") is None


def test_cra_charity_url_uses_full_bn():
    url = op.cra_charity_url("119219814RR0001")
    assert url == ("https://apps.cra-arc.gc.ca/ebci/hacc/srch/pub/dsplyBscInf"
                    "?selectedCharityBn=119219814RR0001&dsrdPg=1")


def test_cra_charity_url_returns_none_without_bn_full():
    assert op.cra_charity_url(None) is None
    assert op.cra_charity_url("") is None


def test_corporations_canada_url_matches_confirmed_working_pattern():
    # Confirmed against a real record (2026-07-18): corpId=456926 loaded
    # The Huntsman Marine Science Centre's real federal corporation page.
    url = op.corporations_canada_url(456926)
    assert url == "https://ised-isde.canada.ca/cc/lgcy/fdrlCrpDtls.html?corpId=456926"


def test_corporations_canada_url_returns_none_without_corp_number():
    assert op.corporations_canada_url(None) is None


def test_c1_federal_receipt_drawer_links_open_canada(db_path):
    # grant_id=11 (entity 3 -> entity 10) carries a real source_ref in the
    # fixture: "Global Affairs Canada|GC-2019-MULTI-00001".
    con = duckdb.connect(db_path, read_only=True)
    try:
        grant = {
            "grant_id": 11, "fiscal_year": 2019, "other_entity_id": 3,
            "program_name": "Multi Year Program", "description": "multi-year desc",
            "amount_cad": 1200000.00, "source_dataset": "federal_gc",
            "source_ref": "Global Affairs Canada|GC-2019-MULTI-00001",
        }
        drawer = op.render_grant_receipt(con, 10, grant, "received")
    finally:
        con.close()
    expected_url = op.open_canada_record_url("Global Affairs Canada", "GC-2019-MULTI-00001")
    assert f"href='{op.esc(expected_url)}'" in drawer
    assert "View official record on open.canada.ca" in drawer
    assert "class='ext'" in drawer


def test_c1_federal_receipt_no_link_without_source_ref(db_path):
    # An amount/year with no matching raw_grants row at all (not just a
    # missing source_ref -- entity 1's real grant_id=1 amount/year DOES
    # resolve via the best-effort fallback, since that row genuinely exists
    # in raw_grants_latest, so that's not a "no link" case). This exercises
    # locate_federal_receipt()'s found=False path, which render_grant_receipt
    # returns from before the official-link code ever runs.
    con = duckdb.connect(db_path, read_only=True)
    try:
        grant = {
            "grant_id": 999, "fiscal_year": 2019, "other_entity_id": 3,
            "program_name": "Foo Program", "description": "desc",
            "amount_cad": 999.00, "source_dataset": "federal_gc",
            "source_ref": None,
        }
        drawer = op.render_grant_receipt(con, 1, grant, "received")
    finally:
        con.close()
    assert "Receipt not located" in drawer
    assert "open.canada.ca" not in drawer


def test_c2_cra_link_shown_in_org_page_header_when_bn_full_present(db_path):
    # entity 1's fixture bn_full is "123456789RR0001". The direct
    # apps.cra-arc.gc.ca record URL is confirmed broken (reproduced against
    # a real user's own browser session) -- the header shows the BN plus a
    # link to the CRA's general List of Charities page instead.
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1)
    finally:
        con.close()
    header = page[: page.find("</header>")]
    assert "123456789RR0001" in header
    assert "look up in the CRA List of Charities" in header
    assert op.CRA_CHARITY_SEARCH_URL in header
    assert "apps.cra-arc.gc.ca" not in header  # the confirmed-broken direct link must not be used


def test_c2_identity_receipt_includes_cra_link_when_bn_full_present():
    links = [{"raw_name": "Test Charity Inc", "source_dataset": "federal_gc", "raw_bn": "123456789",
              "match_method": "exact_bn", "match_score": None, "n_occurrences": 1}]
    html = op.render_identity_receipt(links, bn_full="123456789RR0001")
    assert "look up in the CRA List of Charities" in html
    assert "123456789RR0001" in html
    assert "class='ext'" in html


def test_c2_identity_receipt_omits_cra_link_without_bn_full():
    links = [{"raw_name": "No BN Org", "source_dataset": "federal_gc", "raw_bn": None,
              "match_method": "unmatched_new", "match_score": None, "n_occurrences": 1}]
    html = op.render_identity_receipt(links, bn_full=None)
    assert "look up in the CRA List of Charities" not in html


def test_render_cra_link_html_returns_empty_string_without_bn():
    assert op.render_cra_link_html(None) == ""


def test_render_cra_link_html_uses_custom_label():
    html = op.render_cra_link_html("123456789RR0001", label="Funder's BN")
    assert html.startswith(op.esc("Funder's BN") + " <code>123456789RR0001</code>")
    assert "apps.cra-arc.gc.ca" not in html
    assert op.CRA_CHARITY_SEARCH_URL in html


def test_c2_t3010_qualified_donee_receipt_links_funders_cra_listing_end_to_end(db_path):
    # "the filing is the funder's, so link the funder's CRA listing, not the
    # recipient's" (render_grant_receipt's own comment) -- only
    # render_cra_link_html() in isolation was tested before this; nothing
    # exercised render_grant_receipt's actual funder-bn_full lookup. Grant_id
    # 2: entity 6 "Large Foundation" (funder, bn_full 111111111RR0001) gave
    # to entity 2 "Fuzzy Match Org" (recipient, bn_full=None).
    con = duckdb.connect(db_path, read_only=True)
    try:
        grants = op.fetch_grants(con, 2, "received")
        grant = next(g for g in grants if g["source_dataset"] == "t3010_qualified_donee")
        assert grant["other_entity_id"] == 6
        drawer = op.render_grant_receipt(con, 2, grant, "received")
    finally:
        con.close()
    assert op.esc("Funder's BN") in drawer
    assert "111111111RR0001" in drawer
    assert op.CRA_CHARITY_SEARCH_URL in drawer


def test_c2_t3010_qualified_donee_receipt_omits_cra_link_when_funder_has_no_bn_full():
    # Same branch, opposite case: the funder itself has no bn_full on file
    # (e.g. no identification-schedule row) -- render_cra_link_html()
    # returns "" and render_grant_receipt must not append an empty <p
    # class='ext-line'> or any CRA-listing text.
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE entities (entity_id INTEGER, bn_root VARCHAR, canonical_name VARCHAR,
                                city VARCHAR, province VARCHAR, entity_kind VARCHAR, bn_full VARCHAR)
    """)
    con.executemany("INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?)", [
        (1, "555555555", "No-BN-Full Funder", "Regina", "SK", "charity", None),
        (2, None, "Recipient Org", "Regina", "SK", "charity", None),
    ])
    con.execute("""
        CREATE TABLE entity_links (entity_id INTEGER, source_dataset VARCHAR, raw_name VARCHAR)
    """)
    con.execute("INSERT INTO entity_links VALUES (2, 't3010_qualified_donee', 'Recipient Org')")
    con.execute("""
        CREATE TABLE raw_t3010_qd_dedup (BN VARCHAR, FPE VARCHAR, "Donee Name" VARCHAR, "Total Gifts" VARCHAR)
    """)
    con.execute("INSERT INTO raw_t3010_qd_dedup VALUES ('555555555RR0001', '2022-12-31', 'Recipient Org', '1000')")

    grant = {"source_dataset": "t3010_qualified_donee", "other_entity_id": 1,
              "amount_cad": 1000.0, "fiscal_year": 2022}
    drawer = op.render_grant_receipt(con, 2, grant, "received")
    assert "class='ext-line'" not in drawer
    assert "CRA List of Charities" not in drawer


def test_locate_t3010_qd_receipt_resolves_cleanly_through_the_dedup_table_despite_raw_duplicates():
    # A2's whole point: raw_t3010_qd carries genuine full-row duplicate lines
    # within the same source file (see tests/test_t3010_qd_dedup.py). Every
    # other locate_t3010_qd_receipt test's fixture copies raw_t3010_qd to
    # raw_t3010_qd_dedup 1:1 with zero duplicates, so none of them actually
    # exercise the failure mode the dedup table exists to prevent: if
    # locate_t3010_qd_receipt() were still reading the undeduped
    # raw_t3010_qd (or if raw_t3010_qd_dedup's own dedup ever silently
    # stopped happening upstream), this exact receipt would report "2 raw
    # rows matched ... not unambiguous" instead of resolving -- AGENTS.md
    # documents this as the precise regression the dedup fix has to guard
    # against for org_page.py's own receipt drawer, not just build-time totals.
    from analysis.build_entity_graph import _dedup_t3010_qd_sql

    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE entities (entity_id INTEGER, bn_root VARCHAR, canonical_name VARCHAR,
                                city VARCHAR, province VARCHAR, entity_kind VARCHAR, bn_full VARCHAR)
    """)
    con.executemany("INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?)", [
        (1, "896568417", "CanadaHelps", "Toronto", "ON", "charity", "896568417RR0001"),
        (2, "119219814", "Canadian Red Cross Society", "Toronto", "ON", "charity", "119219814RR0001"),
    ])
    con.execute("CREATE TABLE entity_links (entity_id INTEGER, source_dataset VARCHAR, raw_name VARCHAR)")
    con.execute(
        "INSERT INTO entity_links VALUES (2, 't3010_qualified_donee', 'THE CANADIAN RED CROSS SOCIETY')"
    )
    con.execute("""
        CREATE TABLE raw_t3010_qd (
            BN VARCHAR, FPE VARCHAR, "Form ID" VARCHAR, "#" VARCHAR, "Donee BN" VARCHAR,
            "Donee Name" VARCHAR, Associated VARCHAR, City VARCHAR, Province VARCHAR,
            "Total Gifts" VARCHAR, "Gifts in Kind" VARCHAR, "Political Activity Gift" VARCHAR,
            "Political Activity Amount" VARCHAR, filename VARCHAR, source_year INTEGER
        )
    """)
    # The real CanadaHelps -> Red Cross duplicate from AGENTS.md issue #6:
    # same gift reported on two different line numbers, every other field
    # byte-identical.
    row = ("896568417RR0001", "2024-06-30", "27", "000031480", "119219814RR0001",
           "THE CANADIAN RED CROSS SOCIETY", "", "Toronto", "ON", "3224402", "0",
           "0", "0", "qualified_donees_2024.csv", 2024)
    dup_row = row[:3] + ("000035979",) + row[4:]
    con.executemany(f"INSERT INTO raw_t3010_qd VALUES ({', '.join('?' for _ in row)})", [row, dup_row])
    con.execute(f"CREATE TABLE raw_t3010_qd_dedup AS {_dedup_t3010_qd_sql()}")

    # Sanity check: the raw (undeduped) table genuinely has 2 matching rows
    # for this filer+donee -- confirms this fixture actually reproduces the
    # ambiguity, not a fixture that was never ambiguous to begin with.
    raw_matches = con.execute("""
        SELECT COUNT(*) FROM raw_t3010_qd
        WHERE substr(regexp_replace(BN, '[^0-9A-Za-z]', '', 'g'), 1, 9) = '896568417'
          AND "Donee Name" = 'THE CANADIAN RED CROSS SOCIETY'
    """).fetchone()[0]
    assert raw_matches == 2, "fixture setup error: this test needs a genuine raw-table ambiguity to prove anything"

    r = op.locate_t3010_qd_receipt(con, entity_id=2, funder_entity_id=1, amount=3224402.0, fiscal_year=2024)
    assert r["found"] is True, f"receipt should resolve cleanly via the deduped table, got: {r}"
    assert r["fpe"] == "2024-06-30"


def test_c5_department_page_gets_open_canada_search_link(db_path):
    # entity 3 "Global Affairs Canada" is entity_kind='federal_dept'.
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 3)
        owner_org = op.fetch_department_owner_org(con, 3)
    finally:
        con.close()
    assert owner_org == "Global Affairs Canada"  # derived from grant_id=11's source_ref
    header = page[: page.find("</header>")]
    assert "All records from this department on open.canada.ca" in header
    # "Global Affairs Canada" (the fixture's raw owner_org text) isn't a real
    # owner_org slug, so it's not a DEPARTMENT_LINKS key -- no homepage link.
    assert "homepage" not in header


def test_c5_department_links_dict_uses_real_department_names():
    # Spot-check a few entries against the real top-30-by-record-count query
    # run against nonprofit_network.duckdb -- confirms the dict wasn't
    # accidentally keyed on something else (e.g. canonical_name fragments).
    assert op.DEPARTMENT_LINKS["esdc-edsc"][0] == "Employment and Social Development Canada"
    assert op.DEPARTMENT_LINKS["pch"][0] == "Canadian Heritage"
    for owner_org, (name, url) in op.DEPARTMENT_LINKS.items():
        assert url.startswith("https://")
        assert name


def test_c3_discovery_badge_shows_neq_and_copy_button(db_path):
    discovery_index = {1: {"discovery_source": "req", "discovery_sources": {"req"},
                            "legal_name": "Test Charity Inc", "jurisdiction": "QC",
                            "matched_grant_entity_name": "Test Charity Inc", "source_id": "1142769836"}}
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1, discovery_index=discovery_index)
    finally:
        con.close()
    assert "NEQ" in page
    assert "1142769836" in page
    assert "copyToClipboard" in page
    assert op.REQ_SEARCH_URL in page


def test_c4_discovery_badge_shows_corp_number_and_link(db_path):
    discovery_index = {1: {"discovery_source": "corporations_canada", "discovery_sources": {"corporations_canada"},
                            "legal_name": "Test Charity Inc", "jurisdiction": "ON",
                            "matched_grant_entity_name": "Test Charity Inc", "source_id": "456926"}}
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1, discovery_index=discovery_index)
    finally:
        con.close()
    expected_url = op.corporations_canada_url("456926")
    assert f"href='{op.esc(expected_url)}'" in page
    assert "Corporation number" in page
    assert "456926" in page


def test_discovery_badge_with_no_source_id_omits_official_link(db_path):
    # Older/hand-constructed discovery_index dicts (e.g. some tests) may not
    # carry source_id -- must not KeyError.
    discovery_index = {1: {"discovery_source": "req", "discovery_sources": {"req"},
                            "legal_name": "Test Charity Inc", "jurisdiction": "QC",
                            "matched_grant_entity_name": "Test Charity Inc"}}
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1, discovery_index=discovery_index)
    finally:
        con.close()
    # Scope to the discovery drawer itself -- "NEQ" also appears in this
    # page's embedded JS comments (copyToClipboard's docstring), unrelated
    # to whether the badge's official-link line rendered.
    m = re.search(r"<div class='drawer' id='drawer-discovery'>.*?</div>", page, re.DOTALL)
    assert m is not None, "discovery drawer not found in page"
    assert "NEQ" not in m.group(0)


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


# ── funding timeline: declared vs identified ─────────────────────────────────

def test_fetch_federal_gc_prorated_by_year_splits_across_fiscal_years(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        by_year = op.fetch_federal_gc_prorated_by_year(con, 10)
    finally:
        con.close()
    assert by_year == {2019: 400_000.0, 2020: 400_000.0, 2021: 400_000.0}


def test_fetch_federal_gc_prorated_by_year_falls_back_without_source_ref(db_path):
    # Entity 1's federal grant has source_ref=None in the fixture (see its
    # comment -- exercises the pre-issue-#4 fallback path elsewhere too), so
    # this must fall back to grants_unified's own stored amount/fiscal_year
    # rather than trying to prorate (there's no raw row to prorate from).
    con = duckdb.connect(db_path, read_only=True)
    try:
        by_year = op.fetch_federal_gc_prorated_by_year(con, 1)
    finally:
        con.close()
    assert by_year == {2019: 1_200_000.0}


def test_multi_year_charity_mixes_new_and_old_style_bars(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 10)
    finally:
        con.close()
    assert_parses_cleanly(page)

    # FY2019 and FY2020: T3010 coverage present -> declared/identified bars.
    # FY2020 in particular has NO grants_unified row naively attributing
    # fiscal_year=2020 to this entity at all -- its bar exists only because
    # pro-rating surfaced a share of the multi-year federal agreement there.
    assert "government declared $300,000 &middot; identified $400,000 (133%)" in page
    assert "government declared $500,000 &middot; identified $400,000 (80%)" in page

    # FY2021 (the agreement's third spanned year) has no entity_financials_by_year
    # row and no grants_unified row naively landing there either -- correctly
    # absent from the chart entirely, not shown as an empty/zero column.
    assert "FY2021" not in page

    # FY2022: a real received grant (canada_council) but no T3010 filing on
    # record for that year -- falls back to the old plain received bar using
    # the naive (unprorated) grants_unified total for that year, not the
    # declared/identified comparison.
    assert "FY2022: received $50,000" in page
    assert "class='bar-recv'" in page


def test_non_charity_entity_never_gets_declared_identified_bars(db_path):
    # Entity 5 (funder_org) has real "given" chart activity (the scale-cap
    # regrants) but is not a charity and has no T3010 filings -- must render
    # with only the original plain bars, regardless of activity volume.
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 5)
    finally:
        con.close()
    assert_parses_cleanly(page)
    assert "class='bar-given'" in page
    # Scoped to actual bar markup (class='bar-gov-declared'), not the always-
    # present static CSS rule of the same name in the page's <style> block.
    assert "class='bar-gov-declared'" not in page
    assert "class='bar-fdn-declared'" not in page


def test_charity_with_no_t3010_filing_falls_back_to_plain_bars(db_path):
    # Entity 1 is a charity but has no entity_financials_by_year row in the
    # fixture at all (only entity_financials, the latest-year-only table) --
    # every year on its chart must fall back to the original plain bars.
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 1)
    finally:
        con.close()
    assert_parses_cleanly(page)
    assert "class='bar-gov-declared'" not in page
    assert "class='bar-fdn-declared'" not in page


# ── source_ref receipt disambiguation (AGENTS.md issue #4) ──────────────────
# Standalone/unit-level rather than through the shared fixture DB above:
# demonstrating the fix needs a genuine name+amount+fiscal-year collision
# across two different departments/refs, which the shared fixture doesn't
# have (and adding one there would require re-deriving several other tests'
# expected totals). locate_federal_receipt() is exercised directly instead.

def _make_collision_con():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE raw_grants_latest (
            owner_org VARCHAR, ref_number VARCHAR, recipient_legal_name VARCHAR,
            agreement_value VARCHAR, agreement_start_date VARCHAR,
            prog_name_en VARCHAR, description_en VARCHAR
        )
    """)
    con.executemany("INSERT INTO raw_grants_latest VALUES (?, ?, ?, ?, ?, ?, ?)", [
        ("Global Affairs Canada", "GC-2019-Q2-00001", "Test Charity Inc", "1200000",
         "2019-06-01", "Foo Program", "desc A"),
        ("Global Affairs Canada", "GC-2020-Q1-99999", "Test Charity Inc", "1200000",
         "2019-06-01", "Foo Program Two", "desc B"),
    ])
    con.execute("""
        CREATE TABLE raw_grants (
            owner_org VARCHAR, ref_number VARCHAR, amendment_number VARCHAR, agreement_value VARCHAR
        )
    """)
    con.executemany("INSERT INTO raw_grants VALUES (?, ?, ?, ?)", [
        ("Global Affairs Canada", "GC-2019-Q2-00001", "0", "1200000"),
        ("Global Affairs Canada", "GC-2020-Q1-99999", "0", "1200000"),
    ])
    con.execute("""
        CREATE TABLE entity_links (entity_id INTEGER, source_dataset VARCHAR, raw_name VARCHAR,
                                    raw_bn VARCHAR, match_method VARCHAR, match_score DOUBLE)
    """)
    con.execute("INSERT INTO entity_links VALUES (1, 'federal_gc', 'Test Charity Inc', NULL, 'exact_bn', NULL)")
    return con


def test_locate_federal_receipt_is_ambiguous_without_source_ref_on_a_real_collision():
    # Same recipient, same amount, same fiscal year, two different
    # departments/refs -- exactly the confirmed real-world collision shape
    # from AGENTS.md issue #3 (24,851 colliding ref_numbers). Without
    # source_ref this is genuinely unresolvable from name+amount+year alone.
    con = _make_collision_con()
    r = op.locate_federal_receipt(con, 1, 1200000.0, 2019)
    assert r["found"] is False
    assert "2 raw rows" in r["reason"]


def test_locate_federal_receipt_uses_source_ref_to_disambiguate_collision():
    con = _make_collision_con()
    r1 = op.locate_federal_receipt(con, 1, 1200000.0, 2019,
                                    source_ref="Global Affairs Canada|GC-2019-Q2-00001")
    assert r1["found"] is True
    assert r1["ref_number"] == "GC-2019-Q2-00001"
    assert r1["description"] == "desc A"

    r2 = op.locate_federal_receipt(con, 1, 1200000.0, 2019,
                                    source_ref="Global Affairs Canada|GC-2020-Q1-99999")
    assert r2["found"] is True
    assert r2["ref_number"] == "GC-2020-Q1-99999"
    assert r2["description"] == "desc B"


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


def test_lazy_receipts_each_grant_gets_a_distinct_drawer_id(db_path):
    # Regression test for a real reported bug: in lazy_receipts mode (the
    # live app), drawer_id was computed as f"drawer-{len(drawer_ids)}" --
    # but lazy rows never append to drawer_ids (the whole point: no eager
    # receipt query), so len(drawer_ids) stayed frozen for every lazy row,
    # and EVERY grant claim on the page carried the exact same data-drawer
    # value. In the browser, clicking any grant after the first just
    # re-toggled the first grant's already-cached drawer instead of
    # fetching its own receipt -- confirmed by a user, not just inferred.
    # Entity 8 has 300 embedded lazy claims (canada_council, one category),
    # more than enough to catch a collision if the fix regresses.
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 8, lazy_receipts=True)
    finally:
        con.close()
    drawer_ids_seen = re.findall(r"data-drawer='([^']+)'\s+data-lazy='1'", page)
    assert len(drawer_ids_seen) == op.SCALE_CAP
    assert len(set(drawer_ids_seen)) == len(drawer_ids_seen), (
        f"lazy grant claims share a data-drawer id -- {len(drawer_ids_seen) - len(set(drawer_ids_seen))} "
        f"duplicate(s) found, meaning clicking one grant would show a different grant's cached receipt"
    )


def test_lazy_receipts_distinct_ids_hold_across_multiple_categories(db_path):
    # entity 10 (Multi Year Grant Charity) has both a federal_gc grant and a
    # canada_council grant in "received" -- two different categories within
    # the same direction, sharing the same drawer_ids list across both
    # render_grants_table() calls. Confirms the fix holds across category
    # boundaries, not just within one table.
    con = duckdb.connect(db_path, read_only=True)
    try:
        page = op.render_page(con, 10, lazy_receipts=True)
    finally:
        con.close()
    drawer_ids_seen = re.findall(r"data-drawer='([^']+)'\s+data-lazy='1'", page)
    assert len(drawer_ids_seen) >= 2
    assert len(set(drawer_ids_seen)) == len(drawer_ids_seen)


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


@pytest.mark.parametrize("identified,declared,expected", [
    (50, 200, "25%"),
    (0, 200, "0%"),
    (50, None, "—"),
    (50, 0, "—"),  # declared falsy -- don't divide by zero, but see the >100% case below:
    (500, 200, "250%"),  # over-identification (e.g. a pro-rating-edge outcome) is shown, not hidden
])
def test_fmt_pct(identified, declared, expected):
    assert op.fmt_pct(identified, declared) == expected


def test_prorate_agreement_splits_evenly_across_spanned_years():
    # 2019-06-01 to 2021-05-31 spans FY2019/FY2020/FY2021 under the default
    # month_cutover=4 (a fiscal year starting April 1st) -- three full fiscal
    # years, so the value should split into three equal thirds.
    result = op.prorate_agreement_by_fiscal_year(1_200_000, "2019-06-01", "2021-05-31")
    assert result == {2019: 400_000, 2020: 400_000, 2021: 400_000}


def test_prorate_agreement_single_fiscal_year_span_is_unsplit():
    result = op.prorate_agreement_by_fiscal_year(1_200_000, "2019-06-01", "2020-01-15")
    assert result == {2019: 1_200_000}


def test_prorate_agreement_missing_end_date_falls_back_single_year():
    assert op.prorate_agreement_by_fiscal_year(1_200_000, "2019-06-01", None) == {2019: 1_200_000}
    assert op.prorate_agreement_by_fiscal_year(1_200_000, "2019-06-01", "not a date") == {2019: 1_200_000}


def test_prorate_agreement_end_before_start_falls_back_single_year():
    # A data-quality issue (bad source row), not something to crash or
    # produce a negative fiscal-year range over.
    result = op.prorate_agreement_by_fiscal_year(1_200_000, "2019-06-01", "2018-01-01")
    assert result == {2019: 1_200_000}


def test_prorate_agreement_missing_start_date_returns_empty():
    assert op.prorate_agreement_by_fiscal_year(1_200_000, None, "2021-05-31") == {}


def test_prorate_agreement_none_value_returns_empty():
    assert op.prorate_agreement_by_fiscal_year(None, "2019-06-01", "2021-05-31") == {}


# ── B5: timeline chart y-axis / tooltip accessibility / mobile labels ───────

def test_render_timeline_includes_y_axis_gridlines():
    html = op.render_timeline("charity", [2022, 2023], {2022: 100, 2023: 200}, {})
    assert html.count("class='gridline'") == 2
    assert "$200" in html  # max gridline label
    assert "$100" in html  # midpoint gridline label


def test_render_timeline_barcol_is_focusable():
    html = op.render_timeline("charity", [2022], {2022: 100}, {})
    assert "tabindex='0'" in html


def test_render_timeline_keeps_first_last_and_every_5th_year_label():
    years = list(range(2010, 2022))  # 12 years, indices 0-11
    html = op.render_timeline("charity", years, {y: 100 for y in years}, {})
    # first (2010, i=0), 2015 (i=5), 2020 (i=10), and last (2021, i=11) keep their label
    for y in (2010, 2015, 2020, 2021):
        assert f"class='barcol keep-label' tabindex='0'>" in html.split(f"<i>{y}</i>")[0][-120:], (
            f"year {y} should carry keep-label"
        )
    # a middle year not on the every-5th/first/last list must not carry it
    assert "class='barcol keep-label' tabindex='0'>" not in html.split("<i>2012</i>")[0][-80:]


def test_render_timeline_declared_identified_note_only_shown_when_relevant():
    no_t3010 = op.render_timeline("charity", [2022], {2022: 100}, {})
    assert "Declared = what the charity reported" not in no_t3010

    with_t3010 = op.render_timeline(
        "charity", [2022], {}, {},
        gov_declared_by_year={2022: 500}, gov_identified_by_year={2022: 300},
        fdn_declared_by_year={2022: 0}, fdn_identified_by_year={2022: 0},
    )
    assert "Declared = what the charity reported" in with_t3010


def test_render_timeline_empty_years_returns_empty_string():
    assert op.render_timeline("charity", [], {}, {}) == ""


# ── search index (fetch_batch_entities flags, render_index_page) ────────────
# Dedicated minimal fixture rather than the shared one above -- covers a
# t3010_non_qualified_donee grant in both directions, which the shared
# fixture doesn't have, without risking the totals/counts other tests assert
# against in entity_role_summary there.

def _make_search_fixture_db(path):
    con = duckdb.connect(path)
    con.execute("CREATE TABLE entities (entity_id INTEGER, bn_root VARCHAR, canonical_name VARCHAR, "
                 "city VARCHAR, province VARCHAR, entity_kind VARCHAR)")
    con.executemany("INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?)", [
        (1, None, "Red River Charity", "Winnipeg", "MB", "charity"),
        (2, None, "Non-Qualified Recipient Org", "Halifax", "NS", "charity"),
        (3, None, "Giving Foundation", "Toronto", "ON", "funder_org"),
        (4, None, "Government Department", None, None, "federal_dept"),
        (5, None, "No Flows Org", "Regina", "SK", "charity"),
    ])
    con.execute("CREATE TABLE grants_unified (grant_id INTEGER, source_dataset VARCHAR, "
                 "funder_entity_id INTEGER, recipient_entity_id INTEGER, amount_cad DOUBLE, "
                 "fiscal_year INTEGER, program_name VARCHAR, description VARCHAR, source_ref VARCHAR)")
    con.executemany("INSERT INTO grants_unified VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        # entity 1 receives from a government source and from a qualified donee
        (1, "federal_gc", 4, 1, 50000.0, 2022, None, None, None),
        (2, "t3010_qualified_donee", 3, 1, 10000.0, 2022, None, None, None),
        # entity 2 receives a non-qualified-donee gift from entity 3
        (3, "t3010_non_qualified_donee", 3, 2, 5000.0, 2022, None, None, None),
        # entity 3 gives qualified + non-qualified donee gifts (funder in both rows above)
    ])
    con.execute("CREATE TABLE entity_role_summary (entity_id INTEGER, canonical_name VARCHAR, "
                 "entity_kind VARCHAR, total_given DOUBLE, total_received DOUBLE, n_grants_given INTEGER, "
                 "n_grants_received INTEGER, given_share DOUBLE, role VARCHAR)")
    con.executemany("INSERT INTO entity_role_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        (1, "Red River Charity", "charity", 0, 60000.0, 0, 2, 0.0, "primarily_recipient"),
        (2, "Non-Qualified Recipient Org", "charity", 0, 5000.0, 0, 1, 0.0, "primarily_recipient"),
        (3, "Giving Foundation", "funder_org", 15000.0, 0, 2, 0, 1.0, "primarily_funder"),
        (4, "Government Department", "federal_dept", 50000.0, 0, 1, 0, 1.0, "primarily_funder"),
        (5, "No Flows Org", "charity", 0, 0, 0, 0, None, "no_flows"),
    ])
    con.close()


@pytest.fixture
def search_db_path(tmp_path):
    path = str(tmp_path / "search_fixture.duckdb")
    _make_search_fixture_db(path)
    return path


def test_fetch_batch_entities_computes_category_flags(search_db_path):
    con = duckdb.connect(search_db_path, read_only=True)
    try:
        batch = op.fetch_batch_entities(con)
    finally:
        con.close()
    by_id = {row["entity_id"]: row for row in batch}

    assert by_id[1]["recv_government"] is True
    assert by_id[1]["recv_qualified"] is True
    assert by_id[1]["recv_non_qualified"] is False

    assert by_id[2]["recv_non_qualified"] is True
    assert by_id[2]["recv_qualified"] is False
    assert by_id[2]["recv_government"] is False

    assert by_id[3]["given_qualified"] is True
    assert by_id[3]["given_non_qualified"] is True
    assert by_id[3]["given_government"] is False

    assert by_id[4]["given_government"] is True

    # role='no_flows' entities are excluded entirely, same as the existing
    # (unmodified) filter this query already applied.
    assert 5 not in by_id


def test_render_index_page_has_filter_checkboxes_for_every_category(search_db_path):
    con = duckdb.connect(search_db_path, read_only=True)
    try:
        batch = op.fetch_batch_entities(con)
    finally:
        con.close()
    manifest = op.build_link_manifest(batch)
    page = op.render_index_page(batch, manifest)
    assert_parses_cleanly(page)

    # One checkbox per SEARCH_FILTER_FIELDS entry, keyed the same way the
    # embedded JSON records are, so the JS filter's data-key lookup resolves.
    for key, _, _, _ in op.SEARCH_FILTER_FIELDS:
        assert f"data-key='{key}'" in page
    for direction, category in [("received", "qualified_donee"), ("received", "non_qualified_donee"),
                                 ("received", "government"), ("given", "qualified_donee"),
                                 ("given", "non_qualified_donee"), ("given", "government")]:
        assert op.esc(op.CATEGORY_HEADINGS[(direction, category)]) in page

    # Embedded record data carries the flags as real JSON booleans, not
    # Python-repr'd True/False (which would break the page's own JS parser).
    assert '"rg":true' in page or '"rg": true' in page
    assert "True" not in page and "False" not in page


def test_render_index_page_shows_plain_language_hint_under_every_category_checkbox(search_db_path):
    # B4: current labels ("As a non-qualified donee") are T3010 jargon --
    # CATEGORY_HINTS adds a muted one-line plain-language explanation under
    # each of the 6 category checkboxes. Never asserted anywhere before this.
    con = duckdb.connect(search_db_path, read_only=True)
    try:
        batch = op.fetch_batch_entities(con)
    finally:
        con.close()
    manifest = op.build_link_manifest(batch)
    page = op.render_index_page(batch, manifest)
    assert_parses_cleanly(page)
    for direction, category in [("received", "qualified_donee"), ("received", "non_qualified_donee"),
                                 ("received", "government"), ("given", "qualified_donee"),
                                 ("given", "non_qualified_donee"), ("given", "government")]:
        hint = op.CATEGORY_HINTS[(direction, category)]
        assert op.esc(hint) in page, f"missing hint for {(direction, category)}: {hint!r}"
    assert "class='hint'" in page


def test_build_search_index_writes_index_without_individual_pages(search_db_path, tmp_path):
    # A8: build_search_index now writes a small tombstone (see
    # render_index_tombstone_page()), not the full embedded-JSON search page
    # -- it no longer contains individual entity names.
    out_dir = tmp_path / "orgs"
    n, index_path = op.build_search_index(search_db_path, out_dir=str(out_dir))
    assert n == 4  # 5 entities minus the one with role='no_flows'
    assert os.path.exists(index_path)
    assert not (out_dir / "red-river-charity.html").exists()
    with open(index_path, encoding="utf-8") as f:
        content = f.read()
    assert "Red River Charity" not in content
    assert "/orgs" in content
    assert_parses_cleanly(content)


def test_render_index_tombstone_page_links_to_live_orgs_route():
    page = op.render_index_tombstone_page()
    assert "href=\"/orgs\"" in page
    assert_parses_cleanly(page)


def test_index_tombstone_file_is_small_not_the_old_10mb_embed():
    # Regression guard against accidentally regenerating the old 10.7MB
    # embedded-JSON file at the committed docs/orgs/index.html path.
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "orgs", "index.html")
    if not os.path.exists(path):
        pytest.skip("docs/orgs/index.html not present in this checkout")
    assert os.path.getsize(path) < 5000


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


# ── fetch_regranting_network / render_regranting_network_svg ────────────────
# Isolated in-memory fixture (not the shared db_path one, which has no
# dual_role entities) -- mirrors _make_collision_con()'s pattern.

def _make_regranting_con():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE entities (entity_id INTEGER, canonical_name VARCHAR, entity_kind VARCHAR)
    """)
    con.execute("""
        CREATE TABLE entity_role_summary (entity_id INTEGER, role VARCHAR, total_given DOUBLE, total_received DOUBLE)
    """)
    con.execute("""
        CREATE TABLE grants_unified (funder_entity_id INTEGER, recipient_entity_id INTEGER, amount_cad DOUBLE)
    """)
    entities = [
        (1, "Intermediary Org", "charity"),
        (2, "Big Funder", "federal_dept"),
        (3, "Medium Funder", "funder_org"),
        (4, "Small Funder A", "funder_org"),
        (5, "Small Funder B", "funder_org"),
        (6, "Tiny Funder", "funder_org"),
        (7, "Downstream Recipient A", "charity"),
        (8, "Downstream Recipient B", "charity"),
        (9, "Not Dual Role Org", "charity"),
        (10, "Second Intermediary", "charity"),
    ]
    con.executemany("INSERT INTO entities VALUES (?, ?, ?)", entities)
    con.executemany("INSERT INTO entity_role_summary VALUES (?, ?, ?, ?)", [
        (1, "dual_role", 500000.0, 900000.0),
        (9, "primarily_recipient", 0.0, 50000.0),
        (10, "dual_role", 10000.0, 20000.0),
    ])
    con.executemany("INSERT INTO grants_unified VALUES (?, ?, ?)", [
        (1, 1, 999999.0),      # self-loop -- must be excluded
        (2, 1, 400000.0),      # entity 2 funds both intermediaries (shared node)
        (2, 10, 5000.0),
        (3, 1, 200000.0),
        (4, 1, 50000.0),
        (5, 1, 30000.0),
        (6, 1, 5000.0),        # 5th-ranked funder -- collapsed into "other" at top_n_edges=4
        (1, 7, 300000.0),
        (1, 8, 100000.0),
    ])
    return con


def test_fetch_regranting_network_excludes_self_loops(db_path):
    con = _make_regranting_con()
    try:
        nodes, links = op.fetch_regranting_network(con, top_n_intermediaries=10, top_n_edges=4)
    finally:
        con.close()
    assert all(src != tgt for src, tgt, _ in links)
    # The self-loop's $999,999 must not appear as a funder OR recipient link for entity 1.
    amounts = [amt for src, tgt, amt in links if src == ("mid", 1) or tgt == ("mid", 1)]
    assert 999999.0 not in amounts


def test_fetch_regranting_network_only_includes_dual_role_entities(db_path):
    con = _make_regranting_con()
    try:
        nodes, _ = op.fetch_regranting_network(con, top_n_intermediaries=10, top_n_edges=4)
    finally:
        con.close()
    mid_ids = {key[1] for key in nodes if nodes[key]["column"] == "intermediary"}
    assert mid_ids == {1, 10}
    assert 9 not in mid_ids  # primarily_recipient, not dual_role


def test_fetch_regranting_network_collapses_long_tail_into_other_bucket(db_path):
    con = _make_regranting_con()
    try:
        nodes, links = op.fetch_regranting_network(con, top_n_intermediaries=10, top_n_edges=4)
    finally:
        con.close()
    # 5 real funders for entity 1, top_n_edges=4 -- the 5th (Tiny Funder, $5,000) collapses.
    assert ("fund", 6) not in nodes
    other_key = ("fund_other", 1)
    assert other_key in nodes
    assert nodes[other_key]["entity_id"] is None
    other_link = next(amt for src, tgt, amt in links if src == other_key and tgt == ("mid", 1))
    assert other_link == 5000.0


def test_fetch_regranting_network_shares_funder_node_across_intermediaries(db_path):
    con = _make_regranting_con()
    try:
        nodes, links = op.fetch_regranting_network(con, top_n_intermediaries=10, top_n_edges=4)
    finally:
        con.close()
    # Entity 2 funds both intermediary 1 and intermediary 10 -- one shared
    # node, two separate links, not two separate nodes.
    assert ("fund", 2) in nodes
    fund_2_targets = {tgt for src, tgt, _ in links if src == ("fund", 2)}
    assert fund_2_targets == {("mid", 1), ("mid", 10)}


def test_fetch_regranting_network_respects_top_n_intermediaries_limit(db_path):
    con = _make_regranting_con()
    try:
        nodes, _ = op.fetch_regranting_network(con, top_n_intermediaries=1, top_n_edges=4)
    finally:
        con.close()
    mid_ids = {key[1] for key in nodes if nodes[key]["column"] == "intermediary"}
    assert mid_ids == {1}  # entity 1 has more total flow than entity 10 -- only it survives limit=1


def test_render_regranting_network_svg_empty_input_returns_placeholder():
    assert "svg" not in op.render_regranting_network_svg({}, []).lower()


def test_render_regranting_network_svg_produces_valid_svg_with_expected_nodes():
    con = _make_regranting_con()
    try:
        nodes, links = op.fetch_regranting_network(con, top_n_intermediaries=10, top_n_edges=4)
    finally:
        con.close()
    svg = op.render_regranting_network_svg(nodes, links)
    assert svg.startswith("<svg")
    assert svg.count("<rect") == len(nodes)
    assert svg.count("<path") == len(links)
    assert "Intermediary Org" in svg


def test_render_regranting_network_svg_uses_link_manifest_for_hrefs():
    con = _make_regranting_con()
    try:
        nodes, links = op.fetch_regranting_network(con, top_n_intermediaries=10, top_n_edges=4)
    finally:
        con.close()
    manifest = {1: "custom-slug-for-entity-1"}
    svg = op.render_regranting_network_svg(nodes, links, link_manifest=manifest)
    assert "/orgs/custom-slug-for-entity-1" in svg
    # Entity 2 has no manifest entry -- falls back to slug_for(name).
    assert f"/orgs/{op.slug_for('Big Funder')}" in svg


def test_render_regranting_network_svg_other_bucket_nodes_are_not_links():
    con = _make_regranting_con()
    try:
        nodes, links = op.fetch_regranting_network(con, top_n_intermediaries=10, top_n_edges=4)
    finally:
        con.close()
    svg = op.render_regranting_network_svg(nodes, links)
    assert "other funders" in svg
    assert "<a href='/orgs/other" not in svg  # no href should ever point at a synthetic bucket
