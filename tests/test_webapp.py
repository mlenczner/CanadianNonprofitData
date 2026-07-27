"""Tests for analysis/webapp.py. Uses the same fixture DuckDB as
test_org_page.py (imported directly, not duplicated) plus grant_search.py's
GRANT_TEXT_SOURCES-shaped rows, never the real ~1.6GB nonprofit_network.duckdb."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analysis import webapp as w
from test_org_page import make_fixture_db


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "webapp_fixture.duckdb")
    make_fixture_db(db_path)
    # Reset module-level state so each test gets a fresh connection/manifest
    # against its own tmp_path fixture rather than leaking a prior test's.
    w._state["con"] = None
    w._state["db_path"] = db_path
    w._state["link_manifest"] = None
    w._state["slug_to_id"] = None
    w._state["discovery_index"] = None
    w.app.config["TESTING"] = True
    with w.app.test_client() as c:
        yield c
    if w._state["con"] is not None:
        w._state["con"].close()
        w._state["con"] = None


def test_home_page_loads(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"Search organizations" in res.data
    assert b"Search grant text" in res.data


# ── B1: unified search page ──────────────────────────────────────────────────

def test_unified_search_page_loads(client):
    res = client.get("/search")
    assert res.status_code == 200
    page = res.get_data(as_text=True)
    assert "Organizations" in page
    assert "What the money was for" in page


def test_home_tiles_link_to_unified_search_with_section(client):
    res = client.get("/")
    page = res.get_data(as_text=True)
    assert "/search?section=orgs" in page
    assert "/search?section=grants" in page


def test_unified_search_page_escapes_interpolated_fields(client):
    page = client.get("/search").get_data(as_text=True)
    assert "function esc(" in page
    for field in ("r.n", "r.k", "r.loc", "r.f", "r.h", "r.p", "r.t", "r.amt", "r.y"):
        assert f"esc({field})" in page


def test_get_db_returns_a_fresh_cursor_safe_under_concurrent_use(client):
    # B1's unified search page fires /orgs/search.json and /grants/search.json
    # concurrently (Promise.all) -- the first place in this app that
    # deliberately issues overlapping requests, and doing so against a real
    # server surfaced a real bug: handing every caller the same shared
    # DuckDB Connection object lets two overlapping con.execute() calls
    # corrupt each other's result, intermittently returning None where a row
    # was guaranteed (confirmed directly: 8 threads x 20 queries each via the
    # raw shared Connection corrupted 7/8 threads; the identical test via
    # con.cursor() had zero failures). get_db() now returns a fresh cursor
    # per call for exactly this reason -- this test exercises that directly
    # (not via Flask's test_client, which isn't itself thread-safe for
    # concurrent raw-thread use, a separate, unrelated limitation of the
    # test harness rather than of the real Werkzeug dev server).
    import concurrent.futures

    errors = []

    def worker(i):
        try:
            for _ in range(15):
                con = w.get_db()
                row = con.execute("SELECT lower(strip_accents(?))", [f"query{i}"]).fetchone()
                if row is None:
                    errors.append(f"worker {i} got None")
        except Exception as e:
            errors.append(f"worker {i}: {e}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker, i) for i in range(8)]
        [f.result() for f in futures]

    assert errors == []


# ── B2: URL state ─────────────────────────────────────────────────────────────

def test_orgs_search_page_includes_url_state_js(client):
    page = client.get("/orgs").get_data(as_text=True)
    assert "function syncUrlState(" in page
    assert "function restoreUrlState(" in page


def test_grants_search_page_includes_url_state_js(client):
    page = client.get("/grants").get_data(as_text=True)
    assert "function syncUrlState(" in page
    assert "restoreUrlState(q, checkboxes, 'src')" in page


# ── security fixes found by review: script-tag-breakout JSON, LIKE escaping ─

def test_json_for_script_escapes_script_tag_breakout():
    # A canonical_name containing a literal "</script>" must not be able to
    # prematurely close the embedding <script> block -- json.dumps() alone
    # doesn't escape '<', which would reintroduce the same stored-XSS class
    # A3's esc() JS helper closes for fetched (as opposed to embedded) results.
    payload = [{"n": "Evil Org</script><script>alert(1)</script>"}]
    embedded = w.json_for_script(payload)
    assert "</script>" not in embedded
    assert "\\u003c" in embedded
    import json
    # Round-trips back to the real value -- < is a valid JSON/JS escape
    # for '<', so this is purely a script-tag-safety transform, not a
    # data-corrupting one.
    assert json.loads(embedded.replace("\\u003c", "<"))[0]["n"] == payload[0]["n"]


def test_orgs_search_page_default_payload_has_no_raw_script_close_tag(client):
    # End-to-end: even if an org name in the fixture DB contained "</script>",
    # the rendered page's embedded DEFAULT_RESULTS must not break out of the
    # <script> block. Verified via the unit test above at the function level;
    # this confirms the render path actually uses json_for_script().
    page = client.get("/orgs").get_data(as_text=True)
    assert "const DEFAULT_RESULTS = " in page


def test_escape_like_neutralizes_percent_and_underscore_wildcards():
    assert w._escape_like("100% Fund") == "100\\% Fund"
    assert w._escape_like("a_b") == "a\\_b"
    assert w._escape_like("back\\slash") == "back\\\\slash"


def test_orgs_search_json_literal_percent_query_does_not_crash_or_widen_match(client):
    # "100%" as a query must not act as a SQL LIKE wildcard in the ranking
    # tier -- just confirm the request succeeds and returns a well-formed
    # response (the fixture has no org actually named with '%', so this is
    # a smoke test against a crash/malformed-SQL regression, not a match
    # assertion).
    res = client.get("/orgs/search.json?q=100%25")
    assert res.status_code == 200
    data = res.get_json()
    assert "total" in data and "results" in data


# ── B3: default (pre-query) state ────────────────────────────────────────────

def test_orgs_search_page_embeds_default_top_orgs(client):
    page = client.get("/orgs").get_data(as_text=True)
    assert "DEFAULT_RESULTS" in page
    # entity 5 "Big Regranter Foundation" has the largest total_flow in the
    # fixture -- it should appear in the embedded top-20 default payload.
    assert "Big Regranter Foundation" in page


def test_orgs_search_page_shows_example_query_chips(client):
    page = client.get("/orgs").get_data(as_text=True)
    for example in w.EXAMPLE_ORG_QUERIES:
        assert f"data-q='{example}'" in page


# ── B4: plain-language filter-label hints ────────────────────────────────────
# op.CATEGORY_HINTS/NON_CHARITY_FILTER_HINT were added so a visitor who
# doesn't already know T3010 jargon ("non-qualified donee") gets a one-line
# explanation under each checkbox -- render_orgs_search_page() has its own
# copy of the checkbox-rendering loop (separate from org_page.py's static
# render_index_page, tested independently in test_org_page.py), so it needs
# its own regression test rather than assuming the static page's coverage
# extends here.

def test_orgs_search_page_shows_plain_language_hint_under_every_category_checkbox(client):
    from analysis import org_page as op
    page = client.get("/orgs").get_data(as_text=True)
    for direction, category in [("received", "qualified_donee"), ("received", "non_qualified_donee"),
                                 ("received", "government"), ("given", "qualified_donee"),
                                 ("given", "non_qualified_donee"), ("given", "government")]:
        hint = op.CATEGORY_HINTS[(direction, category)]
        assert op.esc(hint) in page, f"missing hint for {(direction, category)}: {hint!r}"


def test_orgs_search_page_shows_non_charity_filter_hint(client):
    from analysis import org_page as op
    page = client.get("/orgs").get_data(as_text=True)
    assert op.esc(op.NON_CHARITY_FILTER_HINT) in page


def test_orgs_search_page_loads(client):
    res = client.get("/orgs")
    assert res.status_code == 200
    assert b"Search organizations" in res.data
    assert b"data-key='rq'" in res.data


# ── A3: XSS -- server-supplied fields must be escaped before innerHTML use ──
# JS runs client-side, so pytest can't execute it -- this is a source-pattern
# check confirming every field interpolated into the search-result template
# literals is wrapped in esc(), not a DOM/rendering test. Live XSS-payload
# verification (a crafted <img onerror> result rendering as inert text) was
# done manually against a running server in the browser.

def test_orgs_search_page_js_escapes_every_interpolated_field(client):
    page = client.get("/orgs").get_data(as_text=True)
    assert "function esc(" in page
    for field in ("r.n", "r.k", "r.loc", "r.f", "r.s"):
        assert f"esc({field})" in page, f"{field} is interpolated without esc() in render()"


def test_grants_search_page_js_escapes_every_interpolated_field(client):
    page = client.get("/grants").get_data(as_text=True)
    assert "function esc(" in page
    for field in ("r.h", "r.p", "r.t", "r.amt", "r.y"):
        assert f"esc({field})" in page, f"{field} is interpolated without esc() in render()"


def test_orgs_search_json_by_name(client):
    res = client.get("/orgs/search.json?q=Test+Charity+Inc")
    assert res.status_code == 200
    data = res.get_json()
    assert any(r["n"] == "Test Charity Inc" for r in data["results"])


def test_orgs_search_json_by_filter_only(client):
    # entity 3 (Global Affairs Canada) is a federal_dept that GIVES a
    # federal_gc grant -- given_government should be true for it.
    res = client.get("/orgs/search.json?f=gg")
    assert res.status_code == 200
    data = res.get_json()
    names = {r["n"] for r in data["results"]}
    assert "Global Affairs Canada" in names
    # entity 2 (Fuzzy Match Org) only receives a t3010_qualified_donee grant,
    # never gives government funding -- must not match this filter.
    assert "Fuzzy Match Org" not in names


def test_orgs_search_excludes_mid_word_substring_matches(client):
    # Regression test: plain ILIKE '%arge%' would match "Large Foundation"
    # mid-word (L-ARGE), the same bug class reported for real -- "pal"
    # matching inside "municiPALity" and outranking genuine "Pal..." orgs
    # because results are ranked by dollar total, not match quality.
    res = client.get("/orgs/search.json?q=arge")
    assert res.get_json()["results"] == []


def test_orgs_search_matches_word_start(client):
    # "Found" starts a word in both "Large Foundation" and "Big Regranter
    # Foundation" -- word-start matching must still find these.
    res = client.get("/orgs/search.json?q=Found")
    names = {r["n"] for r in res.get_json()["results"]}
    assert "Large Foundation" in names
    assert "Big Regranter Foundation" in names


def test_orgs_search_json_combines_name_and_filter(client):
    res = client.get("/orgs/search.json?q=Fuzzy&f=rq")
    data = res.get_json()
    assert any(r["n"] == "Fuzzy Match Org" for r in data["results"])
    res2 = client.get("/orgs/search.json?q=Fuzzy&f=rg")  # wrong category -- no match
    assert res2.get_json()["results"] == []


# ── A5: total count / pagination ────────────────────────────────────────────

def test_orgs_search_json_returns_total_count(client):
    res = client.get("/orgs/search.json?q=Charity")
    data = res.get_json()
    assert data["total"] == len(data["results"])  # fixture DB is small -- fewer than one page
    assert data["total"] >= 2  # "Test Charity Inc" and "Test Charity Two"


def test_orgs_search_json_offset_paginates(client):
    first = client.get("/orgs/search.json?q=Charity&offset=0").get_json()
    assert first["total"] >= 2
    second = client.get("/orgs/search.json?q=Charity&offset=1").get_json()
    # Same total, but the result set starts one row later.
    assert second["total"] == first["total"]
    if first["results"]:
        assert second["results"] != first["results"] or len(first["results"]) <= 1


# ── A6: match-quality ranking ────────────────────────────────────────────────

def test_orgs_search_ranks_prefix_match_above_higher_flow_word_start_match(client):
    # entity 7 "Canada Council for the Arts" ($15k total flow) has
    # search_name starting with "canada" (tier 1); entity 3 "Global Affairs
    # Canada" ($1.2M total flow) only has "canada" starting a later word
    # (tier 2). Under the old total_flow-only ranking, the $1.2M entity
    # would rank first despite the weaker match -- the same bug class as
    # "red cross" surfacing ICRC above the Canadian Red Cross Society.
    res = client.get("/orgs/search.json?q=canada")
    names = [r["n"] for r in res.get_json()["results"]]
    assert names.index("Canada Council for the Arts") < names.index("Global Affairs Canada")


# ── WILDCARD_FLOW_MULTIPLIER: promoting a dominant match out of a crowded tier ──
# Confirmed against the real rebuilt DB (not just a fixture-only edge case):
# searching "red cross" has 76 tier-1 (search_name starts with "red cross")
# matches, mostly tiny hand-typed donation-line records ($100-$164K) --
# strict tier-then-flow ordering with LIMIT 50 exhausted entirely inside
# tier 1/0 and never reached tier 2, where "THE CANADIAN RED CROSS SOCIETY"
# ($904M+ total flow) sits -- it never appeared on page 1 at all, the
# opposite of A6's own stated purpose. Fixed in search_orgs_live() by
# promoting a tier-2+ match into tier 1 when its own flow dwarfs (100x+)
# every genuine tier-1 match's flow.

def _make_ranking_fixture_con(rows):
    """rows: [(entity_id, canonical_name, total_given, total_received), ...].
    Minimal standalone schema -- just enough for search_orgs_live(), not the
    full shared fixture (which has no entity with anywhere near the crowded-
    tier shape this bug needs to reproduce)."""
    import duckdb as _duckdb
    con = _duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE entities (entity_id INTEGER, canonical_name VARCHAR, city VARCHAR,
                                province VARCHAR, entity_kind VARCHAR, search_name VARCHAR)
    """)
    con.execute("""
        CREATE TABLE entity_role_summary (entity_id INTEGER, total_given DOUBLE,
                                           total_received DOUBLE, role VARCHAR)
    """)
    for entity_id, name, given, received in rows:
        con.execute("INSERT INTO entities VALUES (?, ?, NULL, NULL, 'other_org', ?)",
                    [entity_id, name, name.lower()])
        role = "primarily_recipient" if received >= given else "primarily_funder"
        con.execute("INSERT INTO entity_role_summary VALUES (?, ?, ?, ?)", [entity_id, given, received, role])
    return con


