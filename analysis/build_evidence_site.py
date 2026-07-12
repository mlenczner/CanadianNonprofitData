"""
Evidence Encyclopedia Demo Site Generator

Generates docs/evidence/index.html plus one page per intervention that has
at least one non-"from_model_knowledge" evidence entry, from
evidence/evidence-spine-seed.yaml and evidence/seed-classifications-housing-canada.csv.
See docs/evidence-site-spec.md for the full spec (including a "Decisions"
note at the bottom for anything the spec didn't cover).

Two-page-type design, reusing analysis/org_page.py's CSS/JS/drawer
mechanics and draft-banner text wholesale rather than rewriting them:
  - Intervention pages: mechanism, evidence side by side (never averaged,
    never reproduced as content -- rating/finding quoted + linked out
    only), a standing transfer-caveat box, and Canadian organizations
    identified as delivering it (from the CSV), each a claim with a
    receipt drawer.
  - Index page: framing, cards for published intervention pages, and an
    honest "in progress / not yet shown" list for interventions whose
    evidence is all unverified.

Run with:
    python analysis/build_evidence_site.py
"""

import os
import re
import sys
from datetime import datetime

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analysis.org_page import (  # noqa: E402
    CSS, JS, DRAFT_BANNER_TEXT, DRAFT_FULL_TEXT,
    fmt_money, fmt_int, slugify, english_name, esc,
)

EVIDENCE_DIR = os.path.join(ROOT, "evidence")
YAML_PATH = os.path.join(EVIDENCE_DIR, "evidence-spine-seed.yaml")
CSV_PATH = os.path.join(EVIDENCE_DIR, "seed-classifications-housing-canada.csv")
OUT_DIR = os.path.join(ROOT, "docs", "evidence")
ORGS_DIR = os.path.join(ROOT, "docs", "orgs")

ORG_SCALE_CAP = 50

# Every source URL in the YAML lives only in an inline `#` comment on the
# `status:` line -- PyYAML discards comments entirely, so they're recovered
# separately by scanning the raw text (see extract_status_comments()).
LINK_RE = re.compile(r"((?:[a-z0-9-]+\.)+(?:com|org|gov|ca|uk|net)(?:/[^\s,;)]*)?)", re.I)


# ── data loading ─────────────────────────────────────────────────────────────

def load_interventions(yaml_path=YAML_PATH):
    with open(yaml_path, encoding="utf-8") as f:
        raw_text = f.read()
    data = yaml.safe_load(raw_text)
    comments_by_id = extract_status_comments(raw_text)
    interventions = []
    for item in data["interventions"]:
        comments = comments_by_id.get(item["id"], [])
        evidence = []
        for i, entry in enumerate(item.get("evidence", [])):
            comment = comments[i] if i < len(comments) else None
            evidence.append({**entry, "comment": comment, "parsed_status": parse_status(entry["status"])})
        interventions.append({**item, "evidence": evidence})
    return interventions


def extract_status_comments(yaml_text):
    """{intervention_id: [comment_or_None, ...]} in evidence-entry order.
    PyYAML's data model has no concept of comments, so the source URL/note
    that lives in `status: X  # comment` has to be recovered from the raw
    text by tracking which intervention id and evidence entry we're inside."""
    comments = {}
    current_list = None
    for line in yaml_text.splitlines():
        m_id = re.match(r"  - id:\s*(\S+)", line)
        if m_id:
            current_list = comments.setdefault(m_id.group(1), [])
            continue
        m_status = re.match(r"\s+status:\s*\S+(?:\s*#\s*(.*))?\s*$", line)
        if m_status and current_list is not None:
            current_list.append(m_status.group(1))
    return comments


