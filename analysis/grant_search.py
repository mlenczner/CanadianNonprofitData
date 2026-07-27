"""
Grant Text Search

Answers a different question than org_page.py: not "who is this organization"
but "what was this grant for" -- search over the free text of grants (program
name + description), not organization names. A separate page from org search
(docs/orgs/index.html) because the two are genuinely different lookups with
different result shapes: an org-name match returns one organization; a grant-
text match returns a program/description shared by potentially thousands of
individual grants (funder, recipient, amount, year each).

Why this only covers two sources: of grants_unified's ~5M rows, 3.77M are
t3010_qualified_donee/t3010_non_qualified_donee, whose program_name is a
single constant label ("Qualified donee gift") and whose description is
always NULL -- no free text to search. federal_gc and canada_council/otf
carry real text (federal_gc: 150k distinct descriptions across 1.09M rows;
otf: 32.8k distinct descriptions, nearly one per row; canada_council has no
description at all, only 17 distinct program-category labels). So this
indexes federal_gc + otf only.

Why distinct text, not distinct grant row: grant descriptions are heavily
templated (same insight analysis/classify_l2.py is built around) -- ~183k
distinct texts cover ~1.1M grant rows. Search matches a text; the result
links to a detail page (this module's own one-text-per-file / index-page
split, mirroring org_page.py's one-org-per-file / index-page split) listing
every grant that shares it.

text_hash uses the same convention as classify_l2.py's text_hash (normalized
whitespace, sha256 of description + " " + program_name) so a future join
against l2_text_classifications can attach L2 subject codes to a grant-text
detail page without recomputing anything -- not implemented here, just kept
compatible.

Run with:
    python analysis/grant_search.py --index                  # docs/grants/index.html (fast, capped)
    python analysis/grant_search.py --text-hash <hash>        # one detail page
    python analysis/grant_search.py --all                     # batch-generate detail pages (capped like --index)

Respects AGENTS.md: never reads grants.csv or the T3010 CSVs directly --
everything comes from DuckDB queries against nonprofit_network.duckdb.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from org_page import (
    CSS, JS, DRAFT_BANNER_TEXT, DRAFT_FULL_TEXT, ROOT, DEFAULT_DB_PATH,
    SOURCE_LABELS, VISIBLE_ROWS, SCALE_CAP,
    esc, fmt_money, fmt_money_precise, fmt_int, english_name, slug_for, open_db,
    open_canada_record_url,
)

GRANTS_DIR = os.path.join(ROOT, "docs", "grants")
TEXT_TRUNCATE_LEN = 300  # see module docstring's "Why distinct text" note; bounds the rare 5000+ char outlier
DEFAULT_GRANT_INDEX_LIMIT = 40_000  # keeps the embedded search index in the same ~10MB ballpark as the org index

GRANT_TEXT_SOURCES = ("federal_gc", "otf")


# ── DB access ────────────────────────────────────────────────────────────────

def fetch_distinct_grant_texts(con, limit=None):
    """One row per distinct (source_dataset, normalized description,
    normalized program_name) among GRANT_TEXT_SOURCES, with aggregate stats
    -- ordered by total dollar amount descending, same convention as
    org_page.py's fetch_batch_entities, so a --limit cutoff keeps the most
    significant programs.

    Grouped by the NORMALIZED (whitespace-collapsed) text, not the raw
    columns -- text_hash normalizes whitespace (matching classify_l2.py's
    convention), so two raw rows differing only in e.g. a double space are
    the same grant text and must land in the same group. Grouping by the raw
    columns instead (an earlier version of this function did) let such pairs
    become two separate index entries whose individual stats undercounted
    what their shared detail page (built by grouping on the hash) actually
    shows -- caught by a real ~0.9% file-count mismatch (39,653 pages
    written for a requested top 40,000) when this was first batch-run."""
    sources_sql = ", ".join(f"'{s}'" for s in GRANT_TEXT_SOURCES)
    query = f"""
        WITH normalized AS (
            SELECT source_dataset, description, program_name, amount_cad, fiscal_year,
                   regexp_replace(trim(description), '\\s+', ' ', 'g') AS norm_desc,
                   regexp_replace(trim(COALESCE(program_name, '')), '\\s+', ' ', 'g') AS norm_prog
            FROM grants_unified
            WHERE source_dataset IN ({sources_sql}) AND description IS NOT NULL
        )
        SELECT source_dataset,
               LEFT(ANY_VALUE(description), {TEXT_TRUNCATE_LEN}) AS text,
               ANY_VALUE(program_name) AS program_name,
               count(*) AS n,
               sum(amount_cad) AS total_amount,
               min(fiscal_year) AS min_year,
               max(fiscal_year) AS max_year,
               sha256(norm_desc || ' ' || norm_prog) AS text_hash
        FROM normalized
        GROUP BY source_dataset, norm_desc, norm_prog
        ORDER BY total_amount DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    rows = con.execute(query).fetchall()
    cols = ["source_dataset", "text", "program_name", "n", "total_amount", "min_year", "max_year", "text_hash"]
    return [dict(zip(cols, r)) for r in rows]