def test_orgs_search_promotes_a_dominant_non_prefix_match_out_of_a_crowded_tier():
    # 60 tier-1 matches (search_name starts with "red cross"), each a tiny
    # $1,000 donation-line-style record -- more than LIMIT, so they'd fill
    # the entire page under pure tier ordering. One tier-2 match ("Canadian
    # Red Cross Society", word-start only -- doesn't literally start with
    # "red cross") carries $500M, dwarfing the tier-1 pool by 500,000x, far
    # past WILDCARD_FLOW_MULTIPLIER's 100x bar.
    rows = [(i, f"Red Cross Branch {i}", 0, 1000) for i in range(1, 61)]
    rows.append((999, "Canadian Red Cross Society", 0, 500_000_000))
    con = _make_ranking_fixture_con(rows)
    total, results = w.search_orgs_live(con, "red cross", [], limit=50)
    names = [r["canonical_name"] for r in results]
    assert "Canadian Red Cross Society" in names, (
        "the $500M tier-2 match must not be entirely crowded off page 1 by 60 tiny tier-1 matches"
    )
    # It dwarfs every tier-1 match so overwhelmingly it should rank first.
    assert names[0] == "Canadian Red Cross Society"


def test_orgs_search_does_not_promote_a_modest_flow_gap_out_of_tier():
    # Mirrors the real "canada" scenario's shape (a handful of tier-1
    # matches, one tier-2 match with meaningfully more flow but nowhere
    # near WILDCARD_FLOW_MULTIPLIER's 100x bar -- an 80x gap here) --
    # confirms the promotion mechanism doesn't over-trigger and start
    # reordering every merely-bigger tier-2 match ahead of genuine prefix
    # matches, which would silently reintroduce the flow-only-ranking bug
    # A6 fixed in the first place.
    rows = [
        (1, "Canada Council A", 0, 10_000),
        (2, "Canada Council B", 0, 8_000),
        (3, "Canada Council C", 0, 5_000),
        (4, "Global Affairs Canada", 0, 800_000),  # 80x tier-1 max -- under the 100x bar
    ]
    con = _make_ranking_fixture_con(rows)
    total, results = w.search_orgs_live(con, "canada", [], limit=50)
    names = [r["canonical_name"] for r in results]
    assert names.index("Canada Council A") < names.index("Global Affairs Canada")
    assert names.index("Canada Council B") < names.index("Global Affairs Canada")
    assert names.index("Canada Council C") < names.index("Global Affairs Canada")


