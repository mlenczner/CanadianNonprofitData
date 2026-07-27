"""
Live Web App

A small local Flask server that queries nonprofit_network.duckdb directly on
each request, instead of the pre-generated static HTML approach in
org_page.py / grant_search.py. Those two modules' rendering functions (CSS,
JS, render_page, render_grant_detail_page, ...) are reused as-is -- this
module only adds live-query search and the Flask routing/glue around them.

Why live instead of static: org_page.py's search index had to cap at the top
50,000 organizations (embedding the full ~533k-entity corpus as JSON produced
a 105MB file) and grant_search.py's at the top 40,000 texts (~44MB) --
querying DuckDB directly on each request removes both caps entirely (every
search runs over the full corpus, ~0.2-0.5s) and removes the "does this
detail page exist yet" problem (org_page.py --all / grant_search.py --all
had to batch-generate files in advance; here every page renders on demand).
This is a real architecture change from the rest of this repo's "self-
contained HTML, no dependencies" pages -- a deliberate choice, not a default
(see AGENTS.md).

The org detail page's slow-regranter cost (AGENTS.md: The Salvation Army
takes several minutes to render, because each embedded T3010-qualified-donee
grant's receipt drawer runs its own DB query) is unchanged here -- still not
fixed, now experienced as request latency instead of batch-build time. Worth
addressing before this is used for a large org's page routinely; not done in
this pass.

Run with:
    python analysis/webapp.py                      # http://127.0.0.1:8931
    python analysis/webapp.py --port 8931 --db nonprofit_network.duckdb

Respects AGENTS.md: never reads grants.csv or the T3010 CSVs directly --
everything comes from DuckDB queries against nonprofit_network.duckdb. Opens
the DB read-only; do not run this while a build (analysis/build_entity_graph.py)
holds a write lock on it.
"""

import argparse
import json
import os
import re
import sys
import time

from flask import Flask, jsonify, request, abort, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import org_page as op
import grant_search as gs


def json_for_script(obj):
    """json.dumps() for embedding directly into an inline <script> block.
    Plain json.dumps() doesn't escape '<', so a value containing a literal
    "</script>" (real risk here: raw source org names can carry literal HTML
    -- AGENTS.md issue #3/A1's span-wrapped/entity-encoded names) would
    prematurely close the script tag and get parsed as raw HTML/script,
    reintroducing the same stored-XSS class A3's esc() JS helper closes for
    fetched results. Escaping every '<' (not just "</script>") is the
    standard safe approach and is semantically inert in JS/JSON."""
    return json.dumps(obj).replace("<", "\\u003c")


app = Flask(__name__)

_state = {
    "con": None, "db_path": op.DEFAULT_DB_PATH, "link_manifest": None, "slug_to_id": None,
    "discovery_index": None,
}


def get_db():
    """Returns a fresh DuckDB cursor on every call, sharing the one
    underlying connection opened for the app's lifetime. Confirmed the hard
    way: handing out the raw shared Connection object directly is not safe
    under concurrent requests -- Flask's dev server does process requests
    concurrently (multiple threads), and two overlapping con.execute() calls
    on the same Connection silently corrupt each other's result (a fetchone()
    that should never return None started returning it, surfaced by B1's
    unified search page firing two concurrent fetches for the first time in
    this app -- but the underlying bug was latent before that, not
    introduced by it, and could resurface from any future concurrent access).
    con.cursor() is DuckDB's documented safe pattern for concurrent use of
    one Connection -- confirmed directly: 8 threads x 20 queries each with
    raw con.execute() corrupted 7/8 threads, the identical test via
    con.cursor() had zero failures."""
    if _state["con"] is None:
        _state["con"] = op.open_db(_state["db_path"])
    return _state["con"].cursor()


def get_link_manifest():
    """entity_id<->slug maps for every entity with grant activity, built once
    at first use and kept in memory for the app's lifetime -- unlike the
    static index's embedded JSON, this never has to be capped or sent to the
    browser, since it only lives server-side."""
    if _state["link_manifest"] is None:
        con = get_db()
        batch = op.fetch_batch_entities(con)
        manifest = op.build_link_manifest(batch)
        _state["link_manifest"] = manifest
        _state["slug_to_id"] = {slug: eid for eid, slug in manifest.items()}
    return _state["link_manifest"], _state["slug_to_id"]


def get_discovery_index():
    """entity_id -> confirmed non-charity-nonprofit discovery match, read
    once from discovery/output/*.csv and cached for the app's lifetime, same
    pattern as get_link_manifest() -- see op.load_discovery_index()'s
    docstring. Empty dict (not an error) if the discovery pipeline hasn't
    been run against this database snapshot yet."""
    if _state["discovery_index"] is None:
        _state["discovery_index"] = op.load_discovery_index()
    return _state["discovery_index"]


# ── live search queries ──────────────────────────────────────────────────────

_ORG_FILTER_COLUMNS = {
    "rq": "recv_qualified", "rn": "recv_non_qualified", "rg": "recv_government",
    "gq": "given_qualified", "gn": "given_non_qualified", "gg": "given_government",
}

# Separate from _ORG_FILTER_COLUMNS on purpose: those are all SQL boolean
# flags computed from grants_unified via _ORG_FLAGS_SUBQUERY. "Confirmed
# non-charity nonprofit" isn't a database column at all -- it comes from
# discovery/'s output CSVs (see get_discovery_index()), so it's applied as
# an entity_id IN (...) condition instead of a subquery join.
NON_CHARITY_FILTER_KEY = "nc"


# Fixed flags subquery -- identical to org_page.py's fetch_batch_entities, so
# this stays a single source of truth for what each flag means rather than
# dynamically re-deriving a subset of these expressions per request
# (computing all 6 costs about the same as computing 2 or 3: the cost is the
# one full grants_unified scan, not the number of MAX() expressions over it).
_ORG_FLAGS_SUBQUERY = """
    WITH flows AS (
        SELECT recipient_entity_id AS entity_id, source_dataset, 0 AS is_given FROM grants_unified
        UNION ALL
        SELECT funder_entity_id AS entity_id, source_dataset, 1 AS is_given FROM grants_unified
    )
    SELECT entity_id,
      MAX(is_given = 0 AND source_dataset = 't3010_qualified_donee') AS recv_qualified,
      MAX(is_given = 0 AND source_dataset = 't3010_non_qualified_donee') AS recv_non_qualified,
      MAX(is_given = 0 AND source_dataset IN ('federal_gc', 'canada_council', 'otf')) AS recv_government,
      MAX(is_given = 1 AND source_dataset = 't3010_qualified_donee') AS given_qualified,
      MAX(is_given = 1 AND source_dataset = 't3010_non_qualified_donee') AS given_non_qualified,
      MAX(is_given = 1 AND source_dataset IN ('federal_gc', 'canada_council', 'otf')) AS given_government
    FROM flows GROUP BY entity_id
"""


def _word_start_pattern(text):
    """Regex matching `text` only when it starts at a word boundary (start
    of string, or right after a non-alphanumeric character) -- plain
    substring search (ILIKE '%term%') matched a query like "pal" inside the
    middle of "municiPALity", which then out-ranked genuine matches because
    results are ranked by dollar total, not match quality (e.g. searching
    "pal" surfaced "City of Toronto and the Regional Municipality of York"
    above any org actually named with "Pal"). re.escape neutralizes any
    regex metacharacters in the user's input before this is used as a
    DuckDB regex pattern."""
    return r"(?i)(^|[^a-zA-Z0-9])" + re.escape(text)


