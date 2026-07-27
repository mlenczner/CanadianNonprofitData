"""
Organization Profile Page Generator ("claim and receipt")

Generates one self-contained HTML profile page per organization from
nonprofit_network.duckdb. See docs/org-page-spec.md for the full spec.

Two-layer design:
  1. The clean layer (default) -- a quiet, elegant profile: name, stats,
     a funding timeline, and grants received/given. No jargon, no IDs.
  2. The receipt layer -- every fact on the page is a *claim*, and every
     claim has a *receipt*: the raw record(s) it came from, how they were
     matched, and with what confidence (entity_links' audit trail, plus a
     best-effort lookup into the raw source tables for individual grant
     rows). Claims are marked with a dotted underline when the header's
     "Show your work" toggle is on; clicking one (with or without the
     toggle on) opens its evidence drawer. Drawers are pre-rendered into
     the page (hidden until clicked) -- no fetches, so the file stays
     fully self-contained and works offline.

Run with:
    python analysis/org_page.py "Salvation Army"            # fuzzy name lookup
    python analysis/org_page.py --entity-id 12345
    python analysis/org_page.py --bn 107951618
    python analysis/org_page.py "salvation" --list           # list candidates, build nothing
    python analysis/org_page.py "Salvation Army" --out PATH  # override output path

Respects AGENTS.md: never reads grants.csv or the T3010 CSVs directly --
everything comes from DuckDB queries against nonprofit_network.duckdb.
"""

import argparse
import html
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from urllib.parse import quote

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analysis.build_entity_graph import fiscal_year_from_date  # noqa: E402

DEFAULT_DB_PATH = os.path.join(ROOT, "nonprofit_network.duckdb")
ORGS_DIR = os.path.join(ROOT, "docs", "orgs")
DISCOVERY_OUTPUT_DIR = os.path.join(ROOT, "discovery", "output")

# discovery/run.py and discovery/run_cc.py's own "confirmed not a registered
# charity" wording (see AGENTS.md's discovery sections) -- kept in sync here
# rather than re-deriving from the raw charity_status string, since a badge
# is user-facing copy, not a data value.
DISCOVERY_BADGE_LABELS = {
    "req": "Confirmed Quebec nonprofit — not a registered charity",
    "corporations_canada": "Confirmed federally-incorporated nonprofit — not a registered charity",
}

SCALE_CAP = 300
VISIBLE_ROWS = 30  # default rows shown per grants subsection before a "show more" toggle
DEFAULT_INDEX_LIMIT = 50_000  # default cap for build_search_index -- see its docstring
AMOUNT_EPSILON = 0.01


# ── official-source deep links (Part C) ─────────────────────────────────────
# Every claim's receipt should end in a link to the government's own copy
# when one exists online, alongside (never instead of) our own matched raw
# record -- see render_grant_receipt / render_identity_receipt / render_page
# for where each of these gets called, and .ext in CSS for the shared
# external-link styling.

def open_canada_record_url(owner_org, ref_number):
    """search.open.canada.ca's record page. Confirmed by hand against a real
    browser session (2026-07-18): the URL needs a THIRD, trailing ",current"
    segment that a naive two-part {owner_org},{ref_number} guess doesn't --
    without it the page renders an empty shell with no record data.
    Confirmed working: search.open.canada.ca/grants/record/
    wd-deo,GC-WD-DEO-2021-2022-Q1-704,current loads the real record; the
    same two-segment URL without ",current" does not."""
    if not owner_org or not ref_number:
        return None
    # safe='' -- some ref_numbers contain literal slashes (spec's own note),
    # which quote()'s default safe='/' would leave unescaped, looking like an
    # extra path segment rather than part of the ref value.
    return (f"https://search.open.canada.ca/grants/record/"
            f"{quote(owner_org, safe='')},{quote(ref_number, safe='')},current")


def cra_charity_url(bn_full):
    """CONFIRMED BROKEN, kept only for reference -- do not use to build a
    live link. apps.cra-arc.gc.ca/ebci/hacc/srch/pub/dsplyBscInf?
    selectedCharityBn=... returns a redirect-error page, reproduced twice:
    once from this environment, and again from a real user's own browser
    session (2026-07-18) -- so this isn't automated-traffic blocking, the
    URL pattern itself is wrong or the endpoint no longer exists. Canada.ca
    itself (the informational page one level up, not the apps.cra-arc.gc.ca
    tool) also rendered blank in every attempt from this environment, so the
    correct direct-search-tool URL couldn't be independently rediscovered
    either. render_cra_link_html() below is what's actually used -- the BN
    shown prominently plus a link to the general List of Charities page,
    the same safe-fallback pattern already used for REQ (whose own record
    pages are also not stably deep-linkable)."""
    if not bn_full:
        return None
    return f"https://apps.cra-arc.gc.ca/ebci/hacc/srch/pub/dsplyBscInf?selectedCharityBn={quote(bn_full)}&dsrdPg=1"


CRA_CHARITY_SEARCH_URL = "https://www.canada.ca/en/revenue-agency/services/charities-giving/list-charities.html"


def render_cra_link_html(bn_full, label="BN"):
    """Shared inline markup for all three CRA-link call sites (org page
    header, identity receipt drawer, T3010 funder receipt) -- BN shown
    prominently with a copy button, next to a link to the CRA's general
    List of Charities page, since the guessed direct-record URL is
    confirmed broken (see cra_charity_url()'s docstring). Returns "" (not
    None) when there's no BN, so callers can always concatenate the
    result. Deliberately returns bare inline content, no wrapping <p> --
    the header call site needs to fit inside an already-open <p
    class="meta-line">, so wrapping is left to each caller."""
    if not bn_full:
        return ""
    return (f"{esc(label)} <code>{esc(bn_full)}</code> "
            f"<button type='button' class='copy-btn' data-copy='{esc(bn_full)}' "
            f"onclick='copyToClipboard(this)'>&#10697;</button> — "
            f"<a class='ext' href='{esc(CRA_CHARITY_SEARCH_URL)}' target='_blank' rel='noopener noreferrer'>"
            f"look up in the CRA List of Charities &#8599;</a>")


def corporations_canada_url(corp_number):
    """Confirmed working against a real corporation record (2026-07-18):
    corpId=456926 loaded The Huntsman Marine Science Centre's real federal
    corporation page, matching this exact pattern."""
    if not corp_number:
        return None
    return f"https://ised-isde.canada.ca/cc/lgcy/fdrlCrpDtls.html?corpId={quote(str(corp_number))}"


# REQ's état-de-renseignements pages are session-based, not stably
# deep-linkable (same conclusion the spec already reached, and consistent
# with what was observed here: the REQ search entry point served a
# Cloudflare bot-check interstitial to this automated environment, the same
# class of block CRA's app showed -- not attempted to bypass). NEQ is
# rendered prominently instead, with a copy button, next to this fallback
# search-page link.
REQ_SEARCH_URL = "https://www.registreentreprises.gouv.qc.ca/en/"

# C5: every federal grant's search.open.canada.ca record page (open_canada_
# record_url) always resolves via owner_org; the unfiltered search itself
# (no confirmed way to pre-filter by department -- search.open.canada.ca is
# a client-side app that didn't reliably reflect a filter query string back
# on page load when tested here, so a guessed filtered-URL pattern isn't
# used) is the fallback every department gets regardless of this dict.
OPEN_CANADA_GRANTS_SEARCH_URL = "https://search.open.canada.ca/grants/"

# owner_org slug (from grants_unified.source_ref's "owner_org|ref_number"
# prefix) -> (department name, canada.ca homepage). Deliberately keyed on
# owner_org, not entities.canonical_name -- AGENTS.md issue #3 documents
# that 81% of federal_dept entities have a canonical_name that's just a
# ref-number fragment ("014", "Q4"), not a usable department name, while
# owner_org is a clean, human-recognizable slug straight from the source
# data. Covers the top 30 departments by federal_gc record count (a real
# query against grants_unified, not a guess) -- a department outside this
# dict still gets the unfiltered open.canada.ca search link, just no
# curated homepage link.
DEPARTMENT_LINKS = {
    "esdc-edsc": ("Employment and Social Development Canada", "https://www.canada.ca/en/employment-social-development.html"),
    "isc-sac": ("Indigenous Services Canada", "https://www.canada.ca/en/indigenous-services-canada.html"),
    "pch": ("Canadian Heritage", "https://www.canada.ca/en/canadian-heritage.html"),
    "nrc-cnrc": ("National Research Council Canada", "https://nrc.canada.ca/en"),
    "sshrc-crsh": ("Social Sciences and Humanities Research Council", "https://www.sshrc-crsh.gc.ca/"),
    "tc": ("Transport Canada", "https://tc.canada.ca/en"),
    "cihr-irsc": ("Canadian Institutes of Health Research", "https://cihr-irsc.gc.ca/e/193.html"),
    "ic": ("Innovation, Science and Economic Development Canada", "https://ised-isde.canada.ca/site/ised/en"),
    "acoa-apeca": ("Atlantic Canada Opportunities Agency", "https://www.canada.ca/en/atlantic-canada-opportunities.html"),
    "aandc-aadnc": ("Crown-Indigenous Relations and Northern Affairs Canada", "https://www.canada.ca/en/crown-indigenous-relations-northern-affairs.html"),
    "cic": ("Immigration, Refugees and Citizenship Canada", "https://www.canada.ca/en/immigration-refugees-citizenship.html"),
    "aafc-aac": ("Agriculture and Agri-Food Canada", "https://agriculture.canada.ca/en"),
    "ced-dec": ("Canada Economic Development for Quebec Regions", "https://ced.canada.ca/en/"),
    "dfo-mpo": ("Fisheries and Oceans Canada", "https://www.dfo-mpo.gc.ca/index-eng.htm"),
    "wd-deo": ("Western Economic Diversification Canada", "https://www.canada.ca/en/western-economic-diversification.html"),
    "dfatd-maecd": ("Global Affairs Canada", "https://www.international.gc.ca/global-affairs-affaires-mondiales/home-accueil.aspx?lang=eng"),
    "nserc-crsng": ("Natural Sciences and Engineering Research Council", "https://www.nserc-crsng.gc.ca/"),
    "nrcan-rncan": ("Natural Resources Canada", "https://natural-resources.canada.ca/home"),
    "ec": ("Environment and Climate Change Canada", "https://www.canada.ca/en/environment-climate-change.html"),
    "cra-arc": ("Canada Revenue Agency", "https://www.canada.ca/en/revenue-agency.html"),
    "phac-aspc": ("Public Health Agency of Canada", "https://www.canada.ca/en/public-health.html"),
    "jus": ("Department of Justice Canada", "https://www.justice.gc.ca/eng/"),
    "feddevontario": ("Federal Economic Development Agency for Southern Ontario", "https://feddevontario.canada.ca/en"),
    "iaac-aeic": ("Impact Assessment Agency of Canada", "https://www.canada.ca/en/impact-assessment-agency.html"),
    "ps-sp": ("Public Safety Canada", "https://www.publicsafety.gc.ca/index-en.aspx"),
    "prairiescan": ("Prairies Economic Development Canada", "https://www.canada.ca/en/prairies-economic-development.html"),
    "pc": ("Parks Canada", "https://parks.canada.ca/index"),
    "hc-sc": ("Health Canada", "https://www.canada.ca/en/health-canada.html"),
    "wage": ("Women and Gender Equality Canada", "https://women-gender-equality.canada.ca/en.html"),
    "infc": ("Infrastructure Canada", "https://housing-infrastructure.canada.ca/index-eng.html"),
}


def fetch_department_owner_org(con, entity_id):
    """A federal_dept entity has no owner_org column of its own -- derive it
    from any grants_unified row it funded (source_ref's "owner_org|
    ref_number" prefix), same key C1's open_canada_record_url() already
    uses. None if the department has no federal_gc-sourced grant on record
    (shouldn't happen for a real federal_dept entity, but not assumed)."""
    row = con.execute("""
        SELECT split_part(source_ref, '|', 1) FROM grants_unified
        WHERE funder_entity_id = ? AND source_ref IS NOT NULL LIMIT 1
    """, [entity_id]).fetchone()
    return row[0] if row else None


RED = "#d52b1e"

DRAFT_BANNER_TEXT = "DRAFT — research prototype, not for circulation"
DRAFT_FULL_TEXT = (
    "DRAFT — research prototype. This is an unreleased working draft produced for "
    "research purposes only. Figures are derived from public data using experimental "
    "methods, contain known data-quality limitations, and have not been reviewed for "
    "publication. Do not cite, circulate, or rely on any figure or claim in this document."
)

KIND_LABELS = {
    "charity": "Registered charity",
    "federal_dept": "Federal department",
    "funder_org": "Organization",
    "other_org": "Organization",
}

SOURCE_LABELS = {
    "federal_gc": "Federal G&C",
    "t3010_qualified_donee": "T3010 gift",
    "t3010_non_qualified_donee": "T3010 gift",
    "canada_council": "Canada Council",
    "otf": "Ontario Trillium Foundation",
}

# Which grants-table subsection each source_dataset falls into. canada_council
# and otf are both government-created granting bodies (a federal Crown
# corporation and an Ontario government agency respectively), not charities
# making discretionary gifts, so they group with federal_gc as "government"
# rather than with either T3010 donee schedule. Qualified vs. non-qualified is
# a real legal distinction on the T3010 (the recipient's own charitable/donee
# status in the *giving* charity's eyes), not just a data-source label, so it
# gets its own subsection rather than being folded into one "charity" bucket.
GRANT_CATEGORY = {
    "federal_gc": "government",
    "canada_council": "government",
    "otf": "government",
    "t3010_qualified_donee": "qualified_donee",
    "t3010_non_qualified_donee": "non_qualified_donee",
}
CATEGORY_ORDER = ("qualified_donee", "non_qualified_donee", "government")