# ── A4: accent-aware search ──────────────────────────────────────────────────

def test_orgs_search_ecole_finds_accented_name(client):
    # entity 11 "École Polytechnique" -- an unaccented query must still find it.
    res = client.get("/orgs/search.json?q=ecole")
    names = {r["n"] for r in res.get_json()["results"]}
    assert "École Polytechnique" in names


def test_orgs_search_accented_query_finds_accented_name(client):
    res = client.get("/orgs/search.json?q=%C3%A9cole")  # "école"
    names = {r["n"] for r in res.get_json()["results"]}
    assert "École Polytechnique" in names


def test_orgs_search_cole_does_not_match_inside_ecole(client):
    # "cole" starts mid-word inside "ecole" (accent-folded "École") -- word-
    # start matching must still reject it, the same word-boundary regex now
    # operating in the accent-stripped, all-ASCII search_name space.
    res = client.get("/orgs/search.json?q=cole")
    names = {r["n"] for r in res.get_json()["results"]}
    assert "École Polytechnique" not in names


# ── "confirmed non-charity nonprofit" filter (discovery/ badge, see NON_CHARITY_FILTER_KEY) ──

def test_orgs_search_page_shows_identity_filter_checkbox(client):
    res = client.get("/orgs")
    assert f"data-key='{w.NON_CHARITY_FILTER_KEY}'".encode() in res.data