def _escape_like(text):
    """Escape LIKE metacharacters (%, _, and the escape char itself) so a
    query containing a literal one is matched literally, not as a wildcard,
    when used as a LIKE pattern (paired with ESCAPE '\\' at the call site)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fold_query(con, query_text):
    """Lowercase + accent-strip + whitespace-collapse a query the exact same
    way entities.search_name was built at build time (DuckDB's native
    strip_accents(), not a second Python-side normalizer, and the same
    regexp_replace('\\s+', ' ', 'g') whitespace collapse) -- folding both
    sides through matching logic avoids two failure modes: a Python-
    unidecode-vs-SQL-strip_accents mismatch, and an internal-double-space
    query (not caught by Flask's outer .strip()) never hitting the exact-
    match ranking tier even against a genuinely exact org name."""
    return con.execute(
        "SELECT lower(strip_accents(regexp_replace(trim(?), '\\s+', ' ', 'g')))", [query_text]
    ).fetchone()[0]


# A6 ranking bug, confirmed against the real rebuilt DB (not a fixture
# artifact): strict tier-then-flow ordering (see search_orgs_live) means a
# query whose tier-1 ("search_name starts with query") pool is larger than
# one page can bury a much bigger tier-2+ match entirely -- "red cross" has
# 76 tier-1 matches (mostly tiny hand-typed donation-line records, $100-
# $164K), so LIMIT 50 exhausts inside tier 1 and never reaches tier 2,
# where "THE CANADIAN RED CROSS SOCIETY" ($747.8M total flow) and
# "CANADIAN RED CROSS SOCIETY" ($501.8M) sit -- neither appears anywhere on
# page 1, the opposite of A6's own stated goal. Fixed by promoting a
# tier-2+ match to compete within tier 1 when its own total_flow dwarfs
# every genuine tier-1 match by at least this multiplier -- large enough
# that a merely-bigger (not dominant) match still respects tier purity.
# Confirmed against real data this doesn't flip already-correct orderings:
# for "canada", Global Affairs Canada ($52.2B) is only ~5.5x the biggest
# real tier-1 "canada" match ($9.4B, Canada Economic Development for
# Quebec Regions) -- nowhere near the 100x bar, so it stays unpromoted.
# "red cross"'s ratio is over 4,500x -- far past it. Regression tests:
# tests/test_webapp.py's test_orgs_search_promotes_a_dominant_non_prefix_
# match_out_of_a_crowded_tier and test_orgs_search_does_not_promote_a_
# modest_flow_gap_out_of_tier.
WILDCARD_FLOW_MULTIPLIER = 100


def search_orgs_live(con, query_text, active_filters, limit=50, offset=0, discovery_index=None):
    """Name substring (optional) AND every checked category filter (optional).
    Ranked by match-quality tier first (exact search_name match, then
    search_name starts with query, then any-word-starts-with-query -- the
    only tier the old flow-only ranking supported), flow within tier --
    otherwise "red cross" surfaces ICRC above the Canadian Red Cross Society
    purely because ICRC's total_flow is larger. Matches against
    entities.search_name (accent-stripped, lowercased at build time) rather
    than canonical_name, with the incoming query folded the same way via
    _fold_query(), so "ecole"/"école" return the same results. The flags
    subquery (a full grants_unified scan, ~0.2-0.5s) is only joined in when a
    filter is actually active -- a plain name search skips it entirely and
    stays fast. Returns (total, rows): total is a COUNT(*) over the same
    WHERE with no LIMIT/OFFSET, so the caller can render "Showing 1-50 of
    N" instead of the old "50 matches (showing first 50)", which never
    reflected the true match count."""
    conditions = ["s.role != 'no_flows'"]
    params = []
    folded_query = None
    if query_text:
        folded_query = _fold_query(con, query_text)
        conditions.append("regexp_matches(e.search_name, ?)")
        params.append(_word_start_pattern(folded_query))

    category_filters = [f for f in active_filters if f in _ORG_FILTER_COLUMNS]
    if category_filters:
        for key in category_filters:
            conditions.append(f"COALESCE(f.{_ORG_FILTER_COLUMNS[key]}, false)")
        join_sql = f"LEFT JOIN ({_ORG_FLAGS_SUBQUERY}) f ON f.entity_id = e.entity_id"
    else:
        join_sql = ""

    if NON_CHARITY_FILTER_KEY in active_filters:
        # entity_id is always a plain int from load_discovery_index() (parsed
        # from our own CSV, never user input) -- safe to inline directly
        # rather than pass a ~9,580-item parameter list.
        ids = ",".join(str(eid) for eid in (discovery_index or {}))
        conditions.append(f"e.entity_id IN ({ids})" if ids else "FALSE")

    where_sql = " AND ".join(conditions)

    # A bare integer literal (e.g. "0") in ORDER BY is parsed by DuckDB as a
    # column-position reference, not a constant -- confirmed the hard way
    # ("ORDER term out of range - should be between 1 and 6"). Only add the
    # rank tier to ORDER BY when there's an actual query to rank against;
    # with no query text, ranking is meaningless and the original
    # total_flow-only order applies unchanged.
    if folded_query is not None:
        tier_case = ("CASE WHEN e.search_name = ? THEN 0 "
                     "WHEN e.search_name LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END")
        # LIKE wildcards (%, _) in the query itself must be escaped, or a
        # search for a literal "%" or "_" (e.g. an org named "100% Fund")
        # silently widens the ranking-tier match instead of matching the
        # literal character.
        #
        # Bind order: tier_case's two placeholders now sit inside the
        # `matched` CTE's SELECT list, which is textually *before* the
        # WHERE clause's own placeholder (params) -- DuckDB binds `?`
        # positionally by where it appears in the SQL text, not by
        # variable-naming intent, so rank_params must come first here (the
        # opposite order from the no-query branch below, and from this
        # function's pre-CTE version, which had WHERE's placeholder first).
        rank_params = [folded_query, _escape_like(folded_query) + "%"]
        bind_params = rank_params + params
        # Two CTE layers: `tier` has to already exist as a real column
        # before a window function can aggregate over it (tier1_max_flow),
        # so it can't be computed in the same SELECT as its own CASE
        # expression. See WILDCARD_FLOW_MULTIPLIER's docstring for why the
        # promotion step (the outer ORDER BY's CASE) exists at all.
        query = f"""
            WITH matched AS (
                SELECT e.entity_id, e.canonical_name, e.city, e.province, e.entity_kind,
                       COALESCE(s.total_given, 0) + COALESCE(s.total_received, 0) AS total_flow,
                       {tier_case} AS tier
                FROM entities e
                JOIN entity_role_summary s ON s.entity_id = e.entity_id
                {join_sql}
                WHERE {where_sql}
            ), ranked AS (
                SELECT *,
                       MAX(total_flow) FILTER (WHERE tier = 1) OVER () AS tier1_max_flow,
                       COUNT(*) OVER() AS total_count
                FROM matched
            )
            SELECT entity_id, canonical_name, city, province, entity_kind, total_flow, total_count
            FROM ranked
            ORDER BY CASE WHEN tier > 1 AND total_flow > COALESCE(tier1_max_flow, 0) * {WILDCARD_FLOW_MULTIPLIER}
                          THEN 1 ELSE tier END,
                     total_flow DESC, entity_id
            LIMIT ? OFFSET ?
        """
    else:
        # COUNT(*) OVER() computes the true total over the full WHERE-matched
        # set (window functions apply before LIMIT/OFFSET in SQL's logical
        # order, confirmed directly against DuckDB) in the same pass as the
        # page of rows -- avoids a second full scan of the same joins/
        # subquery just to get a count, which would otherwise double the
        # cost of every filtered search (the _ORG_FLAGS_SUBQUERY join is a
        # full grants_unified scan).
        bind_params = params
        query = f"""
            SELECT e.entity_id, e.canonical_name, e.city, e.province, e.entity_kind,
                   COALESCE(s.total_given, 0) + COALESCE(s.total_received, 0) AS total_flow,
                   COUNT(*) OVER() AS total_count
            FROM entities e
            JOIN entity_role_summary s ON s.entity_id = e.entity_id
            {join_sql}
            WHERE {where_sql}
            ORDER BY total_flow DESC, e.entity_id
            LIMIT ? OFFSET ?
        """
    # entity_id as a final deterministic tiebreaker -- without one, ties on
    # total_flow (common: many entities share the same value, including 0)
    # have no guaranteed stable order across two separate LIMIT/OFFSET
    # queries, so "Show more" could skip or repeat a row at the page
    # boundary depending on how DuckDB's parallel execution happens to
    # order that request.
    rows = con.execute(query, bind_params + [limit, offset]).fetchall()
    total = rows[0][-1] if rows else con.execute(f"""
        SELECT COUNT(*) FROM entities e
        JOIN entity_role_summary s ON s.entity_id = e.entity_id
        {join_sql}
        WHERE {where_sql}
    """, params).fetchone()[0]
    cols = ["entity_id", "canonical_name", "city", "province", "entity_kind", "total_flow"]
    return total, [dict(zip(cols, r[:-1])) for r in rows]


def search_grant_texts_live(con, query_text, active_sources, limit=50, offset=0):
    """Same live-query approach as search_orgs_live, for grant text. Reuses
    grant_search.py's exact normalized-grouping expression (see that
    module's docstring for why: grouping must match text_hash's own
    normalization or index stats silently undercount). Query and
    description/program_name are both folded through lower(strip_accents())
    inline in SQL (A4) -- no precomputed column on grants_unified, since the
    spec's own fallback explicitly allows per-request folding and this
    avoids rebuilding the much larger grants_unified table; if this proves
    measurably slow in practice, the next step is a precomputed column the
    same way entities.search_name was added, not attempted here since it
    isn't proven necessary. Returns (total, rows) -- see search_orgs_live's
    docstring for why."""
    sources = [s for s in active_sources if s in gs.GRANT_TEXT_SOURCES] or list(gs.GRANT_TEXT_SOURCES)
    conditions = ["description IS NOT NULL", f"source_dataset IN ({', '.join('?' for _ in sources)})"]
    params = list(sources)
    if query_text:
        pattern = _word_start_pattern(_fold_query(con, query_text))
        conditions.append(
            "(regexp_matches(lower(strip_accents(description)), ?) "
            "OR regexp_matches(lower(strip_accents(program_name)), ?))"
        )
        params.extend([pattern, pattern])
    where_sql = " AND ".join(conditions)
    normalized_cte = f"""
        WITH normalized AS (
            SELECT source_dataset, description, program_name, amount_cad, fiscal_year,
                   regexp_replace(trim(description), '\\s+', ' ', 'g') AS norm_desc,
                   regexp_replace(trim(COALESCE(program_name, '')), '\\s+', ' ', 'g') AS norm_prog
            FROM grants_unified
            WHERE {where_sql}
        )
    """
    # COUNT(*) OVER() over the grouped rows in the same pass, same reasoning
    # as search_orgs_live -- window functions apply after GROUP BY but
    # before LIMIT/OFFSET in SQL's logical order, so this avoids a second
    # full scan + regroup of grants_unified just to get a count.
    query = f"""
        {normalized_cte}
        SELECT source_dataset,
               LEFT(ANY_VALUE(description), {gs.TEXT_TRUNCATE_LEN}) AS text,
               ANY_VALUE(program_name) AS program_name,
               count(*) AS n,
               sum(amount_cad) AS total_amount,
               min(fiscal_year) AS min_year,
               max(fiscal_year) AS max_year,
               sha256(norm_desc || ' ' || norm_prog) AS text_hash,
               COUNT(*) OVER() AS total_count
        FROM normalized
        GROUP BY source_dataset, norm_desc, norm_prog
        ORDER BY total_amount DESC, text_hash
        LIMIT ? OFFSET ?
    """
    rows = con.execute(query, params + [limit, offset]).fetchall()
    total = rows[0][-1] if rows else con.execute(f"""
        {normalized_cte}
        SELECT COUNT(*) FROM (
            SELECT 1 FROM normalized GROUP BY source_dataset, norm_desc, norm_prog
        )
    """, params).fetchone()[0]
    rows = [r[:-1] for r in rows]
    cols = ["source_dataset", "text", "program_name", "n", "total_amount", "min_year", "max_year", "text_hash"]
    return total, [dict(zip(cols, r)) for r in rows]


# ── HTML shells (search-box + filters, results filled in by JS via fetch) ──

SEARCH_PAGE_CSS_EXTRA = """
.search-box{width:100%;font-size:1.1rem;padding:14px 16px;border:1px solid var(--line);border-radius:10px;margin-top:20px}
.filters{display:flex;gap:28px;flex-wrap:wrap;margin-top:16px;padding:14px 16px;background:var(--card);border:1px solid var(--line);border-radius:10px}
.filter-group{display:flex;flex-direction:column;gap:6px;font-size:.85rem}
.filter-label{color:var(--mut);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin-bottom:2px}
.filter-group label{display:flex;flex-direction:column;gap:2px;cursor:pointer}
.filter-group label .chk-row{display:flex;align-items:center;gap:7px}
.filter-group label .hint{color:var(--mut);font-size:.72rem;margin-left:22px;font-weight:400;text-transform:none;letter-spacing:normal}
.results{margin-top:18px}
.result{display:block;padding:12px 14px;border:1px solid var(--line);border-radius:8px;margin-bottom:8px;background:var(--card);text-decoration:none;color:var(--ink)}
.result:hover{border-color:var(--red)}
.result b{display:block}
.result p{color:var(--mut);font-size:.82rem;margin:4px 0}
.result span{color:var(--mut);font-size:.82rem}
.count{color:var(--mut);font-size:.85rem;margin-top:10px}
.show-more{display:block;margin:14px auto 0;padding:8px 18px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);cursor:pointer;font-size:.85rem}
.show-more:hover{border-color:var(--red)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.chip{font-size:.8rem;padding:6px 12px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--ink);cursor:pointer}
.chip:hover{border-color:var(--red);color:var(--red)}
.section-heading{margin:26px 0 4px;font-size:1rem}
.section-heading:first-child{margin-top:0}
.see-all{font-size:.82rem;font-weight:400;margin-left:8px}
"""

# Shared by both search pages' inline <script> blocks. Every server-supplied
# string (org names, grant descriptions -- both come from raw source data
# that can contain literal HTML, e.g. the span-wrapped/entity-encoded names
# in AGENTS.md issue #3/A1, or grant descriptions with real "<" characters)
# gets interpolated into innerHTML template literals -- without escaping,
# that's a stored-XSS hole, not just a display bug. Mirrors org_page.py's
# server-side esc().
ESC_JS = """
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
"""

# B2: reflect search state into the URL (shareable/bookmarkable searches --
# core for the journalist/researcher audience this project is built for) via
# history.replaceState, read back on load. `f` is a repeatable param (one
# entry per checked filter key); syncUrlState/restoreUrlState both treat it
# as an array so this works unchanged for orgs' `f` and grants' `src`.
DEFAULT_STATE_JS = """
function syncUrlState(state) {
  const params = new URLSearchParams();
  if (state.q) params.set('q', state.q);
  (state.f || []).forEach(v => params.append('f', v));
  (state.src || []).forEach(v => params.append('src', v));
  if (state.section) params.set('section', state.section);
  const qs = params.toString();
  history.replaceState(null, '', qs ? '?' + qs : location.pathname);
}
function restoreUrlState(qInput, checkboxes, keyAttr) {
  const params = new URLSearchParams(location.search);
  const term = params.get('q');
  if (term) qInput.value = term;
  const active = new Set(params.getAll(keyAttr || 'f'));
  checkboxes.forEach(cb => { if (active.has(cb.dataset[keyAttr === 'src' ? 'src' : 'key'])) cb.checked = true; });
}
"""


# B3: shown before any input on the org search / unified search pages --
# blank-page-until-typing wastes the first impression a visitor gets.
EXAMPLE_ORG_QUERIES = ["food bank", "Ontario Trillium Foundation", "housing"]


def render_orgs_search_page(con=None, discovery_index=None):
    groups = []
    for direction, group_label in (("received", "Received"), ("given", "Given")):
        boxes = "".join(
            f"<label><span class='chk-row'><input type='checkbox' data-key='{key}'> "
            f"{op.esc(op.CATEGORY_HEADINGS[(d, cat)])}</span>"
            f"<span class='hint'>{op.esc(op.CATEGORY_HINTS[(d, cat)])}</span></label>"
            for key, _, d, cat in op.SEARCH_FILTER_FIELDS if d == direction
        )
        groups.append(f"<div class='filter-group'><span class='filter-label'>{op.esc(group_label)}</span>{boxes}</div>")
    # Separate group, not a (direction, category) flag like the others above
    # -- see NON_CHARITY_FILTER_KEY's comment in search_orgs_live().
    groups.append(
        f"<div class='filter-group'><span class='filter-label'>Identity</span>"
        f"<label><span class='chk-row'><input type='checkbox' data-key='{NON_CHARITY_FILTER_KEY}'> "
        f"Confirmed non-charity nonprofit (REQ / Corporations Canada)</span>"
        f"<span class='hint'>{op.esc(op.NON_CHARITY_FILTER_HINT)}</span></label></div>"
    )
    filters_html = f"<div class='filters'>{''.join(groups)}</div>"

    default_json, default_caption = _default_orgs_payload(con, discovery_index)
    default_caption_json = json_for_script(default_caption)
    chips_html = "".join(
        f"<button type='button' class='chip' data-q='{op.esc(ex)}'>{op.esc(ex)}</button>"
        for ex in EXAMPLE_ORG_QUERIES
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Search organizations — Canadian Nonprofit Data</title>
<style>{op.CSS}{SEARCH_PAGE_CSS_EXTRA}</style></head><body>
<div class="wrap">
<header><div><h1>Search organizations</h1>
<p class="meta-line">Live search over every organization with recorded grant activity.
<a href="/grants">Search grant text instead &rarr;</a></p></div></header>
<input class="search-box" id="q" type="text" placeholder="Search by organization name..." autofocus>
<div class="chips">{chips_html}</div>
{filters_html}
<div class="count" id="count"></div>
<div class="results" id="results"></div>
<button class="show-more" id="showMore" style="display:none">Show more</button>
</div>
<script>
{ESC_JS}
{DEFAULT_STATE_JS}
const PAGE_SIZE = 50;
const DEFAULT_RESULTS = {default_json};
const DEFAULT_CAPTION = {default_caption_json};
const q = document.getElementById('q');
const results = document.getElementById('results');
const count = document.getElementById('count');
const showMoreBtn = document.getElementById('showMore');
const checkboxes = Array.from(document.querySelectorAll('.filters input[type=checkbox]'));
let debounceTimer;
let offset = 0;
let shownRows = [];
let requestSeq = 0;
function render(list) {{
  results.innerHTML = list.map(r =>
    `<a class="result" href="/orgs/${{esc(r.s)}}"><b>${{esc(r.n)}}</b><span>${{esc(r.k)}}${{r.loc ? ' · ' + esc(r.loc) : ''}} · ${{esc(r.f)}} total flow</span></a>`
  ).join('');
}}
function showDefaultState() {{
  count.textContent = DEFAULT_CAPTION;
  render(DEFAULT_RESULTS);
  showMoreBtn.style.display = 'none';
}}
async function runSearch(reset) {{
  if (reset) {{ offset = 0; shownRows = []; }}
  const term = q.value.trim();
  const activeKeys = checkboxes.filter(cb => cb.checked).map(cb => cb.dataset.key);
  syncUrlState({{q: term, f: activeKeys}});
  if (!term && !activeKeys.length) {{ showDefaultState(); return; }}
  const params = new URLSearchParams();
  if (term) params.set('q', term);
  activeKeys.forEach(k => params.append('f', k));
  params.set('offset', offset);
  count.textContent = 'Searching...';
  // Request-sequencing guard: a fast typist can fire two overlapping
  // fetches (e.g. "a" then "ab") that resolve out of order -- without this,
  // an older, slower response arriving after a newer one silently
  // overwrites the results/count the user is currently looking at.
  const mySeq = ++requestSeq;
  const res = await fetch('/orgs/search.json?' + params.toString());
  const data = await res.json();
  if (mySeq !== requestSeq) return;
  shownRows = shownRows.concat(data.results);
  render(shownRows);
  count.textContent = `Showing 1–${{shownRows.length.toLocaleString()}} of ${{data.total.toLocaleString()}}`;
  showMoreBtn.style.display = shownRows.length < data.total ? 'block' : 'none';
}}
function search() {{ runSearch(true); }}
function showMore() {{ offset += PAGE_SIZE; runSearch(false); }}
function scheduleSearch() {{ clearTimeout(debounceTimer); debounceTimer = setTimeout(search, 200); }}
q.addEventListener('input', scheduleSearch);
checkboxes.forEach(cb => cb.addEventListener('change', search));
showMoreBtn.addEventListener('click', showMore);
document.querySelectorAll('.chip').forEach(chip => chip.addEventListener('click', () => {{
  q.value = chip.dataset.q; search();
}}));
restoreUrlState(q, checkboxes);
if (q.value.trim() || checkboxes.some(cb => cb.checked)) {{ search(); }} else {{ showDefaultState(); }}
</script>
</body></html>"""