CATEGORY_HEADINGS = {
    ("received", "qualified_donee"): "From other charities & foundations",
    ("received", "non_qualified_donee"): "As a non-qualified donee",
    ("received", "government"): "From government",
    ("given", "qualified_donee"): "To other charities & foundations",
    ("given", "non_qualified_donee"): "To non-qualified donees",
    ("given", "government"): "As government funding",
}

# Plain-language hint shown under each category filter checkbox -- the
# precise term (kept, since it's the real T3010/CRA vocabulary) plus a
# one-line explanation for a visitor who doesn't already know what a
# "qualified donee" or "non-qualified donee" is. Keyed the same way as
# CATEGORY_HEADINGS so both stay in sync from one source of truth.
CATEGORY_HINTS = {
    ("received", "qualified_donee"): "Received gifts from other registered charities or foundations.",
    ("received", "non_qualified_donee"): "Received gifts from a charity despite not being a registered charity itself.",
    ("received", "government"): "Received a federal, Canada Council, or Ontario Trillium Foundation grant.",
    ("given", "qualified_donee"): "Gave gifts to other registered charities or foundations.",
    ("given", "non_qualified_donee"): "Gave gifts to a recipient that is not itself a registered charity.",
    ("given", "government"): "Gave out federal, Canada Council, or Ontario Trillium Foundation funding (i.e. is itself a funder).",
}

# The 7th filter (identity, not a (direction, category) flag -- see
# NON_CHARITY_FILTER_KEY in webapp.py) gets its own hint, not part of the
# dict above since it has no (direction, category) key.
NON_CHARITY_FILTER_HINT = (
    "Independently confirmed as a legally incorporated nonprofit (via Quebec's "
    "Registre des entreprises or Corporations Canada) that is not a registered charity."
)


# ── small helpers ────────────────────────────────────────────────────────────

def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "org"


def slug_for(canonical_name):
    """Same trimming build_page() already applies before slugifying a single
    page's filename (drop a bilingual name's French half via "/", which
    english_name() deliberately doesn't split -- see build_page's comment).
    Pulled out as its own function so batch generation can compute every
    entity's slug up front, before any page is rendered, to detect and
    resolve collisions across entities that would otherwise slugify to the
    same filename."""
    return slugify(english_name(canonical_name).split("/", 1)[0].strip())


def english_name(raw):
    """Display-only: take the English half of a bilingual 'English|Français'
    name, preserving original casing/punctuation (unlike normalize_name(),
    which is for matching, not display)."""
    if raw and "|" in raw:
        return raw.split("|", 1)[0].strip()
    return raw or ""


def fmt_money(v):
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:.{0 if a >= 1e10 else 1}f}B"
    if a >= 1e6:
        return f"${v/1e6:.1f}M"
    return f"${v:,.0f}"


def fmt_money_precise(v):
    if v is None:
        return "—"
    return f"${v:,.2f}" if abs(v) < 1000 else f"${v:,.0f}"


def fmt_int(v):
    return f"{v:,}" if v is not None else "—"


def fmt_pct(identified, declared):
    """% of a T3010-declared line we've matched in grants_unified. "—" when
    declared is None/zero rather than dividing by zero -- and note this
    deliberately doesn't hide a real identified amount just because the
    declared line was blank: a charity can have identified > 0 with no
    usable declared figure (misfiled/blank T3010 line), which is worth
    showing plainly rather than silently dropping the identified bar."""
    if not declared:
        return "—"
    pct = 100 * identified / declared
    return f"{pct:.0f}%" if pct <= 999 else ">999%"


def prorate_agreement_by_fiscal_year(agreement_value, start_date, end_date, month_cutover=4):
    """Split agreement_value evenly across the fiscal years [start_date,
    end_date] spans (inclusive), using fiscal_year_from_date's month_cutover
    convention (default 4, matching build_entity_graph.py's own federal_gc
    attribution). Most federal G&C agreements are multi-year -- dumping the
    full value into the start year alone (today's grants_unified behavior)
    would badly misstate any per-year comparison against a charity's T3010
    revenue, which is recognized across the years money actually arrives.

    Falls back to single-year attribution at start_date's fiscal year if
    end_date is missing/unparseable or precedes start_date (a data-quality
    issue, not something to crash or produce a negative range over). Returns
    {} if start_date itself doesn't parse, or agreement_value is None."""
    if agreement_value is None:
        return {}
    start_fy = fiscal_year_from_date(start_date, month_cutover)
    if start_fy is None:
        return {}
    end_fy = fiscal_year_from_date(end_date, month_cutover)
    if end_fy is None or end_fy < start_fy:
        return {start_fy: agreement_value}
    years = list(range(start_fy, end_fy + 1))
    share = agreement_value / len(years)
    return {y: share for y in years}


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


# ── DB access ────────────────────────────────────────────────────────────────

def open_db(db_path):
    if not os.path.exists(db_path):
        print(f"ERROR: database not found at {db_path}\n"
              f"Run analysis/build_entity_graph.py first.", file=sys.stderr)
        sys.exit(1)
    try:
        return duckdb.connect(db_path, read_only=True)
    except duckdb.IOException as e:
        print(f"ERROR: could not open {db_path} read-only (is it locked by another "
              f"process, e.g. a build in progress?): {e}", file=sys.stderr)
        sys.exit(1)


def load_discovery_index(discovery_dir=None):
    """entity_id -> discovery-match info, for the badge render_page() shows
    on an org page confirming non-charity nonprofit status. Reads discovery/
    output/*_discovery_flagged.csv directly (discovery/run.py, discovery/
    run_cc.py's own output) -- read-only, no dependency on those scripts
    having been re-run recently, and no coupling to nonprofit_network.duckdb's
    schema (the discovery module only ever reads that database, never writes
    back into it -- see AGENTS.md's discovery sections for why).

    Scoped deliberately narrow, not every discovery row: only
    charity_status == "non_charity_nonprofit" AND federal_grant_status ==
    "federal_grant_match" rows have both (a) real information not already on
    the page (an `other_org` entity's KIND_LABELS badge just says generic
    "Organization" -- confirming *which* real nonprofit it is, and that it's
    confirmed not a charity, is new) and (b) an actual `entity_id` to attach
    to (matched_grant_entity_id) -- a discovery record with no confirmed
    federal grant link was never resolved into `entities` at all, so there's
    no existing page to enrich. needs_review rows are excluded on purpose:
    this is a live, public page, not an internal review queue -- only
    auto-accepted matches are shown as fact.

    A registered_charity discovery match isn't included either: that
    entity's own entity_kind is already 'charity' (KIND_LABELS already shows
    "Registered charity"), so a discovery-source annotation there is
    lower-value than the non-charity case and left out of this first pass.

    A real, confirmed overlap exists between the two sources: 235 entities
    are independently confirmed by both REQ and Corporations Canada (a
    federally-incorporated nonprofit that also registers to operate in
    Quebec, most likely). Each entity_id is stored once (never double-
    counted), but `discovery_sources` keeps the full set of confirming
    registries -- an earlier version let whichever source loaded second
    silently overwrite the first entirely, undercounting that source's true
    reach in fetch_discovery_summary()'s by-registry breakdown even though
    the deduplicated total was already correct. `discovery_source` (singular)
    stays as the first-confirmed registry, used for the org-page badge,
    which shows one source, not an exhaustive list.

    Returns {} (not an error) if the discovery output files don't exist yet
    -- the badge just doesn't show, exactly like an entity with no financials
    just skips that stat."""
    import csv
    index = {}
    sources = [
        ("quebec_discovery_flagged.csv", "req"),
        ("corporations_canada_discovery_flagged.csv", "corporations_canada"),
    ]
    for filename, source_label in sources:
        path = os.path.join(discovery_dir or DISCOVERY_OUTPUT_DIR, filename)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["charity_status"] != "non_charity_nonprofit":
                    continue
                if row["federal_grant_status"] != "federal_grant_match":
                    continue
                eid = int(row["matched_grant_entity_id"])
                if eid in index:
                    index[eid]["discovery_sources"].add(source_label)
                    continue
                index[eid] = {
                    "discovery_source": source_label,
                    "discovery_sources": {source_label},
                    "legal_name": row["legal_name"],
                    "jurisdiction": row["jurisdiction"],
                    "matched_grant_entity_name": row["matched_grant_entity_name"],
                    # C3/C4: source_id is the NEQ (req) or federal corporation
                    # number (corporations_canada) -- the key each registry's
                    # own lookup/display needs. Only ever present for entities
                    # already scoped to this index (see the docstring above),
                    # so no additional filtering needed here.
                    "source_id": row["source_id"],
                }
    return index


def fetch_discovery_summary(con, discovery_index, top_n=10):
    """Aggregate stats for the confirmed non-charity nonprofit set (see
    load_discovery_index()): total orgs, total dollars received, a split by
    discovery_source, and the top N by total flow (for use as recognizable
    examples on a summary page). Computed live against entity_role_summary
    rather than trusting a dollar figure cached in the CSV, so this stays
    accurate to whatever nonprofit_network.duckdb snapshot is actually
    loaded, independent of when discovery/ was last run.

    total_orgs/total_dollars are deduplicated (each entity counted once,
    even if confirmed by both registries), but by_source counts an entity
    under EVERY registry that confirmed it (discovery_index["discovery_
    sources"]) -- so by_source's counts/dollars can sum to more than the
    total, by exactly the confirmed-by-both overlap (235 entities in a real
    run). That's intentional, not a bug: "how many did each registry
    confirm" is a different question than "how many distinct organizations
    total," and collapsing a dual-confirmed entity into only one registry's
    bucket would understate that registry's real reach -- confirmed the hard
    way: an earlier version keyed by discovery_index[eid]["discovery_source"]
    (singular, whichever source loaded second silently won), undercounting
    REQ's true by-source count by exactly that overlap."""
    if not discovery_index:
        return {"total_orgs": 0, "total_dollars": 0.0, "by_source": {}, "top_examples": []}
    ids_sql = ",".join(str(eid) for eid in discovery_index)
    rows = con.execute(f"""
        SELECT e.entity_id, e.canonical_name, e.city, e.province, COALESCE(s.total_received, 0) AS total_received
        FROM entities e JOIN entity_role_summary s ON s.entity_id = e.entity_id
        WHERE e.entity_id IN ({ids_sql})
    """).fetchall()

    total_dollars = 0.0
    by_source = {}
    examples = []
    for eid, name, city, province, total_received in rows:
        total_dollars += total_received or 0
        for src in discovery_index[eid]["discovery_sources"]:
            bucket = by_source.setdefault(src, {"count": 0, "dollars": 0.0})
            bucket["count"] += 1
            bucket["dollars"] += total_received or 0
        examples.append({
            "entity_id": eid, "canonical_name": name, "city": city, "province": province,
            "total_received": total_received or 0,
        })
    examples.sort(key=lambda r: -r["total_received"])
    return {
        "total_orgs": len(rows),
        "total_dollars": total_dollars,
        "by_source": by_source,
        "top_examples": examples[:top_n],
    }


def fetch_regranting_network(con, top_n_intermediaries=8, top_n_edges=4):
    """Top N `dual_role` entities (by total flow: entities that are
    significant funders AND significant recipients, not overwhelmingly one
    or the other -- see entity_role_summary's role classification) plus
    their top M funders and top M recipients each, for a funder ->
    intermediary -> recipient flow diagram. This is a structural pattern no
    single org page shows: money passing *through* a regranting
    intermediary on its way to smaller downstream nonprofits.

    Self-loops (funder_entity_id == recipient_entity_id) are excluded --
    confirmed real in the data, not a hypothetical: The Salvation Army's own
    entity is its own largest nominal "funder" and "recipient" ($900.5M),
    almost certainly an internal-transfer/allocation artifact rather than a
    real external flow, and a node can't meaningfully flow into itself on a
    network diagram regardless of cause.

    The long tail beyond top M funders/recipients per intermediary is
    collapsed into a single "N other funders"/"N other recipients" node (own
    node per intermediary, not shared across them -- each one represents a
    different underlying set of organizations), so the diagram stays legible
    instead of growing without bound as top_n_intermediaries increases.

    A funder or recipient that appears for more than one intermediary (e.g.
    a federal department funding several different regranting charities)
    becomes a single shared node with multiple links -- deliberate, not
    deduplicated away, since that convergence is itself part of the
    structural picture.

    Returns (nodes, links): nodes is {key: {"name", "column", "entity_id"}},
    links is [(source_key, target_key, amount), ...]. key is either
    ("fund"|"mid"|"recv", entity_id) for a real entity, or
    ("fund_other"|"recv_other", intermediary_entity_id) for a collapsed
    long-tail bucket (entity_id None, not a real page to link to)."""
    intermediaries = con.execute(f"""
        SELECT e.entity_id, e.canonical_name, s.total_given, s.total_received
        FROM entities e JOIN entity_role_summary s ON s.entity_id = e.entity_id
        WHERE s.role = 'dual_role'
        ORDER BY s.total_given + s.total_received DESC
        LIMIT {int(top_n_intermediaries)}
    """).fetchall()

    nodes = {}
    links = []
    for eid, name, _total_given, _total_received in intermediaries:
        nodes[("mid", eid)] = {"name": name, "column": "intermediary", "entity_id": eid}

        funder_rows = con.execute("""
            SELECT f.entity_id, f.canonical_name, SUM(g.amount_cad) AS total
            FROM grants_unified g JOIN entities f ON f.entity_id = g.funder_entity_id
            WHERE g.recipient_entity_id = ? AND g.funder_entity_id != g.recipient_entity_id
            GROUP BY f.entity_id, f.canonical_name ORDER BY total DESC
        """, [eid]).fetchall()
        for feid, fname, amt in funder_rows[:top_n_edges]:
            nodes[("fund", feid)] = {"name": fname, "column": "funder", "entity_id": feid}
            links.append((("fund", feid), ("mid", eid), amt or 0))
        rest = funder_rows[top_n_edges:]
        if rest:
            key = ("fund_other", eid)
            nodes[key] = {"name": f"{len(rest)} other funders", "column": "funder", "entity_id": None}
            links.append((key, ("mid", eid), sum(r[2] or 0 for r in rest)))

        recipient_rows = con.execute("""
            SELECT r.entity_id, r.canonical_name, SUM(g.amount_cad) AS total
            FROM grants_unified g JOIN entities r ON r.entity_id = g.recipient_entity_id
            WHERE g.funder_entity_id = ? AND g.funder_entity_id != g.recipient_entity_id
            GROUP BY r.entity_id, r.canonical_name ORDER BY total DESC
        """, [eid]).fetchall()
        for reid, rname, amt in recipient_rows[:top_n_edges]:
            nodes[("recv", reid)] = {"name": rname, "column": "recipient", "entity_id": reid}
            links.append((("mid", eid), ("recv", reid), amt or 0))
        rest = recipient_rows[top_n_edges:]
        if rest:
            key = ("recv_other", eid)
            nodes[key] = {"name": f"{len(rest)} other recipients", "column": "recipient", "entity_id": None}
            links.append((("mid", eid), key, sum(r[2] or 0 for r in rest)))

    return nodes, links