def parse_status(status):
    """Status values are the contract (see spec): verified_YYYY-MM-DD,
    from_model_knowledge, editorial_position. The real seed file also has a
    verified_tonight variant (no parseable date) -- treated as verified with
    no date rather than raising, since the "verified" kind is what matters
    for rendering (never conflate with from_model_knowledge)."""
    if status == "from_model_knowledge":
        return {"kind": "unverified"}
    if status == "editorial_position":
        return {"kind": "editorial"}
    if status.startswith("verified_"):
        suffix = status[len("verified_"):]
        date = suffix if re.match(r"^\d{4}-\d{2}-\d{2}$", suffix) else None
        return {"kind": "verified", "date": date, "raw_suffix": suffix}
    raise ValueError(f"Unknown evidence status: {status!r}")


def load_csv_by_category(csv_path=CSV_PATH):
    import csv as csv_module
    rows_by_category = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv_module.DictReader(f):
            rows_by_category.setdefault(row["category"], []).append(row)
    return rows_by_category


# ── business logic ───────────────────────────────────────────────────────────

def qualifies_for_page(intervention):
    """An intervention gets a page if at least one evidence entry is not
    from_model_knowledge -- verified_* or editorial_position both count
    (emergency_shelter's only entry is editorial_position and still must get
    a page per the spec's Definition of Done; see the Decisions note for how
    this reconciles with the spec's shorter "verified_* only" phrasing)."""
    return any(e["parsed_status"]["kind"] != "unverified" for e in intervention["evidence"])


def find_transfer_example(evidence_entries):
    """A concrete transfer-failure example to cite in the standing caveat
    box, when the YAML has one (currently only NFP's Building Blocks entry)."""
    for e in evidence_entries:
        if re.search(r"context-transfer|null result", e.get("finding", ""), re.I):
            return e
    return None


def org_page_link(recipient_legal_name, orgs_dir=ORGS_DIR):
    """None if no generated org profile page exists for this org (checked on
    the filesystem, per spec); otherwise the relative link to it."""
    if not recipient_legal_name:
        return None
    name_for_slug = english_name(recipient_legal_name).split("/", 1)[0].strip()
    if not name_for_slug:
        return None
    slug = slugify(name_for_slug)
    if os.path.exists(os.path.join(orgs_dir, f"{slug}.html")):
        return f"../orgs/{slug}.html"
    return None


# ── HTML rendering ───────────────────────────────────────────────────────────

EXTRA_CSS = """
.chip{display:inline-block;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:2px 10px;font-size:.76rem;color:var(--mut);margin:2px 4px 2px 0}
.aliases{margin-top:8px}
.evidence-grid{display:flex;flex-wrap:wrap;gap:14px;margin-top:16px}
.ev-card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;flex:1 1 260px}
.ev-card.muted{opacity:.8}
.ev-card.editorial{background:#fdf3d7;border-color:#e8d68a;flex-basis:100%}
.ev-card h3{font-size:.92rem;margin-bottom:6px}
.ev-card p{font-size:.88rem;margin:6px 0 0}
.badge-verified{display:inline-block;background:#e2f2e4;color:#1b7a2d;border-radius:6px;padding:2px 8px;font-size:.74rem;font-weight:600}
.badge-unverified{display:inline-block;background:#fde4cf;color:#a35200;border-radius:6px;padding:2px 8px;font-size:.74rem;font-weight:600}
.editorial-note{font-style:italic;color:var(--ink)}
.ev-note{color:var(--mut);font-size:.8rem;margin-top:6px}
.caveat-box{background:#fdf3d7;border:1px solid #e8d68a;border-radius:10px;padding:14px 18px;margin:16px 0;font-size:.9rem}
.progress-list{list-style:none;margin-top:12px}
.progress-list li{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:8px}
.progress-list .why{color:var(--mut);font-size:.85rem;margin-top:4px}
.index-cards{display:flex;flex-wrap:wrap;gap:14px;margin-top:16px}
.index-card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;flex:1 1 240px;text-decoration:none;color:inherit;display:block}
.index-card h3{font-size:1.05rem;margin-bottom:6px;color:var(--ink)}
.index-card p{color:var(--mut);font-size:.85rem;margin-bottom:8px}
.framing{max-width:680px;color:var(--ink)}
.org-note{color:var(--mut);font-size:.9rem;margin-top:12px}
"""
FULL_CSS = CSS + EXTRA_CSS