def test_non_charity_filter_returns_only_discovery_matched_entities(client):
    # Entity 1 (Test Charity Inc) is pre-loaded via discovery_index directly,
    # bypassing the CSV read (that path is covered by test_org_page.py's
    # load_discovery_index() tests) -- isolates the filter/label logic here.
    w._state["discovery_index"] = {1: {"discovery_source": "req", "discovery_sources": {"req"},
                                        "legal_name": "Test Charity Inc", "jurisdiction": "QC",
                                        "matched_grant_entity_name": "Test Charity Inc"}}
    res = client.get(f"/orgs/search.json?f={w.NON_CHARITY_FILTER_KEY}")
    data = res.get_json()
    names = {r["n"] for r in data["results"]}
    assert names == {"Test Charity Inc"}


def test_non_charity_filter_result_gets_confirmed_label_not_generic_kind(client):
    w._state["discovery_index"] = {1: {"discovery_source": "req", "discovery_sources": {"req"},
                                        "legal_name": "Test Charity Inc", "jurisdiction": "QC",
                                        "matched_grant_entity_name": "Test Charity Inc"}}
    res = client.get(f"/orgs/search.json?q=Test+Charity+Inc")
    data = res.get_json()
    assert data["results"][0]["k"] == "Confirmed non-charity nonprofit"