def count_distinct_grant_texts(con):
    sources_sql = ", ".join(f"'{s}'" for s in GRANT_TEXT_SOURCES)
    return con.execute(f"""
        SELECT count(*) FROM (
            SELECT DISTINCT source_dataset,
                   regexp_replace(trim(description), '\\s+', ' ', 'g') AS norm_desc,
                   regexp_replace(trim(COALESCE(program_name, '')), '\\s+', ' ', 'g') AS norm_prog
            FROM grants_unified
            WHERE source_dataset IN ({sources_sql}) AND description IS NOT NULL
        )
    """).fetchone()[0]


def fetch_grants_for_text(con, text_hash):
    """All grants_unified rows whose (source_dataset, description, program_name)
    hash to text_hash, joined to entities for funder/recipient display names.
    Recomputes the same sha256 expression as fetch_distinct_grant_texts so a
    detail page always matches its index entry exactly."""
    sources_sql = ", ".join(f"'{s}'" for s in GRANT_TEXT_SOURCES)
    rows = con.execute(f"""
        SELECT g.grant_id, g.source_dataset, g.description, g.program_name, g.amount_cad, g.fiscal_year,
               f.entity_id AS funder_id, f.canonical_name AS funder_name,
               r.entity_id AS recipient_id, r.canonical_name AS recipient_name, g.source_ref
        FROM grants_unified g
        JOIN entities f ON f.entity_id = g.funder_entity_id
        JOIN entities r ON r.entity_id = g.recipient_entity_id
        WHERE g.source_dataset IN ({sources_sql}) AND g.description IS NOT NULL
          AND LEFT(sha256(regexp_replace(trim(g.description), '\\s+', ' ', 'g') || ' ' ||
                           regexp_replace(trim(COALESCE(g.program_name, '')), '\\s+', ' ', 'g')), 16) = ?
        ORDER BY g.fiscal_year DESC NULLS LAST, g.amount_cad DESC NULLS LAST
    """, [text_hash[:16]]).fetchall()
    cols = ["grant_id", "source_dataset", "description", "program_name", "amount_cad", "fiscal_year",
            "funder_id", "funder_name", "recipient_id", "recipient_name", "source_ref"]
    return [dict(zip(cols, r)) for r in rows]


# ── HTML rendering ───────────────────────────────────────────────────────────