def _layout_sankey_columns(nodes, links, height, node_gap=6):
    """Stack each column's nodes vertically, height proportional to each
    node's total flow (sum of every link touching it) relative to its
    column's total -- standard Sankey column layout. Returns
    {key: {"y0", "y1"}} plus {key: total_flow}."""
    node_total = {key: 0.0 for key in nodes}
    for src, tgt, amt in links:
        node_total[src] += amt
        node_total[tgt] += amt

    columns = {"funder": [], "intermediary": [], "recipient": []}
    for key, info in nodes.items():
        columns[info["column"]].append(key)
    for col in columns.values():
        col.sort(key=lambda k: -node_total[k])

    positions = {}
    for keys in columns.values():
        col_total = sum(node_total[k] for k in keys) or 1
        available = max(height - node_gap * max(len(keys) - 1, 0), 1)
        y = 0.0
        for k in keys:
            h = max(3.0, (node_total[k] / col_total) * available)
            positions[k] = {"y0": y, "y1": y + h}
            y += h + node_gap
    return positions, node_total


def _sankey_link_geometry(positions, node_total, links):
    """For every link, the y-sub-range it occupies within its source and
    target nodes' slots -- links touching the same node stack in the order
    given, proportional to each link's share of that node's total. Standard
    Sankey ribbon placement."""
    out_offset = {key: 0.0 for key in positions}
    in_offset = {key: 0.0 for key in positions}
    geometry = []
    for src, tgt, amt in links:
        src_pos, tgt_pos = positions[src], positions[tgt]
        src_h = (src_pos["y1"] - src_pos["y0"]) * (amt / (node_total[src] or 1))
        tgt_h = (tgt_pos["y1"] - tgt_pos["y0"]) * (amt / (node_total[tgt] or 1))
        sy0 = src_pos["y0"] + out_offset[src]
        ty0 = tgt_pos["y0"] + in_offset[tgt]
        out_offset[src] += src_h
        in_offset[tgt] += tgt_h
        geometry.append({"src": src, "tgt": tgt, "amount": amt,
                          "sy0": sy0, "sy1": sy0 + src_h, "ty0": ty0, "ty1": ty0 + tgt_h})
    return geometry


SANKEY_COLUMN_LABELS = {"funder": "Funders", "intermediary": "Regranting intermediaries", "recipient": "Recipients"}


def render_regranting_network_svg(nodes, links, link_manifest=None, width=1080, height=1400, node_width=170):
    """Hand-rolled Sankey-style flow diagram -- no charting library, same
    "self-contained HTML, no dependencies" convention render_timeline()
    already follows for the funding-history bars. Three columns (funder /
    intermediary / recipient), node height proportional to flow, links drawn
    as cubic-bezier ribbons whose width is proportional to dollar amount.

    link_manifest (entity_id -> slug, see build_link_manifest()) is used for
    node hrefs rather than calling slug_for(name) directly -- slug_for()
    alone doesn't handle two different entities sharing a name-derived slug
    (build_link_manifest()'s whole job); using it standalone here risked a
    node linking to a different organization's page on a collision."""
    link_manifest = link_manifest or {}
    if not nodes:
        return "<p>No dual-role intermediaries found.</p>"

    col_x = {"funder": 0, "intermediary": (width - node_width) / 2, "recipient": width - node_width}
    positions, node_total = _layout_sankey_columns(nodes, links, height)
    geometry = _sankey_link_geometry(positions, node_total, links)

    parts = [f"<svg viewBox='0 0 {width} {height + 40}' xmlns='http://www.w3.org/2000/svg' "
             f"font-family='inherit' class='sankey'>"]

    for col, label in SANKEY_COLUMN_LABELS.items():
        parts.append(f"<text x='{col_x[col]}' y='16' class='sankey-col-label'>{esc(label)}</text>")

    for geo in geometry:
        x0 = col_x[nodes[geo["src"]]["column"]] + node_width
        x1 = col_x[nodes[geo["tgt"]]["column"]]
        xm = (x0 + x1) / 2
        sy0, sy1, ty0, ty1 = (v + 30 for v in (geo["sy0"], geo["sy1"], geo["ty0"], geo["ty1"]))
        title = f"{nodes[geo['src']]['name']} → {nodes[geo['tgt']]['name']}: {fmt_money(geo['amount'])}"
        parts.append(
            f"<path class='sankey-link' d='M{x0},{sy0} C{xm},{sy0} {xm},{ty0} {x1},{ty0} "
            f"L{x1},{ty1} C{xm},{ty1} {xm},{sy1} {x0},{sy1} Z'><title>{esc(title)}</title></path>"
        )

    for key, info in nodes.items():
        pos = positions[key]
        x = col_x[info["column"]]
        y0, y1 = pos["y0"] + 30, pos["y1"] + 30
        title = f"{info['name']}: {fmt_money(node_total[key])} total flow"
        href_open, href_close = "", ""
        if info["entity_id"] is not None:
            slug = link_manifest.get(info["entity_id"], slug_for(info["name"]))
            href_open, href_close = f"<a href='/orgs/{esc(slug)}' class='sankey-node-link'>", "</a>"
        label = english_name(info["name"])
        if len(label) > 28:
            label = label[:27] + "…"
        label_x = x + node_width + 6 if info["column"] != "recipient" else x - 6
        anchor = "start" if info["column"] != "recipient" else "end"
        parts.append(
            f"{href_open}<rect class='sankey-node sankey-node-{info['column']}' "
            f"x='{x}' y='{y0}' width='{node_width}' height='{max(y1 - y0, 1)}'><title>{esc(title)}</title></rect>"
            f"<text x='{label_x}' y='{(y0 + y1) / 2}' text-anchor='{anchor}' "
            f"dominant-baseline='middle' class='sankey-node-label'>{esc(label)}</text>{href_close}"
        )

    parts.append("</svg>")
    return "".join(parts)


def find_candidates(con, query):
    """Case-insensitive substring match against entities.canonical_name,
    ranked by total flow. Returns (entity_id, canonical_name, entity_kind,
    city, province, total_flow) tuples."""
    return con.execute("""
        SELECT e.entity_id, e.canonical_name, e.entity_kind, e.city, e.province,
               COALESCE(s.total_given, 0) + COALESCE(s.total_received, 0) AS total_flow
        FROM entities e
        LEFT JOIN entity_role_summary s ON s.entity_id = e.entity_id
        WHERE e.canonical_name ILIKE '%' || ? || '%'
        ORDER BY total_flow DESC
    """, [query]).fetchall()


def resolve_entity_id(con, args):
    """Resolve the CLI arguments to a single entity_id, or exit. Never
    guesses silently: an ambiguous name lookup with no exact match prints
    candidates and exits nonzero instead of picking one."""
    if args.entity_id is not None:
        row = con.execute("SELECT entity_id FROM entities WHERE entity_id = ?", [args.entity_id]).fetchone()
        if not row:
            print(f"ERROR: no entity with entity_id={args.entity_id}", file=sys.stderr)
            sys.exit(1)
        return row[0]

    if args.bn is not None:
        bn_root = re.sub(r"[^0-9]", "", str(args.bn))[:9]
        row = con.execute("SELECT entity_id FROM entities WHERE bn_root = ?", [bn_root]).fetchone()
        if not row:
            print(f"ERROR: no entity with BN root {bn_root}", file=sys.stderr)
            sys.exit(1)
        return row[0]

    query = args.name
    candidates = find_candidates(con, query)
    if not candidates:
        print(f"ERROR: no organization matching {query!r}", file=sys.stderr)
        sys.exit(1)

    if args.list:
        print_candidates(candidates, query)
        sys.exit(0)

    if len(candidates) == 1:
        return candidates[0][0]

    exact = [c for c in candidates if english_name(c[1]).strip().lower() == query.strip().lower()]
    if len(exact) == 1:
        return exact[0][0]

    print(f"Multiple organizations match {query!r} — pick one with --entity-id, "
          f"or refine the name:", file=sys.stderr)
    print_candidates(candidates, query, file=sys.stderr)
    sys.exit(1)


def print_candidates(candidates, query, file=None):
    print(f"\nTop {min(10, len(candidates))} of {len(candidates)} matches for {query!r}:\n", file=file)
    for eid, name, kind, city, province, flow in candidates[:10]:
        loc = ", ".join(p for p in (city, province) if p)
        print(f"  entity_id={eid:<8} [{kind:<11}] {english_name(name)[:50]:<50} "
              f"{loc:<20} total flow={fmt_money(flow)}", file=file)
    print(file=file)


def fetch_entity(con, entity_id):
    row = con.execute(
        "SELECT entity_id, bn_root, canonical_name, city, province, entity_kind, bn_full "
        "FROM entities WHERE entity_id = ?",
        [entity_id],
    ).fetchone()
    return dict(zip(
        ["entity_id", "bn_root", "canonical_name", "city", "province", "entity_kind", "bn_full"], row
    ))


def fetch_role_summary(con, entity_id):
    row = con.execute("""
        SELECT total_given, total_received, n_grants_given, n_grants_received, given_share, role
        FROM entity_role_summary WHERE entity_id = ?
    """, [entity_id]).fetchone()
    if not row:
        return {"total_given": 0, "total_received": 0, "n_grants_given": 0,
                "n_grants_received": 0, "given_share": None, "role": "no_flows"}
    return dict(zip(
        ["total_given", "total_received", "n_grants_given", "n_grants_received", "given_share", "role"], row
    ))


def fetch_financials(con, entity_id):
    row = con.execute("""
        SELECT bn_full, fiscal_period_end, total_revenue, total_expenditures,
               total_expenditures_incl_disbursements, total_gifts_to_qualified_donees,
               revenue_from_federal_gov, revenue_from_any_cdn_gov
        FROM entity_financials WHERE entity_id = ?
    """, [entity_id]).fetchone()
    if not row:
        return None
    return dict(zip([
        "bn_full", "fiscal_period_end", "total_revenue", "total_expenditures",
        "total_expenditures_incl_disbursements", "total_gifts_to_qualified_donees",
        "revenue_from_federal_gov", "revenue_from_any_cdn_gov",
    ], row))


def fetch_entity_links(con, entity_id):
    """One row per distinct (raw_name, source_dataset) variant, with a count
    of how many entity_links rows collapsed into it -- large regranters can
    have thousands of raw grant-recipient-name variants for the same org
    (e.g. one per branch/program per fiscal year), and the identity receipt
    lists variants, not individual link rows."""
    rows = con.execute("""
        SELECT source_dataset, raw_name, ANY_VALUE(raw_bn) AS raw_bn,
               ANY_VALUE(match_method) AS match_method, MAX(match_score) AS match_score,
               COUNT(*) AS n_occurrences
        FROM entity_links WHERE entity_id = ?
        GROUP BY source_dataset, raw_name
        ORDER BY n_occurrences DESC, source_dataset, raw_name
    """, [entity_id]).fetchall()
    cols = ["source_dataset", "raw_name", "raw_bn", "match_method", "match_score", "n_occurrences"]
    return [dict(zip(cols, r)) for r in rows]


def fetch_grants(con, entity_id, direction):
    """direction: 'received' (this entity is recipient) or 'given' (funder)."""
    other_col = "funder_entity_id" if direction == "received" else "recipient_entity_id"
    this_col = "recipient_entity_id" if direction == "received" else "funder_entity_id"
    rows = con.execute(f"""
        SELECT g.grant_id, g.fiscal_year, o.canonical_name AS other_name, o.entity_id AS other_entity_id,
               g.program_name, g.description, g.amount_cad, g.source_dataset, g.source_ref
        FROM grants_unified g
        JOIN entities o ON o.entity_id = g.{other_col}
        WHERE g.{this_col} = ?
        ORDER BY g.fiscal_year DESC NULLS LAST, g.amount_cad DESC NULLS LAST
    """, [entity_id]).fetchall()
    cols = ["grant_id", "fiscal_year", "other_name", "other_entity_id", "program_name",
            "description", "amount_cad", "source_dataset", "source_ref"]
    return [dict(zip(cols, r)) for r in rows]


def fetch_timeline(con, entity_id):
    received = con.execute("""
        SELECT fiscal_year, SUM(amount_cad) FROM grants_unified
        WHERE recipient_entity_id = ? AND fiscal_year IS NOT NULL GROUP BY 1
    """, [entity_id]).fetchall()
    given = con.execute("""
        SELECT fiscal_year, SUM(amount_cad) FROM grants_unified
        WHERE funder_entity_id = ? AND fiscal_year IS NOT NULL GROUP BY 1
    """, [entity_id]).fetchall()
    received_by_year = dict(received)
    given_by_year = dict(given)
    years = sorted(set(received_by_year) | set(given_by_year))
    return years, received_by_year, given_by_year