def render_grants_search_page():
    source_boxes = "".join(
        f"<label><input type='checkbox' data-src='{op.esc(s)}'> {op.esc(op.SOURCE_LABELS.get(s, s))}</label>"
        for s in gs.GRANT_TEXT_SOURCES
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Search grant text — Canadian Nonprofit Data</title>
<style>{op.CSS}{SEARCH_PAGE_CSS_EXTRA}</style></head><body>
<div class="wrap">
<header><div><h1>Search grant text</h1>
<p class="meta-line">Live search over every distinct grant program/description text (Federal G&amp;C, Ontario Trillium Foundation).
<a href="/orgs">Search organizations instead &rarr;</a></p></div></header>
<input class="search-box" id="q" type="text" placeholder="Search grant program names and descriptions..." autofocus>
<div class="filters">{source_boxes}</div>
<div class="count" id="count"></div>
<div class="results" id="results"></div>
<button class="show-more" id="showMore" style="display:none">Show more</button>
</div>
<script>
{ESC_JS}
{DEFAULT_STATE_JS}
const PAGE_SIZE = 50;
const q = document.getElementById('q');
const results = document.getElementById('results');
const count = document.getElementById('count');
const showMoreBtn = document.getElementById('showMore');
const checkboxes = Array.from(document.querySelectorAll('.filters input[type=checkbox]'));
let debounceTimer;
let offset = 0;
let shownRows = [];
let requestSeq = 0;
function render(list) {{
  results.innerHTML = list.map(r =>
    `<a class="result" href="/grants/${{esc(r.h)}}"><b>${{r.p ? esc(r.p) : '(no program name)'}}</b>` +
    `<p>${{esc(r.t)}}</p>` +
    `<span>${{r.n.toLocaleString()}} grant${{r.n === 1 ? '' : 's'}} · ${{esc(r.amt)}} · ${{esc(r.y)}}</span></a>`
  ).join('');
}}
async function runSearch(reset) {{
  if (reset) {{ offset = 0; shownRows = []; }}
  const term = q.value.trim();
  const activeSrcs = checkboxes.filter(cb => cb.checked).map(cb => cb.dataset.src);
  syncUrlState({{q: term, src: activeSrcs}});
  if (!term && !activeSrcs.length) {{
    count.textContent = ''; results.innerHTML = ''; showMoreBtn.style.display = 'none'; return;
  }}
  const params = new URLSearchParams();
  if (term) params.set('q', term);
  activeSrcs.forEach(s => params.append('src', s));
  params.set('offset', offset);
  count.textContent = 'Searching...';
  // See the org search page's identical guard for why this is needed.
  const mySeq = ++requestSeq;
  const res = await fetch('/grants/search.json?' + params.toString());
  const data = await res.json();
  if (mySeq !== requestSeq) return;
  shownRows = shownRows.concat(data.results);
  render(shownRows);
  count.textContent = `Showing 1–${{shownRows.length.toLocaleString()}} of ${{data.total.toLocaleString()}}`;
  showMoreBtn.style.display = shownRows.length < data.total ? 'block' : 'none';
}}
function search() {{ runSearch(true); }}
function showMore() {{ offset += PAGE_SIZE; runSearch(false); }}
function scheduleSearch() {{ clearTimeout(debounceTimer); debounceTimer = setTimeout(search, 200); }}
q.addEventListener('input', scheduleSearch);
checkboxes.forEach(cb => cb.addEventListener('change', search));
showMoreBtn.addEventListener('click', showMore);
restoreUrlState(q, checkboxes, 'src');
if (q.value.trim() || checkboxes.some(cb => cb.checked)) {{ search(); }}
</script>
</body></html>"""


# B1: one search box, results split into two labeled sections rather than
# two separate pages -- "org vs grant text" is this repo's own architecture
# (two different tables, two different live-query functions), not how a
# visitor thinks about the question they're asking. Each section shows a
# handful of top matches with a "see all" link carrying the query forward to
# the existing dedicated page (which still has the full filter/pagination
# UI) -- this page deliberately doesn't reimplement filters or pagination.
UNIFIED_SECTION_PREVIEW = 8


def render_unified_search_page():
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Search — Canadian Nonprofit Data</title>
<style>{op.CSS}{SEARCH_PAGE_CSS_EXTRA}</style></head><body>
<div class="wrap">
<header><div><h1>Search</h1>
<p class="meta-line">Search organizations and what grant money was for, together.</p></div></header>
<input class="search-box" id="q" type="text" placeholder="Search by organization name, or what a grant was for..." autofocus>
<div class="chips">{"".join(f"<button type='button' class='chip' data-q='{op.esc(ex)}'>{op.esc(ex)}</button>" for ex in EXAMPLE_ORG_QUERIES)}</div>

<h2 class="section-heading" id="orgs-section">Organizations
<a class="see-all" id="orgsSeeAll" href="/orgs">See all &rarr;</a></h2>
<div class="count" id="orgsCount"></div>
<div class="results" id="orgsResults"></div>

<h2 class="section-heading" id="grants-section">What the money was for
<a class="see-all" id="grantsSeeAll" href="/grants">See all &rarr;</a></h2>
<div class="count" id="grantsCount"></div>
<div class="results" id="grantsResults"></div>
</div>
<script>
{ESC_JS}
{DEFAULT_STATE_JS}
const q = document.getElementById('q');
const orgsResults = document.getElementById('orgsResults');
const orgsCount = document.getElementById('orgsCount');
const orgsSeeAll = document.getElementById('orgsSeeAll');
const grantsResults = document.getElementById('grantsResults');
const grantsCount = document.getElementById('grantsCount');
const grantsSeeAll = document.getElementById('grantsSeeAll');
let debounceTimer;
let requestSeq = 0;

function renderOrgs(list) {{
  orgsResults.innerHTML = list.map(r =>
    `<a class="result" href="/orgs/${{esc(r.s)}}"><b>${{esc(r.n)}}</b><span>${{esc(r.k)}}${{r.loc ? ' · ' + esc(r.loc) : ''}} · ${{esc(r.f)}} total flow</span></a>`
  ).join('');
}}
function renderGrants(list) {{
  grantsResults.innerHTML = list.map(r =>
    `<a class="result" href="/grants/${{esc(r.h)}}"><b>${{r.p ? esc(r.p) : '(no program name)'}}</b>` +
    `<p>${{esc(r.t)}}</p>` +
    `<span>${{r.n.toLocaleString()}} grant${{r.n === 1 ? '' : 's'}} · ${{esc(r.amt)}} · ${{esc(r.y)}}</span></a>`
  ).join('');
}}
async function search() {{
  const term = q.value.trim();
  syncUrlState({{q: term}});
  if (!term) {{
    orgsCount.textContent = ''; orgsResults.innerHTML = '';
    grantsCount.textContent = ''; grantsResults.innerHTML = '';
    orgsSeeAll.href = '/orgs'; grantsSeeAll.href = '/grants';
    return;
  }}
  orgsSeeAll.href = '/orgs?q=' + encodeURIComponent(term);
  grantsSeeAll.href = '/grants?q=' + encodeURIComponent(term);
  orgsCount.textContent = 'Searching...';
  grantsCount.textContent = 'Searching...';
  // See render_orgs_search_page's identical guard for why this is needed.
  const mySeq = ++requestSeq;
  const [orgsRes, grantsRes] = await Promise.all([
    fetch('/orgs/search.json?q=' + encodeURIComponent(term)),
    fetch('/grants/search.json?q=' + encodeURIComponent(term)),
  ]);
  const orgsData = await orgsRes.json();
  const grantsData = await grantsRes.json();
  if (mySeq !== requestSeq) return;
  const orgsShown = orgsData.results.slice(0, {UNIFIED_SECTION_PREVIEW});
  const grantsShown = grantsData.results.slice(0, {UNIFIED_SECTION_PREVIEW});
  renderOrgs(orgsShown);
  renderGrants(grantsShown);
  orgsCount.textContent = orgsData.total
    ? `Showing ${{orgsShown.length}} of ${{orgsData.total.toLocaleString()}}` : 'No matches';
  grantsCount.textContent = grantsData.total
    ? `Showing ${{grantsShown.length}} of ${{grantsData.total.toLocaleString()}}` : 'No matches';
}}
function scheduleSearch() {{ clearTimeout(debounceTimer); debounceTimer = setTimeout(search, 200); }}
q.addEventListener('input', scheduleSearch);
document.querySelectorAll('.chip').forEach(chip => chip.addEventListener('click', () => {{
  q.value = chip.dataset.q; search();
}}));
restoreUrlState(q, []);
const params = new URLSearchParams(location.search);
const section = params.get('section');
if (section === 'orgs' || section === 'grants') {{
  document.getElementById(section + '-section').scrollIntoView({{block: 'start'}});
}}
if (q.value.trim()) {{ search(); }}
</script>
</body></html>"""


HOME_HTML = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Canadian Nonprofit Data</title>
<style>{op.CSS}
.tiles{{display:flex;gap:16px;flex-wrap:wrap;margin-top:24px}}
.tile{{flex:1 1 260px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:22px;text-decoration:none;color:var(--ink)}}
.tile:hover{{border-color:var(--red)}}
.tile h2{{margin:0 0 8px;border:none;padding:0;font-size:1.15rem;text-transform:none;letter-spacing:0;color:var(--ink)}}
.tile p{{color:var(--mut);font-size:.88rem;margin:0}}
</style></head><body>
<div class="wrap">
<header><div><h1>Canadian Nonprofit Data</h1>
<p class="meta-line">Live search over the entity graph -- organizations, and the grants that connect them.</p></div></header>
<div class="tiles">
<a class="tile" href="/search?section=orgs"><h2>Search organizations &rarr;</h2><p>Find an organization by name, or by what grants it has received or given.</p></a>
<a class="tile" href="/search?section=grants"><h2>Search grant text &rarr;</h2><p>Find grants by what the money was for.</p></a>
<a class="tile" href="/hidden-nonprofits"><h2>Non-charity nonprofits &rarr;</h2><p>Legally incorporated nonprofits confirmed in the federal grants data that do not hold registered-charity status.</p></a>
<a class="tile" href="/regranting-network"><h2>Where regranted money goes &rarr;</h2><p>How money flows through the largest funder-and-recipient organizations on its way to smaller nonprofits.</p></a>
<a class="tile" href="/data-quality-rankings.html"><h2>Grants disclosure quality &rarr;</h2><p>How completely federal departments fill in their own mandatory disclosure fields, department by department.</p></a>
<a class="tile" href="/about"><h2>About this data &rarr;</h2><p>What's here, how it's used, how it's built, and what's coming next.</p></a>
<a class="tile" href="/questions"><h2>What can you ask it? &rarr;</h2><p>Real examples of questions this data can actually answer.</p></a>
</div>
</div>
</body></html>"""

CONTENT_PAGE_CSS = """
.content{max-width:680px}
.content h2{font-size:1.05rem;margin:36px 0 12px;text-transform:none;letter-spacing:0;color:var(--ink);border-bottom:2px solid var(--red);display:inline-block;padding-bottom:4px}
.content h2:first-of-type{margin-top:8px}
.content p{margin:0 0 14px;color:var(--ink)}
.content .intro{color:var(--mut);font-size:.95rem}
.qcard{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:14px}
.qcard h3{margin:0 0 8px;font-size:1.02rem;color:var(--ink)}
.qcard p{margin:0;color:var(--mut);font-size:.92rem}
.back-nav{margin-bottom:20px;font-size:.85rem}
.back-nav a{color:var(--red);text-decoration:none;font-weight:600}
.back-nav a:hover{text-decoration:underline}
.content a{color:var(--red);text-decoration:none;font-weight:600}
.content a:hover{text-decoration:underline}
"""

ABOUT_HTML = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>About this data — Canadian Nonprofit Data</title>
<style>{op.CSS}{CONTENT_PAGE_CSS}</style></head><body>
<div class="wrap">
<div class="back-nav"><a href="/">&larr; Home</a></div>
<header><div><h1>About this data</h1>
<p class="meta-line intro">What's here, how it's used, how it's built, and what's coming next.</p></div></header>
<div class="content">

<h2>What's been collected</h2>
<p><a href="https://open.canada.ca/data/en/dataset/432527ab-7aac-45b5-81d6-7597107a7013" target="_blank" rel="noopener">The federal government's Grants and Contributions proactive disclosure</a> covers most departments and agencies, listing who received money, how much, and roughly what it was for. The bulk of usable records run from 2005 through mid-2026, with a small number of stray pre-2005 entries that are almost certainly data-entry errors rather than real historical grants. Departments vary in how completely they fill in this disclosure — see the <a href="/data-quality-rankings.html">disclosure quality report</a> for a department-by-department breakdown, with real examples.</p>
<p><a href="https://search.open.canada.ca/opendata/?search_text=list+of+charities" target="_blank" rel="noopener">The Canada Revenue Agency's T3010 charity annual returns</a> add a second layer: not just who the registered charities are, but who they gave money to, since larger charities often grant money onward to other organizations rather than spending it all directly. Gifts to other registered charities are covered from 2013 through 2024; gifts to non-charity recipients are a newer mandatory disclosure field and only run from 2022 through 2024.</p>
<p><a href="https://canadacouncil.ca/research/data-tables" target="_blank" rel="noopener">Canada Council for the Arts</a> publishes its own grants separately from the general federal disclosures, covering 2017 through 2024.</p>
<p><a href="https://otf.ca/open" target="_blank" rel="noopener">Ontario Trillium Foundation</a>, a granting body funded through Ontario's charitable gaming proceeds, is the fourth, and the longest-running of the four: 1999 through 2025.</p>
<p>A fifth, newer piece covers Quebec specifically: <a href="https://www.donneesquebec.ca/recherche/dataset/registre-des-entreprises" target="_blank" rel="noopener">the Registre des entreprises</a>, the province's registry of every legally incorporated nonprofit, as it stood as of the most recent snapshot pulled for this project. Most of the organizations in that registry never show up in any charity conversation, because holding a charter as a nonprofit and holding registered-charity status with the CRA are two different things.</p>

<h2>How it's being used</h2>
<p>None of these sources were built to talk to each other. A charity might show up with three slightly different name spellings across three different government files, and there's no shared key connecting a federal grant recipient's record to the same organization's own charity filing. Linking them together is most of the real work here. Once that's done, two things become possible that weren't before: a single page for any organization showing everything it received and everything it gave, drawn from whichever sources mention it, and a search tool that finds organizations by name or by the kind of funding relationship they have.</p>
<p>A second search tool works in the other direction, searching what a grant was actually for rather than who received it. And the Quebec piece asks a narrower, different question than the rest of the project: for a given legal nonprofit, does it hold registered-charity status or not? Most public attention, and most existing tools, focus only on the registered-charity slice of a much larger sector.</p>

<h2>The methodology</h2>
<p>The hard part is deciding when two names in two different datasets refer to the same organization. Where a shared official ID number exists — a charity's federal Business Number, for instance — that's used directly and treated as close to certain. Where no ID number is available, names are compared for similarity using automated text matching, tuned for the specific quirks in this kind of data: bilingual French and English names, legal suffixes like "Inc." or "Foundation" that don't distinguish one organization from another, and category words such as "Centre" or "Association" that show up in hundreds of unrelated names.</p>
<p>That matching isn't trusted at face value. Where a match is confident, it's stated as fact. Where it's genuinely uncertain, that uncertainty is shown rather than resolved by guessing.</p>
<p>Government grant records also get revised after the fact — an agreement's value can change through a later amendment — so only the final, latest version of each agreement counts toward any total, to avoid a superseded number being added on top of its replacement.</p>

<h2>What's coming next</h2>
<p>The Quebec charity-status question started as a Montreal-only pilot and now runs against the whole province: close to 59,000 legally incorporated Quebec nonprofits checked against the CRA's own charity registry. Extending the same approach to federally incorporated nonprofits through Corporations Canada, then eventually to other provinces' own registries, would turn a single-province picture into a national one.</p>
<p>Separately, a small trial run has already classified around a thousand grants by what kind of work they actually fund — health, education, environment, and so on — out of roughly 160,000 distinct grant purposes in the federal data. Scaling that from a trial to the full set is planned but not yet done. A handful of known rough edges in the underlying government files, years where the source data's encoding or formatting breaks in specific, already-identified ways, also still need a proper fix rather than a workaround.</p>

</div>
</div>
</body></html>"""

QUESTIONS = [
    ("Who has been funding a specific organization?",
     "Pull up an organization's page and see every government grant, charity gift, or other funding relationship "
     "recorded under its name, added together with a year-by-year breakdown of when the money came in."),
    ("What has a major foundation or department given money to?",
     "For an organization that mostly gives rather than receives, the site lists its recipients and separates "
     "them by kind: gifts to other registered charities, gifts to organizations without charitable status, and "
     "direct government-program funding."),
    ("Is this Quebec nonprofit actually a registered charity?",
     "Most nonprofits in Canada aren't registered charities. For organizations legally incorporated in Quebec, "
     "this project can say, based on matching against the CRA's own registry, whether a given group holds "
     "charitable status, doesn't, or falls into a genuinely ambiguous case that needs a closer look."),
    ("What was a specific grant actually for?",
     "Rather than starting from an organization's name, searching by the wording of a program description "
     "surfaces every grant that shares that purpose, wherever it shows up in the federal data."),
    ("Does an organization show up under more than one name?",
     "Many organizations file under slightly different spellings, abbreviations, or bilingual variants across "
     "different government sources. Once those are linked together, an organization's full funding picture "
     "stops being split across records that look unrelated at first glance."),
    ("How has an organization's funding changed over time?",
     "A year-by-year chart on each organization's page shows whether its funding has grown, shrunk, or shifted "
     "between government sources and other charities."),
    ("How many legal nonprofits in a province are outside the charity system entirely?",
     "For Quebec specifically, this project can put a number on how many legally incorporated nonprofits exist "
     "that have never registered as charities with the CRA — a part of the sector that rarely gets counted "
     "anywhere else."),
    ("Which organizations mostly give money away versus mostly receive it?",
     "Every organization in the data gets a rough profile — primarily a funder, primarily a recipient, or a "
     "genuine mix of both — based on comparing what it gave against what it received."),
]

QUESTIONS_HTML = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>What can you ask it? — Canadian Nonprofit Data</title>
<style>{op.CSS}{CONTENT_PAGE_CSS}</style></head><body>
<div class="wrap">
<div class="back-nav"><a href="/">&larr; Home</a></div>
<header><div><h1>What can you ask this data?</h1>
<p class="meta-line intro">A few of the actual questions this project can answer, and how.</p></div></header>
<div class="content">
{"".join(f"<div class='qcard'><h3>{op.esc(q)}</h3><p>{op.esc(a)}</p></div>" for q, a in QUESTIONS)}
</div>
</div>
</body></html>"""


DISCOVERY_SOURCE_LABELS = {
    "req": "Quebec (REQ)",
    "corporations_canada": "Federally incorporated (Corporations Canada)",
}


def render_hidden_nonprofits_page(con, discovery_index):
    """Computed live (not a hardcoded/static page like ABOUT_HTML/
    QUESTIONS_HTML above) via op.fetch_discovery_summary(), so the numbers
    stay accurate to whatever nonprofit_network.duckdb snapshot and
    discovery/ output are actually loaded, rather than going stale the way a
    hand-written figure would the next time either gets rebuilt."""
    summary = op.fetch_discovery_summary(con, discovery_index)
    manifest, _ = get_link_manifest()

    source_rows = "".join(
        f"<div class='stat'><b>{op.fmt_int(v['count'])}</b>"
        f"<span>{op.esc(DISCOVERY_SOURCE_LABELS.get(k, k))} &middot; {op.fmt_money(v['dollars'])}</span></div>"
        for k, v in sorted(summary["by_source"].items(), key=lambda kv: -kv[1]["dollars"])
    )

    example_rows = "".join(
        f"<a class='result' href='/orgs/{manifest.get(r['entity_id'], op.slug_for(r['canonical_name']))}'>"
        f"<b>{op.esc(op.english_name(r['canonical_name']))}</b>"
        f"<span>{op.esc(r['province'] or '')} &middot; {op.fmt_money(r['total_received'])} total received</span></a>"
        for r in summary["top_examples"]
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Non-charity nonprofits in the federal grants data — Canadian Nonprofit Data</title>
<style>{op.CSS}{CONTENT_PAGE_CSS}{SEARCH_PAGE_CSS_EXTRA}</style></head><body>
<div class="wrap">
<div class="back-nav"><a href="/">&larr; Home</a></div>
<header><div><h1>Non-charity nonprofits in the federal grants data</h1>
<p class="meta-line intro">Registered-charity status with the CRA and legal incorporation as a nonprofit are governed separately — an organization can hold one without the other.</p></div></header>
<div class="content">

<p>The Canada Revenue Agency's charity registry is the source most nonprofit-sector tools and datasets draw from. But registered-charity status and legal incorporation as a nonprofit are two separate things: an organization can be incorporated as a nonprofit without ever registering as a charity, and there is no requirement that it do so. Federal grant records name tens of thousands of recipients that do not appear in any charity registry, consistent with this.</p>
<p>This project matches federal grant recipients against two independent legal registries — Quebec's provincial nonprofit registry (REQ) and Corporations Canada's federal not-for-profit registry — to identify which recipients are confirmed, legally incorporated nonprofits, independent of whether they also hold registered-charity status.</p>

<div class="stats">
<div class="stat"><b>{op.fmt_int(summary['total_orgs'])}</b><span>confirmed non-charity nonprofits</span></div>
<div class="stat"><b>{op.fmt_money(summary['total_dollars'])}</b><span>in federal grants they've received</span></div>
</div>

<h2>By registry</h2>
<div class="stats">{source_rows}</div>

<h2>Examples</h2>
<p>A selection of confirmed organizations, by total federal funding received:</p>
<div class="results">{example_rows}</div>

<h2>What this isn't</h2>
<p>This is a real but partial picture, not a national count. Quebec's registry only covers Quebec-incorporated nonprofits; Corporations Canada's registry only covers <em>federally</em> incorporated ones. Most local and regional Canadian nonprofits incorporate provincially, in a province other than Quebec — and nothing here currently covers those. Every organization above is a confirmed match, not a guess: see <a href="../entity-resolution-methodology.md">entity-resolution-methodology.md</a> and <code>discovery/</code> in the repository for exactly how each one was verified, including the false-positive patterns that were found and fixed before any of these numbers were trusted.</p>

</div>
</div>
</body></html>"""

SANKEY_PAGE_CSS = """
.wide-wrap{max-width:1180px}
.sankey-scroll{overflow-x:auto;margin-top:24px;border:1px solid var(--line);border-radius:10px;background:var(--card);padding:10px}
.sankey{min-width:1080px;display:block}
.sankey-col-label{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;fill:var(--mut);font-weight:700}
.sankey-node{stroke:var(--card);stroke-width:1.5}
.sankey-node-funder{fill:#8a8f98}
.sankey-node-intermediary{fill:var(--red)}
.sankey-node-recipient{fill:var(--gold)}
.sankey-node-link{cursor:pointer}
.sankey-node-label{font-size:.72rem;fill:var(--ink)}
.sankey-link{fill:#d52b1e;opacity:.12;transition:opacity .15s}
.sankey-link:hover{opacity:.32}
"""


def render_regranting_network_page(con):
    """Computed live via op.fetch_regranting_network() -- see that
    function's docstring for scope (top dual_role intermediaries, top
    funders/recipients each, self-loops excluded, long tail collapsed)."""
    manifest, _ = get_link_manifest()
    nodes, links = op.fetch_regranting_network(con)
    svg = op.render_regranting_network_svg(nodes, links, link_manifest=manifest)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Where regranted money goes — Canadian Nonprofit Data</title>
<style>{op.CSS}{CONTENT_PAGE_CSS}{SANKEY_PAGE_CSS}</style></head><body>
<div class="wrap wide-wrap">
<div class="back-nav"><a href="/">&larr; Home</a></div>
<header><div><h1>Where regranted money goes</h1>
<p class="meta-line intro">The largest organizations that are significant funders <em>and</em> significant recipients at once — money passing through them on its way to smaller, downstream nonprofits.</p></div></header>
<div class="content">
<p>Most organizations in this data are mainly one thing: a funder, or a recipient. A smaller set do real amounts of both — receiving significant funding and re-granting significant amounts onward, acting as an intermediary layer between government/major donors and the smaller nonprofits actually doing the work. The diagram below traces that flow for the largest such intermediaries: hover any node or ribbon for the exact amount, click a node to open its page. Ribbon width is proportional to dollar amount; a funder or recipient shared by more than one intermediary appears once, with a link to each.</p>
</div>
<div class="sankey-scroll">{svg}</div>
</div>
</body></html>"""


# ── routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return HOME_HTML


@app.route("/search")
def unified_search():
    return render_unified_search_page()


@app.route("/about")
def about():
    return ABOUT_HTML


@app.route("/entity-resolution-methodology.md")
def entity_resolution_methodology():
    """org_page.py's discovery-badge drawer and /hidden-nonprofits both link
    to '../entity-resolution-methodology.md' -- a relative reference
    written for the static docs/orgs/<slug>.html layout (where it correctly
    resolves to docs/entity-resolution-methodology.md). In the live app
    every one of those pages is served from a flat, extensionless
    namespace, and the same relative reference resolves to this exact
    root-level path instead (confirmed: urljoin from both /orgs/<slug> and
    /hidden-nonprofits lands here) -- so serving the file at this path,
    rather than rewriting every link, fixes it without touching the
    static-page rendering code at all. Plain text, not HTML -- no
    markdown-rendering dependency in this project."""
    return send_from_directory(os.path.join(op.ROOT, "docs"), "entity-resolution-methodology.md",
                                mimetype="text/plain")


@app.route("/data-quality-rankings.html")
def data_quality_rankings():
    """Serves the static, pre-built department publishing-quality report
    (docs/data-quality-rankings.html, built by analysis/build_quality_report.py
    from grants.csv) from inside the live app's own navigation, at the same
    filename/path it already has in the static docs/ site -- so a link to it
    works unchanged whether it's reached from GitHub Pages or from here.
    Unlike entity-resolution-methodology.md's route above, no relative-link
    resolution problem forced this path; it's just this report's real
    filename, kept stable in case anything already links to it directly."""
    return send_from_directory(os.path.join(op.ROOT, "docs"), "data-quality-rankings.html")


@app.route("/questions")
def questions():
    return QUESTIONS_HTML


@app.route("/hidden-nonprofits")
def hidden_nonprofits():
    con = get_db()
    discovery_index = get_discovery_index()
    return render_hidden_nonprofits_page(con, discovery_index)


@app.route("/regranting-network")
def regranting_network():
    con = get_db()
    return render_regranting_network_page(con)


def _shape_org_results(rows, manifest, discovery_index):
    """Row-shaping shared by orgs_search_json (live search results) and
    _default_orgs_payload (B3's pre-query top-20 embed) -- one place for the
    n/s/k/loc/f field mapping so the two don't drift apart."""
    return [
        {
            "n": op.english_name(r["canonical_name"]),
            "s": manifest.get(r["entity_id"], op.slug_for(r["canonical_name"])),
            # A confirmed discovery match overrides the generic entity_kind
            # label -- "Confirmed non-charity nonprofit" is more informative
            # than "Organization" for exactly the ~9,580 entities this applies to.
            "k": ("Confirmed non-charity nonprofit" if r["entity_id"] in discovery_index
                  else op.KIND_LABELS.get(r["entity_kind"], "Organization")),
            "loc": ", ".join(p for p in (r["city"], r["province"]) if p),
            "f": op.fmt_money(r["total_flow"]),
        }
        for r in rows
    ]


def _default_orgs_payload(con, discovery_index):
    """B3: the top ~20 organizations by total flow, embedded directly in the
    page so there's something to look at before any input -- a blank search
    box until typing wastes the first impression for a visitor who doesn't
    know what to search for yet. con=None (no live DB, e.g. a bare function
    call outside the Flask app) renders an empty default state rather than
    erroring. Returns JSON already made safe for <script> embedding via
    json_for_script() -- callers must not re-encode it."""
    if con is None:
        return "[]", ""
    manifest, _ = get_link_manifest()
    _, rows = search_orgs_live(con, "", [], limit=20, offset=0, discovery_index=discovery_index or {})
    results = _shape_org_results(rows, manifest, discovery_index or {})
    caption = f"Top {len(results)} organizations by total flow" if results else ""
    return json_for_script(results), caption


@app.route("/orgs")
def orgs_search():
    return render_orgs_search_page(get_db(), get_discovery_index())


@app.route("/orgs/search.json")
def orgs_search_json():
    con = get_db()
    manifest, _ = get_link_manifest()
    discovery_index = get_discovery_index()
    query_text = request.args.get("q", "").strip()
    active_filters = request.args.getlist("f")
    offset = request.args.get("offset", 0, type=int)
    total, rows = search_orgs_live(con, query_text, active_filters, limit=50, offset=offset,
                                    discovery_index=discovery_index)
    results = _shape_org_results(rows, manifest, discovery_index)
    return jsonify({"total": total, "results": results})


@app.route("/orgs/<slug>")
def org_detail(slug):
    con = get_db()
    manifest, slug_to_id = get_link_manifest()
    entity_id = slug_to_id.get(slug)
    if entity_id is None:
        abort(404)
    return op.render_page(con, entity_id, link_manifest=manifest, lazy_receipts=True,
                           discovery_index=get_discovery_index())


@app.route("/orgs/<slug>/receipt/<int:grant_id>")
def org_receipt(slug, grant_id):
    """Backs toggleLazyDrawer(): fetches and renders the receipt for exactly
    one grant row, on demand, instead of org_detail eagerly computing up to
    SCALE_CAP of these per page load (confirmed ~0.15s each for a T3010
    qualified-donee receipt -- ~45s for a page with 300 of them)."""
    con = get_db()
    _, slug_to_id = get_link_manifest()
    entity_id = slug_to_id.get(slug)
    if entity_id is None:
        abort(404)
    direction = request.args.get("direction")
    if direction not in ("received", "given"):
        abort(400)
    other_col = "funder_entity_id" if direction == "received" else "recipient_entity_id"
    row = con.execute(f"""
        SELECT g.grant_id, g.fiscal_year, o.canonical_name AS other_name, o.entity_id AS other_entity_id,
               g.program_name, g.description, g.amount_cad, g.source_dataset, g.source_ref
        FROM grants_unified g
        JOIN entities o ON o.entity_id = g.{other_col}
        WHERE g.grant_id = ?
    """, [grant_id]).fetchone()
    if not row:
        abort(404)
    cols = ["grant_id", "fiscal_year", "other_name", "other_entity_id", "program_name",
            "description", "amount_cad", "source_dataset", "source_ref"]
    grant = dict(zip(cols, row))
    return op.render_grant_receipt(con, entity_id, grant, direction)


@app.route("/grants")
def grants_search():
    return render_grants_search_page()


@app.route("/grants/search.json")
def grants_search_json():
    con = get_db()
    query_text = request.args.get("q", "").strip()
    active_sources = request.args.getlist("src")
    offset = request.args.get("offset", 0, type=int)
    total, rows = search_grant_texts_live(con, query_text, active_sources, limit=50, offset=offset)
    results = [
        {
            "t": r["text"], "p": r["program_name"] or "", "h": r["text_hash"][:16], "src": r["source_dataset"],
            "n": r["n"], "amt": op.fmt_money(r["total_amount"]),
            "y": f"{r['min_year']}–{r['max_year']}" if r["min_year"] is not None else "",
        }
        for r in rows
    ]
    return jsonify({"total": total, "results": results})


@app.route("/grants/<text_hash>")
def grant_detail(text_hash):
    con = get_db()
    grants = gs.fetch_grants_for_text(con, text_hash)
    if not grants:
        abort(404)
    return gs.render_grant_detail_page_from_grants(grants, live=True)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the live Canadian Nonprofit Data web app.")
    parser.add_argument("--db", default=op.DEFAULT_DB_PATH, help="path to nonprofit_network.duckdb")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8931)
    parser.add_argument("--debug", action="store_true", help="enable Flask debug mode (auto-reload)")
    args = parser.parse_args(argv)

    _state["db_path"] = args.db
    if not os.path.exists(args.db):
        print(f"ERROR: database not found at {args.db}\nRun analysis/build_entity_graph.py first.", file=sys.stderr)
        sys.exit(1)

    # Both get_link_manifest() and get_discovery_index() are lazily built on
    # first use and cached for the app's lifetime -- left lazy, the first
    # real request after boot paid the full build cost inline (confirmed:
    # ~3s, the link manifest scanning every entity with grant activity).
    # Building both here instead means that cost lands once, at startup,
    # not on whichever visitor's request happens to arrive first.
    warmup_start = time.time()
    get_link_manifest()
    get_discovery_index()
    print(f"Warmed up link manifest + discovery index in {time.time() - warmup_start:.2f}s")

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