def test_non_charity_filter_with_empty_discovery_index_returns_no_results(client):
    w._state["discovery_index"] = {}
    res = client.get(f"/orgs/search.json?f={w.NON_CHARITY_FILTER_KEY}")
    assert res.get_json()["results"] == []


def test_non_charity_filter_combines_with_name_search(client):
    w._state["discovery_index"] = {1: {"discovery_source": "req", "discovery_sources": {"req"},
                                        "legal_name": "Test Charity Inc", "jurisdiction": "QC",
                                        "matched_grant_entity_name": "Test Charity Inc"}}
    res = client.get(f"/orgs/search.json?q=Fuzzy&f={w.NON_CHARITY_FILTER_KEY}")
    assert res.get_json()["results"] == []  # "Fuzzy Match Org" (entity 2) isn't in the discovery index


def test_org_detail_page_renders_with_lazy_receipts(client):
    res = client.get("/orgs/test-charity-inc")
    assert res.status_code == 200
    page = res.get_data(as_text=True)
    assert "Test Charity Inc" in page
    # Lazy mode: claims carry data-lazy/data-grant-id, and no drawer div is
    # pre-rendered for them (the whole point -- no eager receipt query).
    assert "data-lazy='1'" in page
    assert "data-grant-id=" in page


def test_org_detail_page_404_for_unknown_slug(client):
    res = client.get("/orgs/does-not-exist")
    assert res.status_code == 404