def fetch_federal_gc_prorated_by_year(con, entity_id):
    """Government-received money from federal_gc, pro-rated across each
    agreement's real [start, end] span rather than dumped into its start
    fiscal year (see prorate_agreement_by_fiscal_year). One batched query --
    not a per-row lookup like locate_federal_receipt's (capped, lazy) receipt
    drawer -- since this needs every federal_gc row a recipient ever got
    (can run into the thousands for a large regranter), and this runs on
    every live webapp.py page view, not just batch generation. The LEFT JOIN
    can't fan out rows: _latest_amendment_sql() already guarantees
    raw_grants_latest is unique per (TRIM(owner_org), TRIM(ref_number))."""
    rows = con.execute("""
        SELECT g.amount_cad, g.fiscal_year, g.source_ref,
               TRY_CAST(REPLACE(REPLACE(TRIM(rgl.agreement_value), ',', ''), '$', '') AS DOUBLE) AS raw_value,
               rgl.agreement_start_date, rgl.agreement_end_date
        FROM grants_unified g
        LEFT JOIN raw_grants_latest rgl
          ON g.source_ref = TRIM(rgl.owner_org) || '|' || TRIM(rgl.ref_number)
        WHERE g.recipient_entity_id = ? AND g.source_dataset = 'federal_gc'
    """, [entity_id]).fetchall()
    by_year = defaultdict(float)
    for amount_cad, fiscal_year, source_ref, raw_value, start_date, end_date in rows:
        if source_ref and raw_value is not None:
            for y, amt in prorate_agreement_by_fiscal_year(raw_value, start_date, end_date).items():
                by_year[y] += amt
        elif amount_cad is not None and fiscal_year is not None:
            by_year[fiscal_year] += amount_cad  # no source_ref, or join found nothing -- old attribution
    return dict(by_year)


def fetch_government_identified_by_year(con, entity_id):
    """Matched government money per fiscal year: federal_gc (pro-rated) plus
    canada_council/otf (already single-fiscal-year, no pro-rating needed or
    possible -- neither source carries a date range). Mirrors GRANT_CATEGORY's
    "government" bucket so this stays consistent with the Grants Received
    table's own grouping instead of re-deriving it."""
    by_year = defaultdict(float, fetch_federal_gc_prorated_by_year(con, entity_id))
    for fiscal_year, total in con.execute("""
        SELECT fiscal_year, SUM(amount_cad) FROM grants_unified
        WHERE recipient_entity_id = ? AND fiscal_year IS NOT NULL
          AND source_dataset IN ('canada_council', 'otf')
        GROUP BY 1
    """, [entity_id]).fetchall():
        by_year[fiscal_year] += total
    return dict(by_year)


def fetch_foundation_identified_by_year(con, entity_id):
    """Matched charity-to-charity gifts per fiscal year (GRANT_CATEGORY's
    "qualified_donee" bucket). Already single-fiscal-year each -- derived
    from the *giving* charity's own FPE, not this recipient's -- so no
    pro-rating is needed or possible here."""
    return dict(con.execute("""
        SELECT fiscal_year, SUM(amount_cad) FROM grants_unified
        WHERE recipient_entity_id = ? AND fiscal_year IS NOT NULL
          AND source_dataset = 't3010_qualified_donee'
        GROUP BY 1
    """, [entity_id]).fetchall())


def fetch_declared_by_year(con, entity_id):
    """T3010-declared government/foundation revenue per fiscal year, from
    entity_financials_by_year -- the independent ground truth the identified
    totals above get compared against. A year present here with a NULL
    gov_revenue/foundation_revenue (a blank T3010 line, not a missing filing)
    is treated the same as absent by callers -- see fmt_pct/render_timeline."""
    rows = con.execute("""
        SELECT fiscal_year, gov_revenue, foundation_revenue
        FROM entity_financials_by_year WHERE entity_id = ?
    """, [entity_id]).fetchall()
    gov_declared_by_year = {y: g for y, g, f in rows}
    fdn_declared_by_year = {y: f for y, g, f in rows}
    return gov_declared_by_year, fdn_declared_by_year


# ── receipt lookups (best-effort; say so if ambiguous/not found) ────────────

def locate_federal_receipt(con, entity_id, amount, fiscal_year, source_ref=None):
    """Locate the raw_grants row(s) behind a federal_gc grant.

    If source_ref (TRIM(owner_org)+"|"+TRIM(ref_number), populated at build
    time -- AGENTS.md issue #4) is present, looks it up directly: exact,
    not best-effort. Falls back to the original best-effort join -- entity
    name variants against raw_grants_latest.recipient_legal_name, narrowed by
    amount + fiscal year -- only for a grants_unified row built before this
    column existed (source_ref is None)."""
    if source_ref and "|" in source_ref:
        owner_org, ref_number = source_ref.split("|", 1)
        rows = con.execute("""
            SELECT owner_org, ref_number, recipient_legal_name, agreement_value, agreement_start_date,
                   prog_name_en, description_en
            FROM raw_grants_latest
            WHERE TRIM(owner_org) = TRIM(?) AND TRIM(ref_number) = TRIM(?)
        """, [owner_org, ref_number]).fetchall()
        if len(rows) == 1:
            return _federal_receipt_from_row(con, rows[0])
        return {"found": False, "reason": f"source_ref {source_ref!r} matched {len(rows)} raw rows, expected 1"}

    variants = con.execute("""
        SELECT DISTINCT raw_name FROM entity_links
        WHERE entity_id = ? AND source_dataset = 'federal_gc'
    """, [entity_id]).fetchall()
    variant_names = [v[0] for v in variants]
    if not variant_names:
        return {"found": False, "reason": "no federal_gc name variant on record for this entity"}

    placeholders = ", ".join("?" for _ in variant_names)
    candidates = con.execute(f"""
        SELECT owner_org, ref_number, recipient_legal_name, agreement_value, agreement_start_date,
               prog_name_en, description_en
        FROM raw_grants_latest
        WHERE recipient_legal_name IN ({placeholders})
          AND TRY_CAST(REPLACE(REPLACE(TRIM(agreement_value), ',', ''), '$', '') AS DOUBLE)
              BETWEEN ? AND ?
    """, variant_names + [amount - AMOUNT_EPSILON, amount + AMOUNT_EPSILON]).fetchall()

    # Narrow further by fiscal year (month_cutover=4, matching build_entity_graph.py).
    def fy(date_str):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
            try:
                d = datetime.strptime(str(date_str).strip(), fmt)
                return d.year if d.month >= 4 else d.year - 1
            except Exception:
                continue
        return None

    matches = [c for c in candidates if fy(c[4]) == fiscal_year]
    if not matches:
        matches = candidates  # fall back to amount-only matches if fiscal year didn't narrow cleanly

    if len(matches) != 1:
        return {"found": False, "reason": f"{len(matches)} raw rows matched name+amount+year — not unambiguous"}

    return _federal_receipt_from_row(con, matches[0])


def _federal_receipt_from_row(con, row):
    """Build the found-receipt dict (+ full amendment chain) for a single
    located raw_grants_latest row. Shared by both the exact source_ref path
    and the best-effort fallback path in locate_federal_receipt()."""
    owner_org, ref_number, recipient_name, value, start_date, prog, desc = row
    chain = con.execute("""
        SELECT amendment_number,
               TRY_CAST(REPLACE(REPLACE(TRIM(agreement_value), ',', ''), '$', '') AS DOUBLE)
        FROM raw_grants
        WHERE TRIM(owner_org) = TRIM(?) AND TRIM(ref_number) = TRIM(?)
        ORDER BY COALESCE(TRY_CAST(NULLIF(TRIM(amendment_number), '') AS INTEGER), 0)
    """, [owner_org, ref_number]).fetchall()

    return {
        "found": True, "ref_number": ref_number, "department": owner_org,
        "start_date": start_date, "prog_name": prog, "description": desc,
        "chain": chain,
    }


def locate_t3010_qd_receipt(con, entity_id, funder_entity_id, amount, fiscal_year):
    """Best-effort lookup of the raw_t3010_qd row behind a qualified-donee
    gift, to show the exact filer BN and fiscal period end (T3010 gifts
    have no amendment concept, so no chain -- just filer BN + FPE + a
    self-reported note, per the spec's receipts table)."""
    funder_bn = con.execute("SELECT bn_root FROM entities WHERE entity_id = ?", [funder_entity_id]).fetchone()
    funder_bn = funder_bn[0] if funder_bn else None

    variants = con.execute("""
        SELECT DISTINCT raw_name FROM entity_links
        WHERE entity_id = ? AND source_dataset = 't3010_qualified_donee'
    """, [entity_id]).fetchall()
    variant_names = [v[0] for v in variants]
    if not variant_names or not funder_bn:
        return {"found": False, "filer_bn": funder_bn, "reason": "no donee name variant or funder BN on record"}
    if amount is None:
        return {"found": False, "filer_bn": funder_bn, "reason": "grant has no recorded amount to match against"}

    placeholders = ", ".join("?" for _ in variant_names)
    rows = con.execute(f"""
        WITH parsed AS (
            SELECT FPE, "Donee Name", "Total Gifts",
                   TRY_CAST(REPLACE(REPLACE(TRIM("Total Gifts"), ',', ''), '$', '') AS DOUBLE) AS val
            FROM raw_t3010_qd_dedup
            WHERE substr(regexp_replace(BN, '[^0-9A-Za-z]', '', 'g'), 1, 9) = ?
        )
        SELECT FPE FROM parsed
        WHERE "Donee Name" IN ({placeholders}) AND val BETWEEN ? AND ?
    """, [funder_bn] + variant_names + [amount - AMOUNT_EPSILON, amount + AMOUNT_EPSILON]).fetchall()

    if len(rows) != 1:
        return {"found": False, "filer_bn": funder_bn,
                "reason": f"{len(rows)} raw rows matched filer+donee+amount — not unambiguous"}
    return {"found": True, "filer_bn": funder_bn, "fpe": rows[0][0]}


# ── HTML rendering ───────────────────────────────────────────────────────────