PAGE_HEAD = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>[DRAFT] {title}</title>
<style>{css}</style></head><body>
<div class="draft-banner">{banner}</div>
<div class="draft-watermark">DRAFT</div>
<div class="wrap">
"""

PAGE_FOOTER = """<footer><p class="draft-footer-notice">{full_text}</p>
Generated {generated} from evidence/evidence-spine-seed.yaml and
evidence/seed-classifications-housing-canada.csv (federal Grants &amp;
Contributions text classification). See
<a href="../evidence-site-spec.md">evidence-site-spec.md</a> for methodology.</footer>
</div>
<script>{js}</script>
</body></html>"""


def render_status_badge(parsed):
    kind = parsed["kind"]
    if kind == "verified":
        label = f"Verified &middot; {parsed['date']}" if parsed["date"] else f"Verified &mdash; {esc(parsed['raw_suffix'])}"
        return f"<span class='badge-verified'>{label}</span>"
    if kind == "unverified":
        return ("<span class='badge-unverified'>UNVERIFIED &mdash; drafted from model knowledge, "
                "not yet checked against the source</span>")
    return ""  # editorial entries are rendered as a distinct card, not a badge


def render_comment_note(comment):
    if not comment:
        return ""
    m = LINK_RE.search(comment)
    if not m:
        return f"<p class='ev-note'>{esc(comment)}</p>"
    href = m.group(1) if m.group(1).startswith("http") else f"https://{m.group(1)}"
    before, url_text, after = comment[:m.start(1)], m.group(1), comment[m.end(1):]
    link = f'<a href="{esc(href)}" target="_blank" rel="noopener">{esc(url_text)}</a>'
    return f"<p class='ev-note'>{esc(before)}{link}{esc(after)}</p>"


def render_evidence_card(entry):
    parsed = entry["parsed_status"]
    if parsed["kind"] == "editorial":
        return (f"<div class='ev-card editorial'><h3>{esc(entry['source'])}</h3>"
                f"<p class='editorial-note'>{esc(entry['finding'])}</p>"
                f"{render_comment_note(entry.get('comment'))}</div>")
    muted = " muted" if parsed["kind"] == "unverified" else ""
    note_field = entry.get("note")
    note_html = f"<p class='ev-note'>{esc(note_field)}</p>" if note_field else ""
    return (f"<div class='ev-card{muted}'><h3>{esc(entry['source'])}</h3>"
            f"<p>{esc(entry['finding'])}</p>"
            f"<p>{render_status_badge(parsed)}</p>"
            f"{note_html}"
            f"{render_comment_note(entry.get('comment'))}</div>")


CAVEAT_GENERIC = (
    "Evidence summarized above comes from specific places, populations, and delivery "
    "conditions -- not from Canada in general. Effects measured in one system (funding "
    "model, staffing ratios, target population) often shrink, disappear, or reverse when "
    "the same program moves to a different one. A registry rating or systematic review "
    "answers “does this work somewhere, under study conditions” -- not “will "
    "this work here, delivered by this organization, for this population.” Treat every "
    "rating above as a starting point for local evaluation, not a guarantee."
)


def render_caveat_box(evidence_entries):
    example = find_transfer_example(evidence_entries)
    extra = ""
    if example:
        extra = (f"<p><b>Concrete example:</b> {esc(example['source'])} &mdash; "
                 f"{esc(example['finding'])}</p>")
    return f"<div class='caveat-box'><p>{esc(CAVEAT_GENERIC)}</p>{extra}</div>"


def render_org_table(rows, orgs_dir, drawer_ids):
    if not rows:
        return ""
    rows_sorted = sorted(rows, key=lambda r: float(r["total_cad"] or 0), reverse=True)
    total_n = len(rows_sorted)
    capped = rows_sorted[:ORG_SCALE_CAP]
    rollup_note = ""
    if total_n > ORG_SCALE_CAP:
        rollup_note = f"<p class='rollup-note'>Showing {ORG_SCALE_CAP} of {total_n} organizations, by total dollars.</p>"

    out_rows = []
    for row in capped:
        name = english_name(row["recipient_legal_name"]) or "(unnamed)"
        city = english_name(row.get("city") or "")
        province = row.get("province") or ""
        loc = ", ".join(p for p in (city, province) if p)
        n_grants = fmt_int(int(row["n_grants"])) if row.get("n_grants") else "—"
        total = fmt_money(float(row["total_cad"])) if row.get("total_cad") else "—"
        first_year, last_year = row.get("first_year"), row.get("last_year")
        years = f"{first_year}–{last_year}" if first_year and last_year and first_year != last_year else (first_year or "—")

        drawer_id = f"drawer-org-{len(drawer_ids)}"
        drawer_ids.append((drawer_id, (
            f"<p><b>Description:</b> “{esc(row.get('receipt_description_snippet', ''))}”</p>"
            f"<p><b>Ref:</b> {esc(row.get('receipt_ref_number', '—'))} &middot; "
            f"<b>Funder:</b> {esc(english_name(row.get('example_funder', '')))}</p>"
            f"<p class='ev-note'>Classification: matched on category terms in federal grant "
            f"descriptions, latest-amendment deduped.</p>"
        )))
        link = org_page_link(row["recipient_legal_name"], orgs_dir)
        profile_link = f" <a href=\"{esc(link)}\" class='src'>profile &rarr;</a>" if link else ""
        out_rows.append(
            f"<tr><td><span class='claim' data-drawer='{drawer_id}' onclick='toggleDrawer(this)'>"
            f"{esc(name)}</span>{profile_link}</td>"
            f"<td>{esc(loc)}</td><td class='num'>{n_grants}</td>"
            f"<td class='num'>{total}</td><td>{esc(years)}</td></tr>"
        )
    return (f"<div class='table-scroll'><table><thead><tr><th>Organization</th><th>Location</th>"
            f"<th class='num'>Grants</th><th class='num'>Total</th><th>Years</th></tr></thead>"
            f"<tbody>{''.join(out_rows)}</tbody></table></div>{rollup_note}")


def render_intervention_page(intervention, csv_rows_by_category, orgs_dir=ORGS_DIR):
    name = intervention["name"]
    aliases = intervention.get("aliases", [])
    mechanism = intervention.get("mechanism", "").strip()
    evidence = intervention["evidence"]
    org_rows = csv_rows_by_category.get(intervention["id"], [])

    drawer_ids = []
    alias_html = "".join(f"<span class='chip'>{esc(a)}</span>" for a in aliases)
    evidence_html = "".join(render_evidence_card(e) for e in evidence)
    caveat_html = render_caveat_box(evidence)
    if org_rows:
        org_section = render_org_table(org_rows, orgs_dir, drawer_ids)
    else:
        relevance = intervention.get("canadian_relevance", "")
        org_section = (f"<p class='org-note'>No Canadian organizations identified from federal "
                        f"grant text yet. {esc(relevance)}</p>")

    drawer_html = "".join(f"<div class='drawer' id='{did}'>{html}</div>" for did, html in drawer_ids)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    body = f"""