def render_grant_list_table(grants, live=False):
    """Funder | Recipient | Amount | Year table for every grant sharing one
    text -- unlike org_page.py's render_grants_table, neither column is a
    fixed 'this org' anchor, so both funder and recipient vary per row. Same
    SCALE_CAP embed limit + VISIBLE_ROWS collapse-then-show-more pattern as
    the org pages, reusing the identical CSS classes/JS (toggleMoreRows) so
    it behaves the same way without re-implementing it.

    live=False (the static-page default) links org names to
    '../orgs/<slug>.html', correct when this page is itself a static file
    under docs/grants/. live=True (webapp.py's route) drops the '.html' --
    the live app's routes are extensionless (/orgs/<slug>), and the '.html'
    suffix 404s there. Confirmed broken in exactly this way: a user reported
    clicking an org-name link from a live grant-detail page (e.g.
    /grants/37350e1e7360081f) landed on a dead /orgs/<slug>.html URL --
    org_page.py already solved the identical live-vs-static split for its
    own org-to-org links via its lazy_receipts parameter; this mirrors that
    convention rather than inventing a new one."""
    if not grants:
        return ""
    total_n = len(grants)
    capped = grants[:SCALE_CAP]
    rollup_note = ""
    if total_n > SCALE_CAP:
        rest = grants[SCALE_CAP:]
        rest_total = sum(g["amount_cad"] or 0 for g in rest)
        rollup_note = (f"<p class='rollup-note'>Showing the {SCALE_CAP} largest of {total_n:,} grants; "
                        f"totals above include all of them. The remaining {len(rest):,} grants total "
                        f"{fmt_money(rest_total)}.</p>")

    rows = []
    for g in capped:
        year_label = f"FY {g['fiscal_year']}" if g["fiscal_year"] is not None else "Year unknown"
        # C1: per-row link to the official open.canada.ca record, alongside
        # (not replacing) the existing org-page ↗ links -- omitted silently
        # when the row has no source_ref (only federal_gc rows carry one).
        official_link = ""
        source_ref = g.get("source_ref")
        if source_ref and "|" in source_ref:
            owner_org, ref_number = source_ref.split("|", 1)
            oc_url = open_canada_record_url(owner_org, ref_number)
            if oc_url:
                official_link = (f" <a class='ext' href='{esc(oc_url)}' target='_blank' "
                                 f"rel='noopener noreferrer' title='View official record on open.canada.ca'>&#8599;</a>")
        ext = "" if live else ".html"
        rows.append(
            f"<tr><td>{esc(english_name(g['funder_name']))}"
            f" <a class='orglink' href='../orgs/{esc(slug_for(g['funder_name']))}{ext}' "
            f"title='View organization page'>&#8599;</a></td>"
            f"<td>{esc(english_name(g['recipient_name']))}"
            f" <a class='orglink' href='../orgs/{esc(slug_for(g['recipient_name']))}{ext}' "
            f"title='View organization page'>&#8599;</a></td>"
            f"<td class='num'>{esc(fmt_money_precise(g['amount_cad']))}</td>"
            f"<td>{esc(year_label)}{official_link}</td></tr>"
        )

    visible = rows[:VISIBLE_ROWS]
    extra = rows[VISIBLE_ROWS:]
    table_html = (f"<div class='table-scroll'><table><thead><tr><th>Funder</th><th>Recipient</th>"
                  f"<th class='num'>Amount</th><th>Fiscal year</th></tr></thead>"
                  f"<tbody>{''.join(visible)}</tbody>")
    if extra:
        n_extra = len(extra)
        show_label = f"Show {n_extra:,} more grant{'s' if n_extra != 1 else ''}"
        table_html += f"<tbody class='more-rows' id='more-grants'>{''.join(extra)}</tbody>"
        table_html += (f"<tfoot><tr><td colspan='4'><span class='show-more' data-target='more-grants' "
                        f"data-show='{esc(show_label)}' data-hide='Show fewer' onclick='toggleMoreRows(this)'>"
                        f"{esc(show_label)} ▼</span></td></tr></tfoot>")
    table_html += "</table></div>"
    return table_html + rollup_note


def render_grant_detail_page(con, text_hash, live=False):
    grants = fetch_grants_for_text(con, text_hash)
    if not grants:
        print(f"ERROR: no grants found for text_hash={text_hash}", file=sys.stderr)
        sys.exit(1)
    return render_grant_detail_page_from_grants(grants, live=live)