CSS = """
:root{--red:#d52b1e;--ink:#1a1a1a;--mut:#6b6b6b;--bg:#faf8f5;--card:#fff;--line:#e8e4de;--gold:#b8860b}
*{box-sizing:border-box;margin:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);line-height:1.5;padding-top:40px}
.draft-banner{position:fixed;top:0;left:0;right:0;z-index:1000;background:#fff3cd;color:#8a6d00;font-weight:700;text-align:center;padding:8px 12px;font-size:.85rem;border-bottom:2px solid #8a6d00}
.draft-watermark{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) rotate(-30deg);font-size:9rem;font-weight:800;color:#000;opacity:.03;pointer-events:none;z-index:0;white-space:nowrap;user-select:none}
.draft-footer-notice{background:#fff3cd;color:#8a6d00;border:1px solid #8a6d00;border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:.8rem}
.wrap{max-width:920px;margin:0 auto;padding:0 20px 80px}
header{padding:40px 0 8px;display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
h1{font-size:2.1rem;letter-spacing:-.02em}
.badge{display:inline-block;background:var(--card);border:1px solid var(--line);border-radius:20px;padding:3px 12px;font-size:.78rem;color:var(--mut);margin-left:10px;vertical-align:middle}
.meta-line{color:var(--mut);margin:8px 0 0;font-size:.92rem}
h2{font-size:1.05rem;margin:40px 0 14px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);border-bottom:2px solid var(--red);display:inline-block;padding-bottom:4px}
h3{font-size:.82rem;margin:22px 0 10px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);font-weight:700}
.stats{display:flex;flex-wrap:wrap;gap:12px;margin-top:20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;flex:1 1 160px}
.stats>.drawer.open{flex-basis:100%;width:100%}
.stat b{display:block;font-size:1.5rem;letter-spacing:-.02em}
.stat span{color:var(--mut);font-size:.8rem}
.bars{display:flex;gap:5px;height:160px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 18px 30px;position:relative}
.chart-legend{display:flex;gap:16px;margin-bottom:8px;font-size:.78rem;color:var(--mut)}
.chart-legend span{display:inline-flex;align-items:center;gap:5px}
.chart-legend i{display:inline-block;width:10px;height:10px;border-radius:2px;font-style:normal}
.legend-recv{background:var(--red);opacity:.85}
.legend-given{background:var(--ink);opacity:.55}
.legend-gov-declared{background:var(--red);opacity:.35}
.legend-gov-identified{background:var(--red);opacity:.85}
.legend-fdn-declared{background:var(--gold);opacity:.35}
.legend-fdn-identified{background:var(--gold);opacity:.85}
.barcol{flex:1;height:100%;display:flex;flex-direction:column-reverse;gap:1px;position:relative}
.bar-recv{background:var(--red);opacity:.85;min-height:1px}
.bar-given{background:var(--ink);opacity:.55;min-height:1px}
.recv-group{flex:1;height:100%;display:flex;align-items:flex-end;gap:2px}
.bar-gov-declared,.bar-gov-identified,.bar-fdn-declared,.bar-fdn-identified{flex:1;min-height:1px}
.bar-gov-declared{background:var(--red);opacity:.35}
.bar-gov-identified{background:var(--red);opacity:.85}
.bar-fdn-declared{background:var(--gold);opacity:.35}
.bar-fdn-identified{background:var(--gold);opacity:.85}
.barcol i{position:absolute;bottom:-22px;left:50%;transform:translateX(-50%);font-style:normal;font-size:.6rem;color:var(--mut);white-space:nowrap}
.barcol u{display:none;position:absolute;bottom:calc(100% + 6px);left:50%;transform:translateX(-50%);background:var(--ink);color:#fff;font-size:.68rem;padding:3px 8px;border-radius:5px;white-space:nowrap;z-index:3}
.barcol:first-child u{left:0;transform:none}
.barcol:last-child u{left:auto;right:0;transform:none}
.barcol:hover u,.barcol.show-tip u,.barcol:focus u{display:block}
.barcol:focus{outline:2px solid var(--red);outline-offset:2px}
.gridline{position:absolute;left:18px;right:18px;border-top:1px dashed var(--line);pointer-events:none}
.gridline span{position:absolute;right:0;top:-13px;font-size:.62rem;color:var(--mut);background:var(--card);padding:0 3px}
.chart-note{color:var(--mut);font-size:.78rem;margin-bottom:8px}
.table-scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;margin-bottom:10px}
table{min-width:100%;border-collapse:collapse;background:var(--card);font-size:.85rem}
th{background:#f1ede7;text-align:left;padding:8px 10px;white-space:nowrap}
th:last-child,td:last-child{padding-right:16px}
td{padding:8px 10px;border-top:1px solid var(--line);vertical-align:top}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.src{display:inline-block;font-size:.68rem;color:var(--mut);border:1px solid var(--line);border-radius:5px;padding:1px 6px;margin-left:6px;white-space:nowrap}
.orglink{color:var(--red);text-decoration:none;font-weight:700}
.orglink:hover{text-decoration:underline}
.ext{color:var(--mut);text-decoration:none}
.ext:hover{color:var(--red);text-decoration:underline}
.ext-line{margin-top:8px}
.copy-btn{border:1px solid var(--line);background:var(--card);border-radius:4px;padding:0 5px;cursor:pointer;font-size:.78rem;color:var(--mut)}
.copy-btn:hover{border-color:var(--red);color:var(--red)}
.year-group{background:#f1ede7 !important;font-weight:600}
.claim{border-bottom:1px dotted #b8ad9c;cursor:pointer}
body.show-work .claim{border-bottom-color:var(--red);background:#fdf3d7;border-radius:3px}
.claim:hover{background:#fdf3d7;border-bottom-color:var(--red)}
.drawer{display:none;background:#fffdf7;border:1px solid #f0dfa0;border-radius:8px;padding:12px 14px;margin:6px 0 14px;font-size:.82rem;color:#4a4a4a}
.drawer.open{display:block}
.drawer .chain{margin:6px 0;padding-left:18px}
.drawer .not-found{color:#a35200;font-style:italic}
.toggle{display:flex;align-items:center;gap:8px;font-size:.82rem;color:var(--mut);white-space:nowrap}
.sw-hint{display:none;color:var(--mut);font-size:.78rem;font-weight:400}
body.show-work .sw-hint{display:inline}
.switch{position:relative;width:38px;height:22px;background:var(--line);border-radius:11px;cursor:pointer;transition:background .15s}
.switch.on{background:var(--red)}
.switch i{position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;background:#fff;transition:left .15s}
.switch.on i{left:18px}
.rollup-note{color:var(--mut);font-size:.82rem;margin:8px 0 16px}
.more-rows{display:none}
.more-rows.shown{display:table-row-group}
.show-more{display:inline-block;color:var(--red);font-weight:700;font-size:.82rem;cursor:pointer;padding:4px 0}
.show-more:hover{text-decoration:underline}
footer{margin-top:56px;color:var(--mut);font-size:.78rem;border-top:1px solid var(--line);padding-top:16px}
@media(max-width:640px){.barcol i{display:none}.barcol.keep-label i{display:block}}
"""

JS = """
function toggleShowWork(){
  document.body.classList.toggle('show-work');
  document.getElementById('sw').classList.toggle('on');
}
function toggleMoreRows(el){
  const tbody = document.getElementById(el.dataset.target);
  if (!tbody) return;
  const showing = tbody.classList.toggle('shown');
  el.textContent = showing ? (el.dataset.hide + ' \\u25b2') : (el.dataset.show + ' \\u25bc');
}
function placeDrawer(el, d){
  // Relocate the drawer next to the clicked claim so it opens inline, not at
  // the bottom of the document. A table row can only contain <td>/<th>, so a
  // claim inside one gets a sibling <tr><td colspan> row instead of a bare
  // insertAdjacentElement (which a table would silently mangle into an
  // anonymous cell).
  const row = el.closest('tr');
  if (row) {
    let holder = row.nextElementSibling;
    if (!holder || !holder.classList.contains('drawer-row')) {
      holder = document.createElement('tr');
      holder.className = 'drawer-row';
      const td = document.createElement('td');
      td.colSpan = row.children.length;
      holder.appendChild(td);
      row.insertAdjacentElement('afterend', holder);
    }
    holder.firstElementChild.appendChild(d);
  } else {
    const container = el.closest('div, h1');
    if (container) container.insertAdjacentElement('afterend', d);
  }
}
function toggleDrawer(el){
  if (el.dataset.lazy) { toggleLazyDrawer(el); return; }
  const d = document.getElementById(el.dataset.drawer);
  if (!d) return;
  if (d.classList.contains('open')) { d.classList.remove('open'); return; }
  placeDrawer(el, d);
  d.classList.add('open');
}
async function toggleLazyDrawer(el){
  // Live-app-only path (webapp.py): the claim's receipt was never computed
  // at page-render time (see render_grants_table's lazy_receipts branch), so
  // there's no pre-rendered <div class="drawer"> to toggle -- fetch it from
  // this page's own /receipt/<grant_id> route on first click, then behave
  // like a normal drawer on every click after that (no re-fetching).
  let d = document.getElementById(el.dataset.drawer);
  if (d) {
    d.classList.toggle('open');
    return;
  }
  d = document.createElement('div');
  d.className = 'drawer';
  d.id = el.dataset.drawer;
  d.innerHTML = "<p><em>Loading\\u2026</em></p>";
  placeDrawer(el, d);
  d.classList.add('open');
  try {
    const url = window.location.pathname + '/receipt/' + el.dataset.grantId +
                '?direction=' + el.dataset.direction;
    const res = await fetch(url);
    d.innerHTML = res.ok ? await res.text() : "<div class='not-found'>Receipt not found.</div>";
  } catch (e) {
    d.innerHTML = "<div class='not-found'>Failed to load receipt.</div>";
  }
}
// Timeline bar tooltips were CSS :hover-only -- dead on touch, invisible to
// keyboard. .barcol carries tabindex='0' so it's focusable; this toggles the
// same 'show-tip' class on click (for touch) and focus/blur (for keyboard),
// alongside the existing :hover rule rather than replacing it.
function toggleTip(el){ el.classList.toggle('show-tip'); }
document.querySelectorAll('.barcol').forEach(el => {
  el.addEventListener('focus', () => el.classList.add('show-tip'));
  el.addEventListener('blur', () => el.classList.remove('show-tip'));
  el.addEventListener('click', () => toggleTip(el));
});
// C3: copy-to-clipboard for a Quebec nonprofit's NEQ, next to the REQ
// search-page fallback link (REQ's own record pages aren't stably
// deep-linkable -- see REQ_SEARCH_URL's docstring).
function copyToClipboard(btn) {
  navigator.clipboard.writeText(btn.dataset.copy).then(() => {
    const orig = btn.textContent;
    btn.textContent = '\\u2713';
    setTimeout(() => { btn.textContent = orig; }, 1200);
  });
}
"""


def render_identity_receipt(links, bn_full=None):
    if not links:
        return "<div class='not-found'>No linked source records on file.</div>"
    sources = sorted(set(l["source_dataset"] for l in links))
    total_n = len(links)
    out = [f"<p>This organization appears under {total_n} name variant"
           f"{'s' if total_n != 1 else ''} across {len(sources)} source"
           f"{'s' if len(sources) != 1 else ''}:</p><ul>"]
    # Scale cap, same pattern as the grants tables: a regranter's raw name can
    # vary per branch/program/fiscal-year, so this list can run into the
    # thousands -- show the variants behind the most linked records, plus a
    # rollup note, rather than one <li> per variant unconditionally.
    for l in links[:SCALE_CAP]:
        method = l["match_method"]
        if method == "fuzzy_accept":
            badge = f"fuzzy {l['match_score']:.1f}"
        elif method == "exact_bn":
            badge = "exact BN"
        else:
            badge = "unmatched-new"
        count_note = f" ({l['n_occurrences']} records)" if l["n_occurrences"] > 1 else ""
        out.append(f"<li><b>{esc(l['raw_name'])}</b> — {esc(SOURCE_LABELS.get(l['source_dataset'], l['source_dataset']))}"
                    f" <span class='src'>{esc(badge)}</span>"
                    f"{' · BN ' + esc(l['raw_bn']) if l['raw_bn'] else ''}{count_note}</li>")
    out.append("</ul>")
    if total_n > SCALE_CAP:
        out.append(f"<p class='rollup-note'>Showing the {SCALE_CAP} variants behind the most records, "
                    f"of {total_n:,} distinct variants total.</p>")
    # C2/C7: official-source link goes last, alongside the BN match evidence
    # above, never replacing it.
    cra_link = render_cra_link_html(bn_full)
    if cra_link:
        out.append(f"<p class='ext-line'>{cra_link}</p>")
    return "".join(out)


def render_totals_receipt(role, direction):
    n = role["n_grants_received"] if direction == "received" else role["n_grants_given"]
    total = role["total_received"] if direction == "received" else role["total_given"]
    return (f"<p>Computed as the sum of {n:,} row{'s' if n != 1 else ''} in <code>grants_unified</code> "
            f"({fmt_money_precise(total)} total).</p>"
            f"<p>Caveats: federal amounts are latest-amendment-per-agreement (superseded amendment rows "
            f"are excluded, not summed). T3010 qualified-donee gifts are filer-reported by the giving "
            f"charity, not independently verified.</p>")


def render_grant_receipt(con, entity_id, grant, direction):
    src = grant["source_dataset"]
    if src == "federal_gc":
        fed_entity = entity_id if direction == "received" else grant["other_entity_id"]
        r = locate_federal_receipt(con, fed_entity, grant["amount_cad"], grant["fiscal_year"],
                                    source_ref=grant.get("source_ref"))
        if not r["found"]:
            return f"<div class='not-found'>Receipt not located: {esc(r['reason'])}.</div>"
        out = [f"<p><b>Ref:</b> {esc(r['ref_number'])} &middot; <b>Department:</b> {esc(r['department'])} "
               f"&middot; <b>Start date:</b> {esc(r['start_date'])}</p>"]
        if r["prog_name"]:
            out.append(f"<p><b>Program:</b> {esc(r['prog_name'])}</p>")
        if r["description"]:
            out.append(f"<p><b>Description:</b> {esc(r['description'])}</p>")
        if len(r["chain"]) > 1:
            vals = " &rarr; ".join(fmt_money(v) for _, v in r["chain"])
            out.append(f"<p><b>Amendment chain</b> ({len(r['chain'])} versions): {vals} — "
                        f"the profile shows only the final state.</p>")
        # C1/C7: official-source link goes last, after our own matched raw
        # record -- never the only content in the drawer.
        oc_url = open_canada_record_url(r["department"], r["ref_number"])
        if oc_url:
            out.append(f"<p class='ext-line'><a class='ext' href='{esc(oc_url)}' target='_blank' "
                       f"rel='noopener noreferrer'>View official record on open.canada.ca &#8599;</a></p>")
        return "".join(out)

    if src == "t3010_qualified_donee":
        funder_entity = grant["other_entity_id"] if direction == "received" else entity_id
        r = locate_t3010_qd_receipt(con, entity_id if direction == "received" else grant["other_entity_id"],
                                     funder_entity, grant["amount_cad"], grant["fiscal_year"])
        if not r["found"]:
            reason = r.get("reason", "not located")
            return f"<div class='not-found'>Receipt not located: {esc(reason)}.</div>"
        out = [f"<p><b>Source:</b> T3010 Qualified Donees schedule &middot; <b>Filer BN:</b> {esc(r['filer_bn'])} "
               f"&middot; <b>Fiscal period end:</b> {esc(r['fpe'])}</p>"
               f"<p>Self-reported by the giving charity on its own annual return.</p>"]
        # C2: the filing is the funder's, so link the funder's CRA listing,
        # not the recipient's -- needs the funder's full BN (bn_full), not
        # just the bn_root locate_t3010_qd_receipt() already fetched.
        funder_bn_full = con.execute(
            "SELECT bn_full FROM entities WHERE entity_id = ?", [funder_entity]
        ).fetchone()
        funder_bn_full = funder_bn_full[0] if funder_bn_full else None
        cra_link = render_cra_link_html(funder_bn_full, label="Funder's BN")
        if cra_link:
            out.append(f"<p class='ext-line'>{cra_link}</p>")
        return "".join(out)

    # canada_council / t3010_non_qualified_donee: no raw-row lookup specified
    # by the spec's receipts table -- show what grants_unified already has.
    out = [f"<p><b>Source:</b> {esc(SOURCE_LABELS.get(src, src))}</p>"]
    if grant["program_name"]:
        out.append(f"<p><b>Program:</b> {esc(grant['program_name'])}</p>")
    if grant["description"]:
        out.append(f"<p><b>Description:</b> {esc(grant['description'])}</p>")
    return "".join(out)