def test_org_receipt_endpoint_returns_amendment_chain(client):
    # entity 1 (Test Charity Inc) received a federal_gc grant (grant_id=1)
    # with a 3-version amendment chain in the shared fixture.
    res = client.get("/orgs/test-charity-inc/receipt/1?direction=received")
    assert res.status_code == 200
    assert b"Amendment chain" in res.data


def test_org_receipt_endpoint_requires_valid_direction(client):
    res = client.get("/orgs/test-charity-inc/receipt/1?direction=sideways")
    assert res.status_code == 400
    res2 = client.get("/orgs/test-charity-inc/receipt/1")
    assert res2.status_code == 400


def test_org_receipt_endpoint_404_for_unknown_grant(client):
    res = client.get("/orgs/test-charity-inc/receipt/999999?direction=received")
    assert res.status_code == 404


def test_grants_search_page_loads(client):
    res = client.get("/grants")
    assert res.status_code == 200
    assert b"Search grant text" in res.data


def test_grants_search_json_and_detail_roundtrip(client):
    # The shared org_page fixture has no grant_search-shaped rows (no
    # description column populated for federal_gc/otf), so grants
    # search.json legitimately returns nothing here -- this exercises the
    # empty-result path, not a false positive.
    res = client.get("/grants/search.json?q=anything")
    assert res.status_code == 200
    data = res.get_json()
    assert data["total"] == 0
    assert data["results"] == []


def test_grant_detail_404_for_unknown_hash(client):
    res = client.get("/grants/0000000000000000")
    assert res.status_code == 404


def test_grant_detail_route_passes_live_true(client):
    # Real bug, user-reported: org-name links on a live-served grant detail
    # page used the static-site '../orgs/<slug>.html' convention, which
    # 404s against the live app's extensionless /orgs/<slug> routes.
    # Source-pattern check (not a rendered-page assertion) since the shared
    # fixture DB has no grant_search-shaped rows to hit a real detail page
    # with -- grant_search.py's own test suite covers the rendering
    # behavior of live=True directly.
    import inspect
    from analysis import webapp as w
    source = inspect.getsource(w.grant_detail)
    assert "live=True" in source


# ── entity-resolution-methodology.md: fixes the same class of bug -- a
# relative link written for the static docs/orgs/<slug>.html layout, which
# resolves to a different (previously 404ing) root-level path in the live
# app's flat namespace. ────────────────────────────────────────────────────

def test_entity_resolution_methodology_route_serves_the_real_file(client):
    res = client.get("/entity-resolution-methodology.md")
    assert res.status_code == 200
    text = res.get_data(as_text=True)
    assert len(text) > 100  # not an empty/stub response
    assert res.content_type.startswith("text/plain")


# ── /data-quality-rankings.html: department publishing-quality report,
# pre-built by analysis/build_quality_report.py and served here so it's
# reachable from the live app's own navigation (home tile + /about link),
# not just the static docs/ site. ────────────────────────────────────────────

