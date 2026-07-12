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
from datetime import datetime

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(ROOT, "nonprofit_network.duckdb")
ORGS_DIR = os.path.join(ROOT, "docs", "orgs")

SCALE_CAP = 300
AMOUNT_EPSILON = 0.01

RED = "#d52b1e"

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
}


# ── small helpers ────────────────────────────────────────────────────────────

def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "org"


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
        "SELECT entity_id, bn_root, canonical_name, city, province, entity_kind FROM entities WHERE entity_id = ?",
        [entity_id],
    ).fetchone()
    return dict(zip(["entity_id", "bn_root", "canonical_name", "city", "province", "entity_kind"], row))


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
               g.program_name, g.description, g.amount_cad, g.source_dataset
        FROM grants_unified g
        JOIN entities o ON o.entity_id = g.{other_col}
        WHERE g.{this_col} = ?
        ORDER BY g.fiscal_year DESC NULLS LAST, g.amount_cad DESC NULLS LAST
    """, [entity_id]).fetchall()
    cols = ["grant_id", "fiscal_year", "other_name", "other_entity_id", "program_name",
            "description", "amount_cad", "source_dataset"]
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


# ── receipt lookups (best-effort; say so if ambiguous/not found) ────────────

def locate_federal_receipt(con, entity_id, amount, fiscal_year):
    """Best-effort lookup of the raw_grants row(s) behind a federal_gc grant.
    Joins through this entity's entity_links raw_name variants for
    source_dataset='federal_gc' against raw_grants_latest.recipient_legal_name,
    then matches on amount + fiscal year (computed from agreement_start_date
    the same way build_entity_graph.py does). Returns a dict with either the
    located row + its full amendment chain, or an explicit not-found marker."""
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

    owner_org, ref_number, recipient_name, value, start_date, prog, desc = matches[0]
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

    placeholders = ", ".join("?" for _ in variant_names)
    rows = con.execute(f"""
        WITH parsed AS (
            SELECT FPE, "Donee Name", "Total Gifts",
                   TRY_CAST(REPLACE(REPLACE(TRIM("Total Gifts"), ',', ''), '$', '') AS DOUBLE) AS val
            FROM raw_t3010_qd
            WHERE substr(regexp_replace(BN, '[^0-9A-Za-z]', ''), 1, 9) = ?
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
:root{--red:#d52b1e;--ink:#1a1a1a;--mut:#6b6b6b;--bg:#faf8f5;--card:#fff;--line:#e8e4de}
*{box-sizing:border-box;margin:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);line-height:1.5}
.wrap{max-width:920px;margin:0 auto;padding:0 20px 80px}
header{padding:40px 0 8px;display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
h1{font-size:2.1rem;letter-spacing:-.02em}
.badge{display:inline-block;background:var(--card);border:1px solid var(--line);border-radius:20px;padding:3px 12px;font-size:.78rem;color:var(--mut);margin-left:10px;vertical-align:middle}
.meta-line{color:var(--mut);margin:8px 0 0;font-size:.92rem}
h2{font-size:1.05rem;margin:40px 0 14px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);border-bottom:2px solid var(--red);display:inline-block;padding-bottom:4px}
.stats{display:flex;flex-wrap:wrap;gap:12px;margin-top:20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;flex:1 1 160px}
.stats>.drawer.open{flex-basis:100%;width:100%}
.stat b{display:block;font-size:1.5rem;letter-spacing:-.02em}
.stat span{color:var(--mut);font-size:.8rem}
.bars{display:flex;gap:5px;height:160px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 18px 30px;position:relative}
.barcol{flex:1;height:100%;display:flex;flex-direction:column-reverse;gap:1px;position:relative}
.bar-recv{background:var(--red);opacity:.85;min-height:1px}
.bar-given{background:var(--ink);opacity:.55;min-height:1px}
.barcol i{position:absolute;bottom:-22px;left:50%;transform:translateX(-50%);font-style:normal;font-size:.6rem;color:var(--mut);white-space:nowrap}
.barcol u{display:none;position:absolute;bottom:calc(100% + 6px);left:50%;transform:translateX(-50%);background:var(--ink);color:#fff;font-size:.68rem;padding:3px 8px;border-radius:5px;white-space:nowrap;z-index:3}
.barcol:hover u{display:block}
.table-scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;margin-bottom:10px}
table{min-width:100%;border-collapse:collapse;background:var(--card);font-size:.85rem}
th{background:#f1ede7;text-align:left;padding:8px 10px;white-space:nowrap}
td{padding:8px 10px;border-top:1px solid var(--line);vertical-align:top}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.src{display:inline-block;font-size:.68rem;color:var(--mut);border:1px solid var(--line);border-radius:5px;padding:1px 6px;margin-left:6px;white-space:nowrap}
.year-group{background:#f1ede7 !important;font-weight:600}
.claim{border-bottom:1px dotted #b8ad9c;cursor:pointer}
body.show-work .claim{border-bottom-color:var(--red)}
.claim:hover{background:#fdf3d7;border-bottom-color:var(--red)}
.drawer{display:none;background:#fffdf7;border:1px solid #f0dfa0;border-radius:8px;padding:12px 14px;margin:6px 0 14px;font-size:.82rem;color:#4a4a4a}
.drawer.open{display:block}
.drawer .chain{margin:6px 0;padding-left:18px}
.drawer .not-found{color:#a35200;font-style:italic}
.toggle{display:flex;align-items:center;gap:8px;font-size:.82rem;color:var(--mut);white-space:nowrap}
.switch{position:relative;width:38px;height:22px;background:var(--line);border-radius:11px;cursor:pointer;transition:background .15s}
.switch.on{background:var(--red)}
.switch i{position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;background:#fff;transition:left .15s}
.switch.on i{left:18px}
.rollup-note{color:var(--mut);font-size:.82rem;margin:8px 0 16px}
footer{margin-top:56px;color:var(--mut);font-size:.78rem;border-top:1px solid var(--line);padding-top:16px}
@media(max-width:640px){.barcol i{display:none}}
"""

JS = """
function toggleShowWork(){
  document.body.classList.toggle('show-work');
  document.getElementById('sw').classList.toggle('on');
}
function toggleDrawer(el){
  const d = document.getElementById(el.dataset.drawer);
  if (!d) return;
  if (d.classList.contains('open')) { d.classList.remove('open'); return; }
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
  d.classList.add('open');
}
"""


def render_identity_receipt(links):
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
        r = locate_federal_receipt(con, fed_entity, grant["amount_cad"], grant["fiscal_year"])
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
        return "".join(out)

    if src == "t3010_qualified_donee":
        funder_entity = grant["other_entity_id"] if direction == "received" else entity_id
        r = locate_t3010_qd_receipt(con, entity_id if direction == "received" else grant["other_entity_id"],
                                     funder_entity, grant["amount_cad"], grant["fiscal_year"])
        if not r["found"]:
            reason = r.get("reason", "not located")
            return f"<div class='not-found'>Receipt not located: {esc(reason)}.</div>"
        return (f"<p><b>Source:</b> T3010 Qualified Donees schedule &middot; <b>Filer BN:</b> {esc(r['filer_bn'])} "
                f"&middot; <b>Fiscal period end:</b> {esc(r['fpe'])}</p>"
                f"<p>Self-reported by the giving charity on its own annual return.</p>")

    # canada_council / t3010_non_qualified_donee: no raw-row lookup specified
    # by the spec's receipts table -- show what grants_unified already has.
    out = [f"<p><b>Source:</b> {esc(SOURCE_LABELS.get(src, src))}</p>"]
    if grant["program_name"]:
        out.append(f"<p><b>Program:</b> {esc(grant['program_name'])}</p>")
    if grant["description"]:
        out.append(f"<p><b>Description:</b> {esc(grant['description'])}</p>")
    return "".join(out)


def render_grants_table(con, entity_id, grants, direction, drawer_ids):
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
    current_year = object()
    for g in capped:
        if g["fiscal_year"] != current_year:
            current_year = g["fiscal_year"]
            year_label = f"FY {current_year}" if current_year is not None else "Year unknown"
            rows.append(f"<tr class='year-group'><td colspan='4'>{esc(year_label)}</td></tr>")
        drawer_id = f"drawer-{len(drawer_ids)}"
        drawer_ids.append((drawer_id, lambda g=g: render_grant_receipt(con, entity_id, g, direction)))
        other_label = "Funder" if direction == "received" else "Recipient"
        rows.append(
            f"<tr><td><span class='claim' data-drawer='{drawer_id}' onclick='toggleDrawer(this)'>"
            f"{esc(english_name(g['other_name']))}</span>"
            f"<span class='src'>{esc(SOURCE_LABELS.get(g['source_dataset'], g['source_dataset']))}</span></td>"
            f"<td>{esc(g['program_name']) if g['program_name'] else '—'}</td>"
            f"<td class='num'>{esc(fmt_money_precise(g['amount_cad']))}</td>"
            f"<td>{esc(other_label)}</td></tr>"
        )
    return (f"<div class='table-scroll'><table><thead><tr><th>Organization</th><th>Program</th>"
            f"<th class='num'>Amount</th><th>Role</th></tr></thead><tbody>{''.join(rows)}</tbody>"
            f"</table></div>{rollup_note}")


def render_timeline(years, received_by_year, given_by_year):
    if not years:
        return ""
    max_v = max(
        max(received_by_year.get(y, 0) or 0, given_by_year.get(y, 0) or 0)
        for y in years
    ) or 1
    cols = []
    for y in years:
        rv = received_by_year.get(y, 0) or 0
        gv = given_by_year.get(y, 0) or 0
        rh = max(2, 100 * rv / max_v) if rv else 0
        gh = max(2, 100 * gv / max_v) if gv else 0
        tip = f"FY{y}: received {fmt_money(rv)}" + (f" &middot; given {fmt_money(gv)}" if gv else "")
        cols.append(
            f"<div class='barcol'>"
            f"<div class='bar-given' style='height:{gh}%'></div>"
            f"<div class='bar-recv' style='height:{rh}%'></div>"
            f"<u>{tip}</u><i>{y}</i></div>"
        )
    return f"<div class='bars'>{''.join(cols)}</div>"


def render_page(con, entity_id):
    entity = fetch_entity(con, entity_id)
    role = fetch_role_summary(con, entity_id)
    financials = fetch_financials(con, entity_id)
    links = fetch_entity_links(con, entity_id)
    received = fetch_grants(con, entity_id, "received")
    given = fetch_grants(con, entity_id, "given")
    years, received_by_year, given_by_year = fetch_timeline(con, entity_id)

    name_display = english_name(entity["canonical_name"])
    kind_label = KIND_LABELS.get(entity["entity_kind"], "Organization")
    loc = ", ".join(p for p in (entity["city"], entity["province"]) if p)

    drawer_ids = []  # [(drawer_id, render_fn), ...]

    identity_drawer_id = "drawer-identity"
    identity_html = render_identity_receipt(links)

    header_meta = []
    if loc:
        header_meta.append(esc(loc))
    if entity["bn_root"]:
        header_meta.append(f"BN {esc(entity['bn_root'])}")

    # ── stat row ──
    stats = []
    if role["total_received"]:
        stats.append((fmt_money(role["total_received"]), "total received", "recv-total"))
    if role["total_given"]:
        stats.append((fmt_money(role["total_given"]), "total given", "given-total"))
    if role["n_grants_received"]:
        stats.append((fmt_int(role["n_grants_received"]), "grants received", None))
    if role["n_grants_given"]:
        stats.append((fmt_int(role["n_grants_given"]), "grants given", None))
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

    timeline_html = render_timeline(years, received_by_year, given_by_year)
    received_html = render_grants_table(con, entity_id, received, "received", drawer_ids)
    given_html = render_grants_table(con, entity_id, given, "given", drawer_ids)

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
<title>{esc(name_display)} — Canadian Nonprofit Data</title>
<style>{CSS}</style></head><body>
<div class="wrap">
<header>
<div>
<h1><span class="claim" data-drawer="{identity_drawer_id}" onclick="toggleDrawer(this)">{esc(name_display)}</span>
<span class="badge">{esc(kind_label)}</span></div>
<p class="meta-line">{" &middot; ".join(header_meta) if header_meta else ""}</p>
</div>
<div class="toggle" onclick="toggleShowWork()">
<div class="switch" id="sw"><i></i></div>
Show your work
</div>
</header>
{"".join(sections)}
<div id="drawers">{"".join(drawer_html)}</div>
<footer>Generated {esc(generated)} from the Canadian Nonprofit Data entity graph
(federal Grants &amp; Contributions, CRA T3010, Canada Council for the Arts).
Matching methodology &amp; limitations: see
<a href="../entity-resolution-methodology.md">entity-resolution-methodology.md</a>.</footer>
</div>
<script>{JS}</script>
</body></html>"""


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_page(db_path, entity_id, out_path=None):
    con = open_db(db_path)
    try:
        entity = fetch_entity(con, entity_id)
        page = render_page(con, entity_id)
    finally:
        con.close()

    if out_path is None:
        os.makedirs(ORGS_DIR, exist_ok=True)
        # Some T3010 canonical names use "/" (not "|") as the EN/FR separator,
        # which english_name() doesn't split (that's a display decision -- see
        # docs/org-page-spec.md -- since a blanket split risks corrupting
        # legitimate single-language names containing a slash). For the
        # *filename* specifically, a needlessly long bilingual slug is a pure
        # cosmetic cost with no such risk, so trim it here only.
        name_for_slug = english_name(entity["canonical_name"]).split("/", 1)[0].strip()
        out_path = os.path.join(ORGS_DIR, f"{slugify(name_for_slug)}.html")
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
    args = parser.parse_args(argv)

    if not any([args.name, args.entity_id is not None, args.bn]):
        parser.error("provide a name, --entity-id, or --bn")

    con = open_db(args.db)
    try:
        entity_id = resolve_entity_id(con, args)
    finally:
        con.close()

    out_path = build_page(args.db, entity_id, args.out)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