def render_grants_table(con, entity_id, grants, direction, drawer_ids, link_manifest=None, table_id="",
                         cap=SCALE_CAP, lazy_receipts=False):
    if not grants:
        return ""
    link_manifest = link_manifest or {}
    total_n = len(grants)
    if cap <= 0:
        # This category's whole embed budget was already spent by an earlier
        # category on the same page (render_grant_sections shares one
        # SCALE_CAP-sized budget across both subsections per direction, so a
        # regranter with e.g. 44k charity-sourced grants doesn't also force
        # 300 more government-sourced receipt lookups -- each embedded row
        # costs a real DB query for its receipt drawer, so doubling the cap
        # per category was a real, measured slowdown, not just page bloat).
        total_amt = sum(g["amount_cad"] or 0 for g in grants)
        return (f"<p class='rollup-note'>{total_n:,} grants totaling {fmt_money(total_amt)} in this category are "
                f"not shown individually -- this organization's other grant category already filled the page's "
                f"embedded-grant limit. Totals above still include them.</p>")
    capped = grants[:cap]
    rollup_note = ""
    if total_n > cap:
        rest = grants[cap:]
        rest_total = sum(g["amount_cad"] or 0 for g in rest)
        rollup_note = (f"<p class='rollup-note'>Showing the {cap} largest of {total_n:,} grants in this "
                        f"category; totals above include all of them. The remaining {len(rest):,} grants total "
                        f"{fmt_money(rest_total)}.</p>")

    # Each row is (is_data_row, html) -- year-group headers are not data rows,
    # so they don't count toward the visible-rows cutoff below.
    rows = []
    current_year = object()
    for row_index, g in enumerate(capped):
        if g["fiscal_year"] != current_year:
            current_year = g["fiscal_year"]
            year_label = f"FY {current_year}" if current_year is not None else "Year unknown"
            rows.append((False, f"<tr class='year-group'><td colspan='4'>{esc(year_label)}</td></tr>"))
        # len(drawer_ids) is NOT a safe per-row counter here: in lazy_receipts
        # mode (the live app), nothing is ever appended to drawer_ids (see
        # below), so every lazy row in this call -- and in every other
        # lazy_receipts=True call sharing this same drawer_ids list -- got the
        # exact same "drawer-N" id, frozen at whatever drawer_ids' length was
        # before this loop started. Confirmed real, not hypothetical: clicking
        # any grant after the first re-toggled the FIRST grant's already-
        # cached drawer instead of fetching its own receipt, since
        # toggleLazyDrawer()'s getElementById(el.dataset.drawer) found that
        # first drawer's element under the shared id and never re-fetched.
        # table_id is unique per (direction, category) section (see the
        # render_grant_sections call site) and row_index is unique within it,
        # so the combination is unique across the whole page regardless of
        # how many rows are lazy -- no shared mutable counter needed.
        drawer_id = f"drawer-{esc(table_id)}-{row_index}" if table_id else f"drawer-{len(drawer_ids) + row_index}"
        if lazy_receipts:
            # Live-app path (webapp.py): don't compute the receipt now -- each
            # one costs a real DB query (locate_t3010_qd_receipt in
            # particular: confirmed ~0.15s/call), so eagerly computing up to
            # SCALE_CAP of them turned a page load into 30-50+ seconds.
            # Instead this claim carries enough to fetch its own receipt on
            # first click (webapp.py's /orgs/<slug>/receipt/<grant_id> route);
            # nothing is added to drawer_ids, so no placeholder <div class=
            # 'drawer'> is pre-rendered for it either -- the client creates
            # one lazily (see toggleLazyDrawer in JS below).
            claim_attrs = (f"data-drawer='{drawer_id}' data-lazy='1' data-grant-id='{g['grant_id']}' "
                           f"data-direction='{direction}' onclick='toggleDrawer(this)'")
        else:
            drawer_ids.append((drawer_id, lambda g=g: render_grant_receipt(con, entity_id, g, direction)))
            claim_attrs = f"data-drawer='{drawer_id}' onclick='toggleDrawer(this)'"
        other_label = "Funder" if direction == "received" else "Recipient"
        # Cross-link to the other org's own page when it has one in this
        # batch (link_manifest is empty for a lone single-page build, so this
        # is a no-op there) -- a separate small icon rather than making the
        # claim span itself a link, so the existing evidence-drawer click
        # behavior (org-page-spec.md's "claim and receipt" design) is
        # untouched for pages that link nowhere.
        other_slug = link_manifest.get(g["other_entity_id"])
        # Static pages are literal files on disk (need the .html suffix);
        # the live app (webapp.py) serves org pages at /orgs/<slug> with no
        # extension -- lazy_receipts doubles as "are we the live app" since
        # both are always set together by webapp.py's render_page() call.
        other_href = f"{esc(other_slug)}" if lazy_receipts else f"{esc(other_slug)}.html"
        orglink = f" <a class='orglink' href='{other_href}' title='View organization page'>↗</a>" if other_slug else ""
        rows.append((True,
            f"<tr><td><span class='claim' {claim_attrs}>"
            f"{esc(english_name(g['other_name']))}</span>{orglink}"
            f"<span class='src'>{esc(SOURCE_LABELS.get(g['source_dataset'], g['source_dataset']))}</span></td>"
            f"<td>{esc(g['program_name']) if g['program_name'] else '—'}</td>"
            f"<td class='num'>{esc(fmt_money_precise(g['amount_cad']))}</td>"
            f"<td>{esc(other_label)}</td></tr>"
        ))

    # A regranter or major recipient can have hundreds of rows even after the
    # SCALE_CAP embed limit above -- rendering them all directly into the page
    # buries whatever section comes next. Show the first VISIBLE_ROWS data
    # rows; the rest go into a second, initially-hidden <tbody> a "Show more"
    # toggle reveals in place (no fetch -- the page stays self-contained).
    # Once the cutoff is reached, header rows (which carry no count of their
    # own) collapse along with the data rows that follow them, so a visible
    # year label is never left dangling with nothing under it.
    visible, extra = [], []
    shown = 0
    in_extra = False
    for is_data, html_row in rows:
        if not in_extra and shown >= VISIBLE_ROWS:
            in_extra = True
        (extra if in_extra else visible).append(html_row)
        if is_data and not in_extra:
            shown += 1

    table_html = (f"<div class='table-scroll'><table><thead><tr><th>Organization</th><th>Program</th>"
                  f"<th class='num'>Amount</th><th>Role</th></tr></thead>"
                  f"<tbody>{''.join(visible)}</tbody>")
    if extra:
        n_extra = sum(1 for is_data, _ in rows if is_data) - shown
        more_id = f"more-{esc(table_id) or len(drawer_ids)}"
        show_label = f"Show {n_extra:,} more grant{'s' if n_extra != 1 else ''}"
        table_html += f"<tbody class='more-rows' id='{more_id}'>{''.join(extra)}</tbody>"
        table_html += (f"<tfoot><tr><td colspan='4'><span class='show-more' data-target='{more_id}' "
                        f"data-show='{esc(show_label)}' data-hide='Show fewer' onclick='toggleMoreRows(this)'>"
                        f"{esc(show_label)} ▼</span></td></tr></tfoot>")
    table_html += "</table></div>"
    return table_html + rollup_note


def render_grant_sections(con, entity_id, grants, direction, drawer_ids, link_manifest=None, lazy_receipts=False):
    """Split grants into qualified-donee / non-qualified-donee / government
    subsections (GRANT_CATEGORY, keyed by source_dataset) rather than one
    undifferentiated table -- a community foundation's page otherwise mixes
    federal program disbursements in with peer-charity gifts and non-charity
    gifts, three legally and practically different kinds of money. Each
    non-empty bucket gets its own heading and its own collapse behavior via
    render_grants_table, but all subsections SHARE one SCALE_CAP-sized embed
    budget (not 300 rows each) -- every embedded row costs a real per-row DB
    query for its receipt drawer (locate_federal_receipt / locate_t3010_qd_
    receipt), and a large regranter can have tens of thousands of rows in one
    category, so an independent cap per category would multiply that query
    cost by the number of non-empty categories on the page."""
    if not grants:
        return ""
    buckets = {c: [] for c in CATEGORY_ORDER}
    for g in grants:
        buckets[GRANT_CATEGORY.get(g["source_dataset"], "government")].append(g)

    out = []
    budget = SCALE_CAP
    for category in CATEGORY_ORDER:
        bucket = buckets[category]
        if not bucket:
            continue
        cap = max(0, budget)
        table_html = render_grants_table(con, entity_id, bucket, direction, drawer_ids, link_manifest,
                                          table_id=f"{direction}-{category}", cap=cap, lazy_receipts=lazy_receipts)
        budget -= min(len(bucket), cap)
        if table_html:
            heading = CATEGORY_HEADINGS[(direction, category)]
            out.append(f"<h3>{esc(heading)} ({fmt_int(len(bucket))})</h3>{table_html}")
    return "".join(out)


def render_timeline(entity_kind, years, received_by_year, given_by_year,
                     gov_declared_by_year=None, gov_identified_by_year=None,
                     fdn_declared_by_year=None, fdn_identified_by_year=None):
    """For a charity-year with a T3010 filing on record (a key present in
    gov_declared_by_year -- both declared dicts always share the same key set
    since they come from the same entity_financials_by_year row), render
    declared-vs-identified comparison bars for government and foundation
    funding instead of a plain received bar. Every other year (non-charity
    entities; charity-years with no T3010 filing at all) falls back to the
    original plain received bar -- a single org's chart can mix both styles
    across its years. `given` is always the original plain bar; see
    docs/org-page-spec.md's Decisions section for why the given side doesn't
    get the same declared/identified treatment."""
    gov_declared_by_year = gov_declared_by_year or {}
    gov_identified_by_year = gov_identified_by_year or {}
    fdn_declared_by_year = fdn_declared_by_year or {}
    fdn_identified_by_year = fdn_identified_by_year or {}
    if not years:
        return ""

    def all_nonnull(d):
        return [v for v in d.values() if v]

    max_v = max(
        all_nonnull(received_by_year) + all_nonnull(given_by_year) +
        all_nonnull(gov_declared_by_year) + all_nonnull(gov_identified_by_year) +
        all_nonnull(fdn_declared_by_year) + all_nonnull(fdn_identified_by_year) + [0]
    ) or 1

    def bar(cls, v):
        return f"<div class='{cls}' style='height:{max(2, 100 * v / max_v)}%'></div>" if v else ""

    used = {"given": False, "recv": False, "gov": False, "fdn": False}
    cols = []
    for i, y in enumerate(years):
        gv = given_by_year.get(y, 0) or 0
        given_div = bar("bar-given", gv)
        used["given"] |= bool(gv)

        tip_parts = []
        has_t3010 = entity_kind == "charity" and y in gov_declared_by_year
        if has_t3010:
            gov_declared = gov_declared_by_year.get(y)
            gov_identified = gov_identified_by_year.get(y, 0) or 0
            fdn_declared = fdn_declared_by_year.get(y)
            fdn_identified = fdn_identified_by_year.get(y, 0) or 0
            used["gov"] |= bool(gov_declared or gov_identified)
            used["fdn"] |= bool(fdn_declared or fdn_identified)
            recv_content = (
                f"<div class='recv-group'>"
                f"{bar('bar-gov-declared', gov_declared or 0)}{bar('bar-gov-identified', gov_identified)}"
                f"{bar('bar-fdn-declared', fdn_declared or 0)}{bar('bar-fdn-identified', fdn_identified)}"
                f"</div>"
            )
            if gov_declared or gov_identified:
                tip_parts.append(f"government declared {fmt_money(gov_declared)} "
                                  f"&middot; identified {fmt_money(gov_identified)} "
                                  f"({fmt_pct(gov_identified, gov_declared)})")
            if fdn_declared or fdn_identified:
                tip_parts.append(f"foundation declared {fmt_money(fdn_declared)} "
                                  f"&middot; identified {fmt_money(fdn_identified)} "
                                  f"({fmt_pct(fdn_identified, fdn_declared)})")
        else:
            rv = received_by_year.get(y, 0) or 0
            recv_content = bar("bar-recv", rv)
            used["recv"] |= bool(rv)
            if rv:
                tip_parts.append(f"received {fmt_money(rv)}")

        if gv:
            tip_parts.append(f"given {fmt_money(gv)}")
        tip = f"FY{y}: " + " &middot; ".join(tip_parts) if tip_parts else f"FY{y}"
        # keep-label: under 640px, year labels are hidden by default (CSS) --
        # only the first, last, and every 5th column keeps its <i> visible,
        # so the chart still has date anchors instead of no labels at all.
        keep_label = i == 0 or i == len(years) - 1 or i % 5 == 0
        col_cls = "barcol keep-label" if keep_label else "barcol"
        cols.append(f"<div class='{col_cls}' tabindex='0'>{given_div}{recv_content}<u>{tip}</u><i>{y}</i></div>")

    legend_items = []
    if used["recv"]:
        legend_items.append("<span><i class='legend-recv'></i>Received</span>")
    if used["given"]:
        legend_items.append("<span><i class='legend-given'></i>Given</span>")
    if used["gov"]:
        legend_items.append("<span><i class='legend-gov-declared'></i>Gov't declared</span>")
        legend_items.append("<span><i class='legend-gov-identified'></i>Gov't identified</span>")
    if used["fdn"]:
        legend_items.append("<span><i class='legend-fdn-declared'></i>Foundation declared</span>")
        legend_items.append("<span><i class='legend-fdn-identified'></i>Foundation identified</span>")
    legend = f"<div class='chart-legend'>{''.join(legend_items)}</div>"

    note = ""
    if used["gov"] or used["fdn"]:
        note = ("<p class='chart-note'>Declared = what the charity reported receiving on its T3010; "
                "identified = what this project found in funders' own records.</p>")

    # Y-axis: max and midpoint gridlines, positioned to match .bars' fixed
    # 160px height / 18px-18px-30px padding (the same geometry the bar
    # height percentages above are computed against) -- so bars aren't
    # purely relative with no reference scale.
    bars_height, pad_top, pad_bottom = 160, 18, 30
    content_height = bars_height - pad_top - pad_bottom
    gridlines = (
        f"<div class='gridline' style='top:{pad_top}px'><span>{fmt_money(max_v)}</span></div>"
        f"<div class='gridline' style='top:{pad_top + content_height / 2:.0f}px'><span>{fmt_money(max_v / 2)}</span></div>"
    )
    return f"{note}{legend}<div class='bars'>{gridlines}{''.join(cols)}</div>"