<header>
<div><h1>{esc(name)}</h1><div class="aliases">{alias_html}</div></div>
<div class="toggle" onclick="toggleShowWork()"><div class="switch" id="sw"><i></i></div>Show your work</div>
</header>
<h2>What it is</h2>
<p>{esc(mechanism)}</p>
<h2>Evidence, side by side</h2>
<p class="ev-note">Ratings are cited as facts with links to the source registry, never averaged into a
composite score. Where sources disagree, they are shown side by side.</p>
<div class="evidence-grid">{evidence_html}</div>
<h2>Will it work here?</h2>
{caveat_html}
<h2>Organizations identified as delivering this (Canada)</h2>
{org_section}
<div id="drawers">{drawer_html}</div>
"""
    return (PAGE_HEAD.format(title=f"{esc(name)} — Evidence Encyclopedia", css=FULL_CSS, banner=esc(DRAFT_BANNER_TEXT))
            + body
            + PAGE_FOOTER.format(full_text=esc(DRAFT_FULL_TEXT), generated=esc(generated), js=JS))


INDEX_FRAMING = (
    "Funders often conflate two different questions: does this intervention work, anywhere, "
    "under any conditions -- and will it work here, delivered by this organization, for this "
    "population, under Canadian funding and service conditions. This site keeps them separate. "
    "Each intervention page shows how the major evidence registries rate it, cited as facts with "
    "links out, never averaged into a single score; when registries disagree, they sit side by "
    "side rather than being smoothed into a composite. Where Canadian organizations have been "
    "identified as delivering the intervention from federal grant records, they're listed with "
    "receipts -- the funder's own description, the grant reference number, and how the match was "
    "made. This is a small, honest demo, not a finished encyclopedia: some interventions have real "
    "Canadian evaluations that simply haven't been read and checked yet, and that gap is shown, "
    "not hidden, below."
)


def render_index(interventions, csv_rows_by_category):
    published = [i for i in interventions if qualifies_for_page(i)]
    in_progress = [i for i in interventions if not qualifies_for_page(i)]

    cards = []
    for i in published:
        mechanism_line = i.get("mechanism", "").strip().split(". ")[0].rstrip(".") + "."
        n_registries = len(i["evidence"])
        n_orgs = len(csv_rows_by_category.get(i["id"], []))
        cards.append(
            f"<a class='index-card' href='{esc(i['id'])}.html'><h3>{esc(i['name'])}</h3>"
            f"<p>{esc(mechanism_line)}</p>"
            f"<span class='chip'>{n_registries} registries</span> "
            f"<span class='chip'>{n_orgs} Canadian orgs identified</span></a>"
        )

    progress_items = []
    for i in in_progress:
        progress_items.append(
            f"<li><b>{esc(i['name'])}</b>"
            f"<div class='why'>All evidence entries are unverified &mdash; "
            f"not yet checked against source.</div></li>"
        )

    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = f"""