def render_grant_detail_page_from_grants(grants, live=False):
    """Pure rendering half of render_grant_detail_page, split out so batch
    generation (build_all_detail_pages) can pass in grants already fetched
    and grouped by one bulk query instead of each page re-querying
    fetch_grants_for_text's full-table hash scan (measured: ~0.6s/text,
    which is ~7 hours for 40,000 texts one at a time vs. ~4s for one bulk
    fetch of every relevant row, grouped in Python by hash).

    live=False (default) renders links for the static docs/grants/*.html
    convention; live=True (webapp.py's /grants/<hash> route) renders the
    live app's extensionless routes instead -- see render_grant_list_table's
    docstring for the confirmed-broken symptom this fixes (org-name links
    404ing when this page is served live)."""
    first = grants[0]
    full_text = first["description"] or ""
    program_name = first["program_name"]
    source = first["source_dataset"]
    total_amount = sum(g["amount_cad"] or 0 for g in grants)
    years = sorted({g["fiscal_year"] for g in grants if g["fiscal_year"] is not None})
    n_funders = len({g["funder_id"] for g in grants})
    n_recipients = len({g["recipient_id"] for g in grants})

    stats = [
        (fmt_money(total_amount), "total awarded"),
        (fmt_int(len(grants)), "grants"),
        (fmt_int(n_funders), "funder" + ("s" if n_funders != 1 else "")),
        (fmt_int(n_recipients), "recipient" + ("s" if n_recipients != 1 else "")),
    ]
    if years:
        stats.append((f"{years[0]}–{years[-1]}", "years active"))
    stat_html = "".join(f"<div class='stat'><b>{esc(l)}</b><span>{esc(s)}</span></div>" for l, s in stats)

    table_html = render_grant_list_table(grants, live=live)
    generated = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")
    grants_index_href = "/grants" if live else "index.html"
    orgs_index_href = "/orgs" if live else "../orgs/index.html"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>[DRAFT] {esc(program_name or 'Grant')} — Canadian Nonprofit Data</title>
