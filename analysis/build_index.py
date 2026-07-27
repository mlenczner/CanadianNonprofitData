"""Build docs/index.html, the landing page for the static GitHub Pages site.

Links out to the self-contained HTML reports in docs/ plus the sample org/grant/
evidence pages already committed there. Org and evidence sample links are
discovered from the files actually present (title extracted from each page's
<title> tag) rather than hand-typed, so this stays in sync automatically.

Run:
    python3 analysis/build_index.py
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")

DRAFT_BANNER_TEXT = "DRAFT — research prototype, not for circulation"
DRAFT_FULL_TEXT = (
    "DRAFT — research prototype. This is an unreleased working draft produced for "
    "research purposes only. Figures are derived from public data using experimental "
    "methods, contain known data-quality limitations, and have not been reviewed for "
    "publication. Do not cite, circulate, or rely on any figure or claim in this document."
)

# Known bad sample: filename says Ontario Trillium Foundation but the page's actual
# title is a different, unrelated org ("Commonwealth Lawn Bowling Club of Edmonton")
# -- a real content bug in analysis/org_page.py's sample generation, not something
# to link to from the front page. Excluded here rather than silently worked around
# elsewhere; flagged separately for a fix.
SKIP_ORG_FILES = {"ontario-trillium-foundation.html"}

TITLE_RE = re.compile(r"<title>\[DRAFT\]\s*(.*?)\s*(?:—[^—]*)?</title>")


def extract_title(path):
    with open(path, encoding="utf-8") as f:
        head = f.read(2000)
    m = re.search(r"<title>(.*?)</title>", head, re.S)
    if not m:
        return os.path.basename(path)
    title = m.group(1)
    title = title.replace("[DRAFT] ", "")
    title = re.sub(r"\s*&mdash;.*$", "", title)
    title = re.sub(r"\s*—[^—]*$", "", title)
    title = title.replace("&#x27;", "'").replace("&eacute;", "e")
    return title.strip()


def list_samples(subdir, skip=frozenset(), prefix_filter=None):
    d = os.path.join(DOCS, subdir)
    out = []
    for fn in sorted(os.listdir(d)):
        if fn == "index.html" or fn in skip:
            continue
        if prefix_filter and not fn.startswith(prefix_filter):
            continue
        path = os.path.join(d, fn)
        out.append({"href": f"{subdir}/{fn}", "title": extract_title(path)})
    return out


org_samples = list_samples("orgs", skip=SKIP_ORG_FILES)
evidence_samples = list_samples("evidence")
grant_samples = list_samples("grants", prefix_filter="grant-")

REPORTS = [
    ("data-quality-rankings.html", "Federal Grants & Contributions: Disclosure Quality",
     "Policy brief on disclosure completeness across federal departments — executive "
     "summary, recommendations, Post-December-2025 compliance, and recurring data "
     "patterns with real records."),
    ("grants-dashboard.html", "Federal Grants & Contributions: Overview",
     "Totals, department-by-department disclosure-completeness metrics, and a "
     "fiscal-year funding chart."),
]

DOC_LINKS = [
    ("data-publishing-problems.html", "Data Publishing Problems",
     "Full technical findings behind the disclosure-quality report."),
    ("entity-resolution-methodology.html", "Entity Resolution Methodology",
     "How organizations are matched and de-duplicated across sources."),
    ("questions-and-insights.html", "Questions & Possible Insights",
     "Working list of open research questions."),
    ("why-this-matters.html", "Policy Context",
     "Background on Canada's open-government commitments relevant to this data."),
    ("dept-compliance-report.html", "Department Compliance Report",
     "Per-department breakdown behind the disclosure-quality findings."),
    ("geo-date-anomaly-report.html", "Date & Geography Anomaly Report",
     "Records with implausible dates or invalid geography codes."),
]

CSS = """
:root{--red:#d52b1e;--ink:#1a1a1a;--mut:#6b6b6b;--bg:#faf8f5;--card:#fff;--line:#e8e4de}
*{box-sizing:border-box;margin:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);line-height:1.5;padding-top:40px}
.draft-banner{position:fixed;top:0;left:0;right:0;z-index:1000;background:#fff3cd;color:#8a6d00;font-weight:700;text-align:center;padding:8px 12px;font-size:.85rem;border-bottom:2px solid #8a6d00}
.wrap{max-width:900px;margin:0 auto;padding:0 24px 80px}
header{padding:56px 0 8px}
h1{font-size:2.2rem;letter-spacing:-.02em}
.sub{color:var(--mut);margin:10px 0 0;max-width:640px}
h2{font-size:1.05rem;margin:44px 0 14px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);border-bottom:2px solid var(--red);display:inline-block;padding-bottom:4px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
a.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;text-decoration:none;color:inherit;display:block}
a.card:hover{border-color:var(--red)}
a.card h3{font-size:1.02rem;margin-bottom:6px;color:var(--ink)}
a.card p{color:var(--mut);font-size:.85rem}
.chips{display:flex;flex-wrap:wrap;gap:8px}
a.chip{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:6px 14px;font-size:.85rem;text-decoration:none;color:var(--ink)}
a.chip:hover{border-color:var(--red);color:var(--red)}
.note{color:var(--mut);font-size:.85rem;margin-top:10px}
footer{margin-top:56px;color:var(--mut);font-size:.78rem;border-top:1px solid var(--line);padding-top:16px}
"""


def render_cards(items):
    return "".join(
        f'<a class="card" href="{href}"><h3>{title}</h3><p>{desc}</p></a>'
        for href, title, desc in items
    )


def render_chips(items):
    return "".join(f'<a class="chip" href="{it["href"]}">{it["title"]}</a>' for it in items)


report_cards = render_cards([(href, title, desc) for href, title, desc in REPORTS])
doc_cards = render_cards([(href, title, desc) for href, title, desc in DOC_LINKS])

org_chips = render_chips(org_samples)
evidence_chips = render_chips(evidence_samples)
grant_chips = render_chips(grant_samples)

html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>[DRAFT] Canadian Nonprofit Data</title>
<style>{CSS}</style></head><body>
<div class="draft-banner">{DRAFT_BANNER_TEXT}</div>
<div class="wrap">
<header>
<h1>Canadian Nonprofit Data</h1>
<p class="sub">Analysis of the Government of Canada's Proactive Disclosure — Grants and
Contributions dataset, cross-referenced against the CRA charity registry and provincial/
federal nonprofit incorporation registries.</p>
</header>

<h2>Reports</h2>
<div class="cards">{report_cards}</div>

<h2>Organization profiles (sample)</h2>
<div class="chips">{org_chips}</div>
<p class="note">These are individually generated sample pages, not a search index — the
live application (not part of this static site) supports searching every organization in
the dataset.</p>

<h2>Grant text search</h2>
<div class="chips"><a class="chip" href="grants/index.html">Search grant descriptions &rarr;</a>{grant_chips}</div>

<h2>Evidence Encyclopedia (demo)</h2>
<div class="chips"><a class="chip" href="evidence/index.html">Evidence Encyclopedia index &rarr;</a>{evidence_chips}</div>

<h2>Documentation</h2>
<div class="cards">{doc_cards}</div>

<footer>
<p>Source: <a href="https://github.com/mlenczner/CanadianNonprofitData">github.com/mlenczner/CanadianNonprofitData</a>.
{DRAFT_FULL_TEXT}</p>
</footer>
</div>
</body></html>"""

out_path = os.path.join(DOCS, "index.html")
with open(out_path, "w", encoding="utf-8") as f:
    f.write(html)
print(f"wrote {out_path} ({len(html):,} bytes)")
print(f"org samples: {len(org_samples)}, evidence samples: {len(evidence_samples)}, grant samples: {len(grant_samples)}")