def render_page(con, entity_id, link_manifest=None, lazy_receipts=False, discovery_index=None):
    entity = fetch_entity(con, entity_id)
    role = fetch_role_summary(con, entity_id)
    financials = fetch_financials(con, entity_id)
    links = fetch_entity_links(con, entity_id)
    received = fetch_grants(con, entity_id, "received")
    given = fetch_grants(con, entity_id, "given")
    years, received_by_year, given_by_year = fetch_timeline(con, entity_id)
    gov_declared_by_year, fdn_declared_by_year = fetch_declared_by_year(con, entity_id)
    gov_identified_by_year = fetch_government_identified_by_year(con, entity_id)
    fdn_identified_by_year = fetch_foundation_identified_by_year(con, entity_id)
    # A charity can have a T3010 filing for a year with zero matched grants
    # (a real, interesting "0% identified" data point) -- widen years beyond
    # fetch_timeline's grants_unified-only set so that year still shows up.
    years = sorted(set(years) | set(gov_declared_by_year))

    name_display = english_name(entity["canonical_name"])
    kind_label = KIND_LABELS.get(entity["entity_kind"], "Organization")
    loc = ", ".join(p for p in (entity["city"], entity["province"]) if p)

    drawer_ids = []  # [(drawer_id, render_fn), ...]

    identity_drawer_id = "drawer-identity"
    identity_html = render_identity_receipt(links, bn_full=entity.get("bn_full"))

    discovery_badge_html = ""
    discovery_match = (discovery_index or {}).get(entity_id)
    if discovery_match:
        discovery_drawer_id = "drawer-discovery"
        source = discovery_match["discovery_source"]
        source_name = "Registre des entreprises du Québec (REQ)" if source == "req" \
            else "Corporations Canada's federal not-for-profit registry"
        # C3/C4: NEQ (req) / corporation number (corporations_canada),
        # prominently displayed with a copy button, next to the official
        # registry link -- REQ's own record pages aren't stably deep-
        # linkable (session-based), so the search page + the number itself
        # is the fallback; Corporations Canada's corpId deep link is
        # confirmed working.
        official_link_html = ""
        source_id = discovery_match.get("source_id")
        if source_id and source == "req":
            official_link_html = (
                f"<p class='ext-line'>NEQ <code>{esc(source_id)}</code> "
                f"<button type='button' class='copy-btn' data-copy='{esc(source_id)}' "
                f"onclick='copyToClipboard(this)'>&#10697;</button> — "
                f"<a class='ext' href='{esc(REQ_SEARCH_URL)}' target='_blank' rel='noopener noreferrer'>"
                f"look up in the Registre des entreprises &#8599;</a></p>"
            )
        elif source_id and source == "corporations_canada":
            cc_url = corporations_canada_url(source_id)
            official_link_html = (
                f"<p class='ext-line'>Corporation number <code>{esc(source_id)}</code> — "
                f"<a class='ext' href='{esc(cc_url)}' target='_blank' rel='noopener noreferrer'>"
                f"view on Corporations Canada &#8599;</a></p>"
            )
        discovery_drawer_html = (
            f"<p>Matched via {esc(source_name)}: "
            f"<b>{esc(discovery_match['legal_name'])}</b> was independently confirmed as a legally "
            f"incorporated nonprofit (not currently a registered charity), and its federal grant "
            f"activity was linked to this entity via <b>{esc(discovery_match['matched_grant_entity_name'])}</b>. "
            f"See <a href='../entity-resolution-methodology.md'>entity-resolution-methodology.md</a> "
            f"and <code>discovery/</code> in the repo for the matching methodology.</p>"
            f"{official_link_html}"
        )
        drawer_ids.append((discovery_drawer_id, (lambda h=discovery_drawer_html: h)))
        badge_text = DISCOVERY_BADGE_LABELS[source]
        discovery_badge_html = (
            f"<span class='badge claim' data-drawer='{discovery_drawer_id}' "
            f"onclick='toggleDrawer(this)'>{esc(badge_text)}</span>"
        )

    header_meta = []
    if loc:
        header_meta.append(esc(loc))
    if entity["bn_root"]:
        # C2: the direct apps.cra-arc.gc.ca record-URL guess is confirmed
        # broken (reproduced against both this environment and a real
        # user's own browser session, 2026-07-18 -- see cra_charity_url()'s
        # docstring) -- render_cra_link_html() shows the BN plus a copy
        # button and a link to the CRA's general List of Charities page
        # instead of a dead direct link.
        cra_link = render_cra_link_html(entity.get("bn_full"))
        header_meta.append(cra_link if cra_link else f"BN {esc(entity['bn_root'])}")

    # C5: department links -- every federal_dept entity gets the unfiltered
    # open.canada.ca grants search link; the ~30 largest by record count also
    # get a curated department homepage link (DEPARTMENT_LINKS).
    if entity["entity_kind"] == "federal_dept":
        dept_links = [f"<a class='ext' href='{esc(OPEN_CANADA_GRANTS_SEARCH_URL)}' target='_blank' "
                     f"rel='noopener noreferrer'>All records from this department on open.canada.ca &#8599;</a>"]
        owner_org = fetch_department_owner_org(con, entity_id)
        dept_info = DEPARTMENT_LINKS.get(owner_org) if owner_org else None
        if dept_info:
            dept_name, dept_url = dept_info
            dept_links.append(f"<a class='ext' href='{esc(dept_url)}' target='_blank' rel='noopener noreferrer'>"
                              f"{esc(dept_name)} homepage &#8599;</a>")
        header_meta.append(" &middot; ".join(dept_links))

    # ── stat row ──
    stats = []
    if role["total_received"]:
        stats.append((fmt_money(role["total_received"]), "total received", "recv-total"))
    if role["total_given"]:
        stats.append((fmt_money(role["total_given"]), "total given", "given-total"))
    if role["n_grants_received"]:
        n = role["n_grants_received"]
        stats.append((fmt_int(n), f"grant{'s' if n != 1 else ''} received", None))
    if role["n_grants_given"]:
        n = role["n_grants_given"]
        stats.append((fmt_int(n), f"grant{'s' if n != 1 else ''} given", None))
    if financials and financials["total_revenue"] is not None:
        fpe = financials["fiscal_period_end"]
        stats.append((fmt_money(financials["total_revenue"]), f"latest reported revenue ({fpe})", "revenue"))
    if years:
        stats.append((f"{years[0]}–{years[-1]}", "years active", None))

    stat_drawers = {}
    if role["total_received"]:
        stat_drawers["recv-total"] = render_totals_receipt(role, "received")
    if role["total_given"]:
        stat_drawers["given-total"] = render_totals_receipt(role, "given")
    if financials and financials["total_revenue"] is not None:
        stat_drawers["revenue"] = (
            f"<p>From <code>entity_financials</code>: BN {esc(financials['bn_full'])}, "
            f"fiscal period end {esc(financials['fiscal_period_end'])}. T3010 line 4700 (total revenue). "
            f"Only the latest filed fiscal year is kept per organization.</p>"
        )

    stat_html = []
    for label, sub, drawer_key in stats:
        if drawer_key and drawer_key in stat_drawers:
            did = f"drawer-stat-{drawer_key}"
            drawer_ids.append((did, (lambda h=stat_drawers[drawer_key]: h)))
            stat_html.append(
                f"<div class='stat'><b class='claim' data-drawer='{did}' onclick='toggleDrawer(this)'>{esc(label)}</b>"
                f"<span>{esc(sub)}</span></div>"
            )
        else:
            stat_html.append(f"<div class='stat'><b>{esc(label)}</b><span>{esc(sub)}</span></div>")

    timeline_html = render_timeline(
        entity["entity_kind"], years, received_by_year, given_by_year,
        gov_declared_by_year, gov_identified_by_year, fdn_declared_by_year, fdn_identified_by_year,
    )
    received_html = render_grant_sections(con, entity_id, received, "received", drawer_ids, link_manifest,
                                           lazy_receipts=lazy_receipts)
    given_html = render_grant_sections(con, entity_id, given, "given", drawer_ids, link_manifest,
                                        lazy_receipts=lazy_receipts)

    # Now that render_grants_table has appended to drawer_ids with closures,
    # materialize their HTML (deferred so table-building order doesn't matter).
    drawer_html = []
    drawer_html.append(f"<div class='drawer' id='{identity_drawer_id}'>{identity_html}</div>")
    for did, render_fn in drawer_ids:
        drawer_html.append(f"<div class='drawer' id='{did}'>{render_fn()}</div>")

    sections = []
    if stat_html:
        sections.append(f"<div class='stats'>{''.join(stat_html)}</div>")
    if timeline_html:
        sections.append(f"<h2>Funding timeline</h2>{timeline_html}")
    if received_html:
        sections.append(f"<h2>Grants received</h2>{received_html}")
    if given_html:
        sections.append(f"<h2>Grants given</h2>{given_html}")

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>[DRAFT] {esc(name_display)} — Canadian Nonprofit Data</title>
<style>{CSS}</style></head><body>
<div class="draft-banner">{esc(DRAFT_BANNER_TEXT)}</div>
<div class="draft-watermark">DRAFT</div>
<div class="wrap">
<header>
<div>
<h1><span class="claim" data-drawer="{identity_drawer_id}" onclick="toggleDrawer(this)">{esc(name_display)}</span>
<span class="badge">{esc(kind_label)}</span>{discovery_badge_html}</h1>
<p class="meta-line">{" &middot; ".join(header_meta) if header_meta else ""}</p>
</div>
<div class="toggle" onclick="toggleShowWork()">
<div class="switch" id="sw"><i></i></div>
Show your work
<span class="sw-hint">&mdash; highlighted text is clickable; click any claim to see its source</span>
</div>
</header>
{"".join(sections)}
<div id="drawers">{"".join(drawer_html)}</div>
<footer><p class="draft-footer-notice">{esc(DRAFT_FULL_TEXT)}</p>
Generated {esc(generated)} from the Canadian Nonprofit Data entity graph
(federal Grants &amp; Contributions, CRA T3010, Canada Council for the Arts).
Matching methodology &amp; limitations: see
<a href="../entity-resolution-methodology.md">entity-resolution-methodology.md</a>.
&middot; <a href="{'/orgs' if lazy_receipts else 'index.html'}">&larr; Search all organizations</a></footer>
</div>
<script>{JS}</script>
</body></html>"""


# ── batch generation (many pages, one connection, cross-linked) ─────────────

def fetch_batch_entities(con, min_flow=0, limit=None):
    """Entities worth a page: any grant flow at all (role != 'no_flows'),
    above min_flow, ordered by total flow descending so a --limit cutoff
    keeps the most significant organizations rather than an arbitrary slice.

    Also computes the six received/given x qualified_donee/non_qualified_donee/
    government flags used by the search index's filter checkboxes, via one
    GROUP BY aggregate over grants_unified (a full-table scan, but a single
    one -- ~0.2s over the real ~540k-entity corpus) rather than a per-entity
    lookup, which is what makes rendering an individual receipt drawer slow
    at this scale (see render_grant_sections's docstring)."""
    query = """
        WITH flows AS (
            SELECT recipient_entity_id AS entity_id, source_dataset, 0 AS is_given FROM grants_unified
            UNION ALL
            SELECT funder_entity_id AS entity_id, source_dataset, 1 AS is_given FROM grants_unified
        ),
        flags AS (
            SELECT entity_id,
              MAX(is_given = 0 AND source_dataset = 't3010_qualified_donee') AS recv_qualified,
              MAX(is_given = 0 AND source_dataset = 't3010_non_qualified_donee') AS recv_non_qualified,
              MAX(is_given = 0 AND source_dataset IN ('federal_gc', 'canada_council', 'otf')) AS recv_government,
              MAX(is_given = 1 AND source_dataset = 't3010_qualified_donee') AS given_qualified,
              MAX(is_given = 1 AND source_dataset = 't3010_non_qualified_donee') AS given_non_qualified,
              MAX(is_given = 1 AND source_dataset IN ('federal_gc', 'canada_council', 'otf')) AS given_government
            FROM flows GROUP BY entity_id
        )
        SELECT e.entity_id, e.canonical_name, e.city, e.province, e.entity_kind,
               COALESCE(s.total_given, 0) AS total_given, COALESCE(s.total_received, 0) AS total_received,
               COALESCE(f.recv_qualified, false), COALESCE(f.recv_non_qualified, false),
               COALESCE(f.recv_government, false), COALESCE(f.given_qualified, false),
               COALESCE(f.given_non_qualified, false), COALESCE(f.given_government, false)
        FROM entities e
        JOIN entity_role_summary s ON s.entity_id = e.entity_id
        LEFT JOIN flags f ON f.entity_id = e.entity_id
        WHERE s.role != 'no_flows' AND (COALESCE(s.total_given, 0) + COALESCE(s.total_received, 0)) >= ?
        ORDER BY (COALESCE(s.total_given, 0) + COALESCE(s.total_received, 0)) DESC
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    rows = con.execute(query, [min_flow]).fetchall()
    cols = ["entity_id", "canonical_name", "city", "province", "entity_kind", "total_given", "total_received",
            "recv_qualified", "recv_non_qualified", "recv_government",
            "given_qualified", "given_non_qualified", "given_government"]
    return [dict(zip(cols, r)) for r in rows]


def count_batch_entities(con, min_flow=0):
    """True count matching fetch_batch_entities's WHERE clause, independent
    of any --limit -- used to show "top N of TOTAL" on the search index when
    the batch was capped rather than silently truncating with no indication."""
    return con.execute("""
        SELECT count(*) FROM entities e JOIN entity_role_summary s ON s.entity_id = e.entity_id
        WHERE s.role != 'no_flows' AND (COALESCE(s.total_given, 0) + COALESCE(s.total_received, 0)) >= ?
    """, [min_flow]).fetchone()[0]


def build_link_manifest(batch_entities):
    """entity_id -> unique slug for every entity that will get a page.
    Two different entities can legitimately slugify to the same string
    (e.g. same-named orgs in different provinces, or a truncated/expanded
    name variant) -- first one (highest total flow, since batch_entities is
    already sorted that way) keeps the bare slug; later collisions get an
    entity_id suffix so no page silently overwrites another."""
    manifest = {}
    used_slugs = set()
    for row in batch_entities:
        slug = slug_for(row["canonical_name"])
        if slug in used_slugs:
            slug = f"{slug}-{row['entity_id']}"
        used_slugs.add(slug)
        manifest[row["entity_id"]] = slug
    return manifest


# (json_key, batch_entities dict key, filter checkbox label) -- one row per
# GRANT_CATEGORY x direction combination, in the same order they're offered
# as filter checkboxes. Labels reuse CATEGORY_HEADINGS so the search page's
# vocabulary matches each org page's own subsection headings exactly.
SEARCH_FILTER_FIELDS = [
    ("rq", "recv_qualified", "received", "qualified_donee"),
    ("rn", "recv_non_qualified", "received", "non_qualified_donee"),
    ("rg", "recv_government", "received", "government"),
    ("gq", "given_qualified", "given", "qualified_donee"),
    ("gn", "given_non_qualified", "given", "non_qualified_donee"),
    ("gg", "given_government", "given", "government"),
]


LIVE_APP_ORGS_URL = "/orgs"


def render_index_tombstone_page():
    """A8: docs/orgs/index.html used to embed the top DEFAULT_INDEX_LIMIT
    organizations as inline JSON (10.7MB, real measured size) -- superseded
    by the live webapp's /orgs route, which runs every search over the full
    ~536k-entity corpus with no cap and no build step. Replaces
    render_index_page()'s output at both build_all_pages' and
    build_search_index's index_path write, so re-running --index/--all no
    longer reproduces the old file. The individual per-org pages
    build_all_pages writes are untouched -- still useful for the three
    committed samples under docs/orgs/."""
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Search organizations — Canadian Nonprofit Data</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:80px auto;padding:0 20px;color:#2a2a2a}
a{color:#d52b1e;font-weight:600}</style></head><body>
<p>This static search page has been retired in favour of the live search app, which searches
every organization in the dataset instead of a capped top subset embedded here.</p>
<p><a href="/orgs">Go to the live organization search &rarr;</a></p>
</body></html>"""


def render_index_page(batch_entities, link_manifest, total_count=None):
    """A single self-contained search page over every entity with recorded
    grant activity -- vanilla JS, no fetch/build step, no dependencies, same
    self-contained-HTML principle as the rest of docs/. Combines an optional
    name substring with any checked category-flag filters (AND across both
    the text query and every checked box), so "orgs with 'red' in the name",
    "orgs that received a government grant", and combinations of the two are
    all the same one query box + checkbox list -- no separate search modes.

    total_count is the true count before any --limit cap (count_batch_entities),
    used only to render a "showing the top N of TOTAL" note when the two
    differ -- embedding every one of the ~540k real entities inline as JSON
    produces a 105MB file, impractical to load in a browser, so the batch
    passed in here is usually capped (see DEFAULT_INDEX_LIMIT) and that cap
    needs to be stated plainly rather than silently truncating results."""
    import json as _json

    records = [
        {
            "n": english_name(row["canonical_name"]),
            "s": link_manifest[row["entity_id"]],
            "k": KIND_LABELS.get(row["entity_kind"], "Organization"),
            "loc": ", ".join(p for p in (row["city"], row["province"]) if p),
            "f": fmt_money((row["total_given"] or 0) + (row["total_received"] or 0)),
            **{key: bool(row[field]) for key, field, _, _ in SEARCH_FILTER_FIELDS},
        }
        for row in batch_entities
    ]
    # Plain json.dumps() doesn't escape '<', so a canonical_name containing a
    # literal "</script>" (raw source names can carry literal HTML -- A1's
    # span-wrapped/entity-encoded fix cleans the DB going forward, but this
    # is defense in depth against any future record slipping through) would
    # prematurely close the script tag and get parsed as raw HTML/script.
    # Escaping every '<' is semantically inert in JS/JSON.
    data_json = _json.dumps(records, ensure_ascii=False).replace("<", "\\u003c")

    groups = []
    for direction, group_label in (("received", "Received"), ("given", "Given")):
        boxes = "".join(
            f"<label><span class='chk-row'><input type='checkbox' data-key='{key}'> "
            f"{esc(CATEGORY_HEADINGS[(d, cat)])}</span>"
            f"<span class='hint'>{esc(CATEGORY_HINTS[(d, cat)])}</span></label>"
            for key, _, d, cat in SEARCH_FILTER_FIELDS if d == direction
        )
        groups.append(f"<div class='filter-group'><span class='filter-label'>{esc(group_label)}</span>{boxes}</div>")
    filters_html = f"<div class='filters'>{''.join(groups)}</div>"

    if total_count is not None and total_count > len(records):
        meta_text = (f"Showing the top {len(records):,} of {total_count:,} organizations with at least one "
                     f"recorded grant, funding, or gift -- ranked by total flow, largest first. Name and "
                     f"filter searches only run over these {len(records):,}.")
    else:
        n = total_count if total_count is not None else len(records)
        meta_text = f"{n:,} organizations with at least one recorded grant, funding{'' if n == 1 else 's'}, or gift"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Search organizations — Canadian Nonprofit Data</title>
<style>{CSS}
.search-box{{width:100%;font-size:1.1rem;padding:14px 16px;border:1px solid var(--line);border-radius:10px;margin-top:20px}}
.filters{{display:flex;gap:28px;flex-wrap:wrap;margin-top:16px;padding:14px 16px;background:var(--card);border:1px solid var(--line);border-radius:10px}}
.filter-group{{display:flex;flex-direction:column;gap:6px;font-size:.85rem}}
.filter-label{{color:var(--mut);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin-bottom:2px}}
.filter-group label{{display:flex;flex-direction:column;gap:2px;cursor:pointer}}
.filter-group label .chk-row{{display:flex;align-items:center;gap:7px}}
.filter-group label .hint{{color:var(--mut);font-size:.72rem;margin-left:22px;font-weight:400;text-transform:none;letter-spacing:normal}}
.results{{margin-top:18px}}
.result{{display:block;padding:12px 14px;border:1px solid var(--line);border-radius:8px;margin-bottom:8px;background:var(--card);text-decoration:none;color:var(--ink)}}
.result:hover{{border-color:var(--red)}}
.result b{{display:block}}
.result span{{color:var(--mut);font-size:.82rem}}
.count{{color:var(--mut);font-size:.85rem;margin-top:10px}}
</style></head><body>
<div class="wrap">
<header><div><h1>Search organizations</h1>
<p class="meta-line">{esc(meta_text)}</p>
<p class="meta-line">Looking for what a grant was <em>for</em> instead of who received it?
<a href="../grants/index.html">Search grant text &rarr;</a></p></div></header>
<input class="search-box" id="q" type="text" placeholder="Search by organization name...">
{filters_html}
<div class="count" id="count"></div>
<div class="results" id="results"></div>
</div>
<script>
function esc(s) {{
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({{
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }}[c]));
}}
const DATA = {data_json};
const q = document.getElementById('q');
const results = document.getElementById('results');
const count = document.getElementById('count');
const checkboxes = Array.from(document.querySelectorAll('.filters input[type=checkbox]'));
function render(list) {{
  results.innerHTML = list.slice(0, 50).map(r =>
    `<a class="result" href="${{esc(r.s)}}.html"><b>${{esc(r.n)}}</b><span>${{esc(r.k)}}${{r.loc ? ' · ' + esc(r.loc) : ''}} · ${{esc(r.f)}} total flow</span></a>`
  ).join('');
}}
function search() {{
  const term = q.value.trim().toLowerCase();
  const activeKeys = checkboxes.filter(cb => cb.checked).map(cb => cb.dataset.key);
  if (!term && !activeKeys.length) {{ count.textContent = ''; results.innerHTML = ''; return; }}
  let matches = DATA;
  if (term) matches = matches.filter(r => r.n.toLowerCase().includes(term));
  if (activeKeys.length) matches = matches.filter(r => activeKeys.every(k => r[k]));
  count.textContent = matches.length.toLocaleString() + ' match' + (matches.length === 1 ? '' : 'es') +
    (matches.length > 50 ? ' (showing first 50)' : '');
  render(matches);
}}
q.addEventListener('input', search);
checkboxes.forEach(cb => cb.addEventListener('change', search));
</script>
</body></html>"""


def build_all_pages(db_path, out_dir=None, min_flow=0, limit=None, progress_every=2000):
    """Generate a cross-linked page for every entity with grant activity
    (subject to min_flow/limit), plus a small tombstone at docs/orgs/
    index.html (A8 -- the search page itself is retired in favour of the
    live webapp's /orgs route; see render_index_tombstone_page()). One
    connection for the whole run, unlike the single-page CLI path -- opening
    a fresh connection per page doesn't matter for one page, but does for
    tens of thousands."""
    out_dir = out_dir or ORGS_DIR
    os.makedirs(out_dir, exist_ok=True)
    con = open_db(db_path)
    try:
        batch = fetch_batch_entities(con, min_flow=min_flow, limit=limit)
        link_manifest = build_link_manifest(batch)
        discovery_index = load_discovery_index()
        print(f"Generating {len(batch):,} organization pages ...")
        for i, row in enumerate(batch, 1):
            page = render_page(con, row["entity_id"], link_manifest, discovery_index=discovery_index)
            out_path = os.path.join(out_dir, f"{link_manifest[row['entity_id']]}.html")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(page)
            if progress_every and i % progress_every == 0:
                print(f"  ... {i:,}/{len(batch):,}")
        index_path = os.path.join(out_dir, "index.html")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(render_index_tombstone_page())
    finally:
        con.close()
    print(f"Wrote {len(batch):,} pages + {index_path} (tombstone)")
    return len(batch), index_path


def build_search_index(db_path, out_dir=None, min_flow=0, limit=None):
    """Writes docs/orgs/index.html as a small tombstone pointing at the live
    webapp's /orgs route (A8) -- this used to build the full name +
    category-filter search page embedding up to DEFAULT_INDEX_LIMIT
    organizations as inline JSON (~105MB uncapped, 10.7MB at the default
    cap), superseded by /orgs, which runs every search over the full
    ~536k-entity corpus with no cap. Still returns the batch count (a fast
    aggregate query, kept for the informative print/return value) even
    though it's no longer embedded in the file."""
    if limit is None:
        limit = DEFAULT_INDEX_LIMIT
    out_dir = out_dir or ORGS_DIR
    os.makedirs(out_dir, exist_ok=True)
    con = open_db(db_path)
    try:
        batch = fetch_batch_entities(con, min_flow=min_flow, limit=limit)
        index_path = os.path.join(out_dir, "index.html")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(render_index_tombstone_page())
    finally:
        con.close()
    print(f"Wrote tombstone -> {index_path} (search now lives at the live webapp's /orgs route)")
    return len(batch), index_path


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_page(db_path, entity_id, out_path=None):
    con = open_db(db_path)
    try:
        entity = fetch_entity(con, entity_id)
        page = render_page(con, entity_id, discovery_index=load_discovery_index())
    finally:
        con.close()

    if out_path is None:
        os.makedirs(ORGS_DIR, exist_ok=True)
        out_path = os.path.join(ORGS_DIR, f"{slug_for(entity['canonical_name'])}.html")
    else:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return out_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate an organization profile page.")
    parser.add_argument("name", nargs="?", help="organization name (fuzzy substring lookup)")
    parser.add_argument("--entity-id", type=int, help="resolve by exact entity_id")
    parser.add_argument("--bn", help="resolve by CRA business number")
    parser.add_argument("--list", action="store_true", help="print candidate matches, build nothing")
    parser.add_argument("--out", help="output path (default: docs/orgs/<slug>.html)")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="path to nonprofit_network.duckdb")
    parser.add_argument("--all", action="store_true",
                         help="batch-generate a cross-linked page for every entity with grant activity, "
                              "plus a docs/orgs/index.html tombstone pointing at the live webapp's /orgs "
                              "search route, instead of a single lookup")
    parser.add_argument("--index", action="store_true",
                         help="build only the docs/orgs/index.html tombstone (search itself now lives at "
                              "the live webapp's /orgs route -- see AGENTS.md), without rendering "
                              "individual profile pages -- fast (one aggregate query), unlike --all's "
                              "full per-page batch")
    parser.add_argument("--limit", type=int,
                         help=f"with --all/--index: cap to the top N entities by total flow "
                              f"(--index defaults to {DEFAULT_INDEX_LIMIT:,} if not given; --all is uncapped)")
    parser.add_argument("--min-flow", type=float, default=0,
                         help="with --all/--index: skip entities whose total given+received is below this")
    args = parser.parse_args(argv)

    if args.all:
        build_all_pages(args.db, min_flow=args.min_flow, limit=args.limit)
        return

    if args.index:
        build_search_index(args.db, min_flow=args.min_flow, limit=args.limit)
        return

    if not any([args.name, args.entity_id is not None, args.bn]):
        parser.error("provide a name, --entity-id, or --bn (or --all/--index to batch-generate)")

    con = open_db(args.db)
    try:
        entity_id = resolve_entity_id(con, args)
    finally:
        con.close()

    out_path = build_page(args.db, entity_id, args.out)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