<style>{CSS}</style></head><body>
<div class="draft-banner">{esc(DRAFT_BANNER_TEXT)}</div>
<div class="draft-watermark">DRAFT</div>
<div class="wrap">
<header>
<div>
<h1>{esc(program_name) if program_name else 'Grant'} <span class="badge">{esc(SOURCE_LABELS.get(source, source))}</span></h1>
<p class="meta-line">{esc(full_text)}</p>
</div>
</header>
<div class='stats'>{stat_html}</div>
<h2>Grants sharing this text</h2>
{table_html}
<footer><p class="draft-footer-notice">{esc(DRAFT_FULL_TEXT)}</p>
Generated {esc(generated)} from the Canadian Nonprofit Data entity graph.
&middot; <a href="{grants_index_href}">&larr; Search grant text</a>
&middot; <a href="{orgs_index_href}">Search organizations</a></footer>
</div>
<script>{JS}</script>
</body></html>"""


def render_grant_index_page(records, total_count):
    import json as _json

    out_records = [
        {
            "t": r["text"],
            "p": r["program_name"] or "",
            "h": r["text_hash"][:16],
            "src": r["source_dataset"],
            "n": r["n"],
            "amt": fmt_money(r["total_amount"]),
            "y": f"{r['min_year']}–{r['max_year']}" if r["min_year"] is not None else "",
        }
        for r in records
    ]
    data_json = _json.dumps(out_records, ensure_ascii=False)

    if total_count > len(records):
        meta_text = (f"Showing the top {len(records):,} of {total_count:,} distinct grant program/description "
                     f"texts -- ranked by total dollar amount, largest first. Search only runs over these "
                     f"{len(records):,}.")
    else:
        meta_text = f"{len(records):,} distinct grant program/description texts (Federal G&C, Ontario Trillium Foundation)"

    source_boxes = "".join(
        f"<label><input type='checkbox' data-src='{esc(s)}'> {esc(SOURCE_LABELS.get(s, s))}</label>"
        for s in GRANT_TEXT_SOURCES
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Search grant text — Canadian Nonprofit Data</title>
<style>{CSS}
.search-box{{width:100%;font-size:1.1rem;padding:14px 16px;border:1px solid var(--line);border-radius:10px;margin-top:20px}}
.filters{{display:flex;gap:28px;flex-wrap:wrap;margin-top:16px;padding:14px 16px;background:var(--card);border:1px solid var(--line);border-radius:10px}}
.filters label{{display:flex;align-items:center;gap:7px;cursor:pointer;font-size:.85rem}}
.results{{margin-top:18px}}
.result{{display:block;padding:12px 14px;border:1px solid var(--line);border-radius:8px;margin-bottom:8px;background:var(--card);text-decoration:none;color:var(--ink)}}
.result:hover{{border-color:var(--red)}}
.result b{{display:block}}
.result p{{color:var(--mut);font-size:.82rem;margin:4px 0}}
.result span{{color:var(--mut);font-size:.82rem}}
.count{{color:var(--mut);font-size:.85rem;margin-top:10px}}
</style></head><body>
<div class="wrap">
<header><div><h1>Search grant text</h1>
<p class="meta-line">{esc(meta_text)}</p></div></header>
<input class="search-box" id="q" type="text" placeholder="Search grant program names and descriptions...">
<div class="filters">{source_boxes}</div>
<div class="count" id="count"></div>
<div class="results" id="results"></div>
</div>
<script>
const DATA = {data_json};
const q = document.getElementById('q');
const results = document.getElementById('results');
const count = document.getElementById('count');
const checkboxes = Array.from(document.querySelectorAll('.filters input[type=checkbox]'));
function render(list) {{
  results.innerHTML = list.slice(0, 50).map(r =>
    `<a class="result" href="grant-${{r.h}}.html"><b>${{r.p || '(no program name)'}}</b>` +
    `<p>${{r.t}}</p>` +
    `<span>${{r.n.toLocaleString()}} grant${{r.n === 1 ? '' : 's'}} · ${{r.amt}} · ${{r.y}}</span></a>`
  ).join('');
}}
function search() {{
  const term = q.value.trim().toLowerCase();
  const activeSrcs = checkboxes.filter(cb => cb.checked).map(cb => cb.dataset.src);
  if (!term && !activeSrcs.length) {{ count.textContent = ''; results.innerHTML = ''; return; }}
  let matches = DATA;
  if (term) matches = matches.filter(r => r.t.toLowerCase().includes(term) || r.p.toLowerCase().includes(term));
  if (activeSrcs.length) matches = matches.filter(r => activeSrcs.includes(r.src));
  count.textContent = matches.length.toLocaleString() + ' match' + (matches.length === 1 ? '' : 'es') +
    (matches.length > 50 ? ' (showing first 50)' : '');
  render(matches);
}}
q.addEventListener('input', search);
checkboxes.forEach(cb => cb.addEventListener('change', search));
</script>
</body></html>"""


# ── batch / CLI ──────────────────────────────────────────────────────────────

def build_search_index(db_path, out_dir=None, limit=None):
    if limit is None:
        limit = DEFAULT_GRANT_INDEX_LIMIT
    out_dir = out_dir or GRANTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    con = open_db(db_path)
    try:
        records = fetch_distinct_grant_texts(con, limit=limit)
        total_count = count_distinct_grant_texts(con)
        index_path = os.path.join(out_dir, "index.html")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(render_grant_index_page(records, total_count))
    finally:
        con.close()
    print(f"Wrote grant-text search index over {len(records):,} of {total_count:,} texts -> {index_path}")
    return len(records), index_path