<header><h1>Evidence Encyclopedia &mdash; Demo</h1></header>
<p class="framing">{esc(INDEX_FRAMING)}</p>
<h2>Interventions</h2>
<div class="index-cards">{"".join(cards)}</div>
<h2>In progress, not yet shown</h2>
<ul class="progress-list">{"".join(progress_items)}</ul>
"""
    return (PAGE_HEAD.format(title="Evidence Encyclopedia — Demo", css=FULL_CSS, banner=esc(DRAFT_BANNER_TEXT))
            + body
            + PAGE_FOOTER.format(full_text=esc(DRAFT_FULL_TEXT), generated=esc(generated), js=JS))


# ── build ────────────────────────────────────────────────────────────────────

def build_site(yaml_path=YAML_PATH, csv_path=CSV_PATH, out_dir=OUT_DIR, orgs_dir=ORGS_DIR):
    interventions = load_interventions(yaml_path)
    csv_rows_by_category = load_csv_by_category(csv_path)

    os.makedirs(out_dir, exist_ok=True)
    written = []

    index_html = render_index(interventions, csv_rows_by_category)
    index_path = os.path.join(out_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_html)
    written.append(index_path)

    for intervention in interventions:
        if not qualifies_for_page(intervention):
            continue
        page_html = render_intervention_page(intervention, csv_rows_by_category, orgs_dir)
        page_path = os.path.join(out_dir, f"{intervention['id']}.html")
        with open(page_path, "w", encoding="utf-8") as f:
            f.write(page_html)
        written.append(page_path)

    return written


def main():
    written = build_site()
    for path in written:
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