def test_data_quality_rankings_route_serves_the_real_file(client):
    res = client.get("/data-quality-rankings.html")
    assert res.status_code == 200
    text = res.get_data(as_text=True)
    assert len(text) > 1000  # not an empty/stub response
    assert "Federal Grants &amp; Contributions: Disclosure Quality" in text
    assert res.content_type.startswith("text/html")


def test_home_page_links_to_data_quality_rankings(client):
    res = client.get("/")
    assert b"/data-quality-rankings.html" in res.data


def test_about_page_links_to_data_quality_rankings(client):
    res = client.get("/about")
    assert b"/data-quality-rankings.html" in res.data


# ── /hidden-nonprofits: "the nonprofits charity data misses" ────────────────

def test_hidden_nonprofits_page_loads_with_empty_discovery_index(client):
    # No discovery data loaded -- must render a real page (zero state), not error.
    res = client.get("/hidden-nonprofits")
    assert res.status_code == 200
    assert b"Non-charity nonprofits in the federal grants data" in res.data


def test_hidden_nonprofits_shows_correct_totals_and_example(client):
    w._state["discovery_index"] = {
        1: {"discovery_source": "req", "discovery_sources": {"req"}, "legal_name": "Test Charity Inc",
            "jurisdiction": "QC", "matched_grant_entity_name": "Test Charity Inc"},
    }
    res = client.get("/hidden-nonprofits")
    assert res.status_code == 200
    body = res.data.decode()
    assert "Test Charity Inc" in body
    assert w.op.fmt_money(1200000.00) in body  # entity 1's total_received from the shared fixture


def test_hidden_nonprofits_splits_by_source(client):
    w._state["discovery_index"] = {
        1: {"discovery_source": "req", "discovery_sources": {"req"}, "legal_name": "Test Charity Inc",
            "jurisdiction": "QC", "matched_grant_entity_name": "Test Charity Inc"},
        2: {"discovery_source": "corporations_canada", "discovery_sources": {"corporations_canada"},
            "legal_name": "Fuzzy Match Org", "jurisdiction": "BC", "matched_grant_entity_name": "Fuzzy Match Org"},
    }
    res = client.get("/hidden-nonprofits")
    body = res.data.decode()
    assert "Quebec (REQ)" in body
    assert "Corporations Canada" in body


def test_hidden_nonprofits_linked_from_homepage(client):
    res = client.get("/")
    assert b"/hidden-nonprofits" in res.data


# ── /regranting-network ──────────────────────────────────────────────────────
# The shared fixture DB has no dual_role entities (see test_org_page.py's
# isolated _make_regranting_con() for the real data-shape tests) -- this only
# confirms the route itself wires together and renders the empty state
# without crashing.

def test_regranting_network_page_loads(client):
    res = client.get("/regranting-network")
    assert res.status_code == 200
    assert b"Where regranted money goes" in res.data


def test_regranting_network_linked_from_homepage(client):
    res = client.get("/")
    assert b"/regranting-network" in res.data


# ── A7: eager cold-start warmup ─────────────────────────────────────────────

def test_main_warms_up_link_manifest_and_discovery_index_before_serving(tmp_path, monkeypatch):
    # app.run() blocks forever in a real server -- monkeypatch it to a no-op
    # so main() runs to completion, and assert the warmup already happened
    # by the time it would have been called (i.e. before, not lazily on the
    # first real request).
    db_path = str(tmp_path / "warmup_fixture.duckdb")
    make_fixture_db(db_path)
    w._state["con"] = None
    w._state["db_path"] = None
    w._state["link_manifest"] = None
    w._state["slug_to_id"] = None
    w._state["discovery_index"] = None

    ran = {}
    def fake_run(*args, **kwargs):
        ran["link_manifest"] = w._state["link_manifest"]
        ran["discovery_index"] = w._state["discovery_index"]
    monkeypatch.setattr(w.app, "run", fake_run)

    w.main(["--db", db_path])

    assert ran["link_manifest"] is not None
    assert ran["discovery_index"] is not None
    if w._state["con"] is not None:
        w._state["con"].close()
        w._state["con"] = None