def fetch_all_grants_grouped_by_hash(con):
    """Every grant row from GRANT_TEXT_SOURCES, with text_hash computed once
    per row, grouped in Python into {hash16: [grants...]}. One full-table
    scan (~3.5s over the real ~1.01M qualifying rows) instead of one scan per
    text -- see build_all_detail_pages' docstring for why the per-text
    version (fetch_grants_for_text, fine for a single on-demand lookup)
    doesn't scale to batch generation."""
    sources_sql = ", ".join(f"'{s}'" for s in GRANT_TEXT_SOURCES)
    rows = con.execute(f"""
        SELECT LEFT(sha256(regexp_replace(trim(g.description), '\\s+', ' ', 'g') || ' ' ||
                            regexp_replace(trim(COALESCE(g.program_name, '')), '\\s+', ' ', 'g')), 16) AS h,
               g.grant_id, g.source_dataset, g.description, g.program_name, g.amount_cad, g.fiscal_year,
               f.entity_id AS funder_id, f.canonical_name AS funder_name,
               r.entity_id AS recipient_id, r.canonical_name AS recipient_name, g.source_ref
        FROM grants_unified g
        JOIN entities f ON f.entity_id = g.funder_entity_id
        JOIN entities r ON r.entity_id = g.recipient_entity_id
        WHERE g.source_dataset IN ({sources_sql}) AND g.description IS NOT NULL
        ORDER BY g.fiscal_year DESC NULLS LAST, g.amount_cad DESC NULLS LAST
    """).fetchall()
    grouped = {}
    for row in rows:
        h = row[0]
        g = {
            "grant_id": row[1], "source_dataset": row[2], "description": row[3], "program_name": row[4],
            "amount_cad": row[5], "fiscal_year": row[6], "funder_id": row[7], "funder_name": row[8],
            "recipient_id": row[9], "recipient_name": row[10], "source_ref": row[11],
        }
        grouped.setdefault(h, []).append(g)
    return grouped


def build_all_detail_pages(db_path, out_dir=None, limit=None):
    """Batch-generate a detail page per distinct text (capped like --index by
    default). Unlike org_page.py's per-entity pages, this needs no per-row
    receipt-lookup query -- funder/recipient/amount/year are already sitting
    in grants_unified -- so one bulk fetch (fetch_all_grants_grouped_by_hash)
    covers every page's data in a single scan, rather than one scan per page."""
    if limit is None:
        limit = DEFAULT_GRANT_INDEX_LIMIT
    out_dir = out_dir or GRANTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    con = open_db(db_path)
    try:
        records = fetch_distinct_grant_texts(con, limit=limit)
        grouped = fetch_all_grants_grouped_by_hash(con)
        print(f"Generating {len(records):,} grant-text detail pages ...")
        n_written = 0
        for i, r in enumerate(records, 1):
            h = r["text_hash"][:16]
            grants = grouped.get(h)
            if not grants:
                continue  # shouldn't happen -- fetch_distinct_grant_texts and this query use the same hash expression
            page = render_grant_detail_page_from_grants(grants)
            out_path = os.path.join(out_dir, f"grant-{h}.html")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(page)
            n_written += 1
            if i % 5000 == 0:
                print(f"  ... {i:,}/{len(records):,}")
    finally:
        con.close()
    print(f"Wrote {n_written:,} detail pages to {out_dir}")
    return n_written


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search grant program/description text.")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="path to nonprofit_network.duckdb")
    parser.add_argument("--index", action="store_true", help="build docs/grants/index.html search page")
    parser.add_argument("--text-hash", help="build one detail page for this text_hash (first 16 hex chars)")
    parser.add_argument("--all", action="store_true",
                         help="batch-generate detail pages for every indexed text (capped like --index)")
    parser.add_argument("--limit", type=int,
                         help=f"cap to top N texts by total amount (default {DEFAULT_GRANT_INDEX_LIMIT:,})")
    parser.add_argument("--out-dir", help="output directory (default docs/grants)")
    args = parser.parse_args(argv)

    if args.index:
        build_search_index(args.db, out_dir=args.out_dir, limit=args.limit)
        return
    if args.all:
        build_all_detail_pages(args.db, out_dir=args.out_dir, limit=args.limit)
        return
    if args.text_hash:
        con = open_db(args.db)
        try:
            page = render_grant_detail_page(con, args.text_hash)
        finally:
            con.close()
        out_dir = args.out_dir or GRANTS_DIR
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"grant-{args.text_hash}.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(page)
        print(f"Wrote {out_path}")
        return

    parser.error("provide --index, --text-hash <hash>, or --all")


if __name__ == "__main__":
    main()
