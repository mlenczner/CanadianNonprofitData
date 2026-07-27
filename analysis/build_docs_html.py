"""Render selected docs/*.md files to self-contained, styled HTML twins for the
static GitHub Pages site (docs/*.md is served as raw text by Pages -- there's no
Jekyll layout configured, so it shows up as unrendered markdown source in a
browser). The .md source files stay as the source of truth (and are what
analysis/webapp.py's live /entity-resolution-methodology.md route still serves
directly); this script produces an additional .html file next to each one.

Requires the `markdown` package (added to requirements.txt).

Run:
    python3 analysis/build_docs_html.py
"""
import os
import re

import markdown

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
REPO_BLOB_BASE = "https://github.com/mlenczner/CanadianNonprofitData/blob/main/"

DRAFT_BANNER_TEXT = "DRAFT — research prototype, not for circulation"
DRAFT_FULL_TEXT = (
    "DRAFT — research prototype. This is an unreleased working draft produced for "
    "research purposes only. Figures are derived from public data using experimental "
    "methods, contain known data-quality limitations, and have not been reviewed for "
    "publication. Do not cite, circulate, or rely on any figure or claim in this document."
)

# Every file rendered to HTML here, so cross-links between them (below) resolve
# to a working .html page instead of falling back to raw-markdown .md links.
DOC_FILES = [
    "data-publishing-problems.md",
    "entity-resolution-methodology.md",
    "questions-and-insights.md",
    "why-this-matters.md",
    "dept-compliance-report.md",
    "geo-date-anomaly-report.md",
]

# Leading blockquote-style draft banner already present in some source files
# (dept-compliance-report.md, geo-date-anomaly-report.md, why-this-matters.md) --
# stripped from the converted body since the fixed-position .draft-banner div
# below covers it for every rendered page, banner-in-source or not.
LEADING_BANNER_RE = re.compile(r"^>\s*\*\*DRAFT.*?document\.\*?\*?\s*\n+", re.S)

CSS = """
:root{--red:#d52b1e;--ink:#1a1a1a;--mut:#6b6b6b;--bg:#faf8f5;--card:#fff;--line:#e8e4de}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);line-height:1.6;padding-top:40px;margin:0}
.draft-banner{position:fixed;top:0;left:0;right:0;z-index:1000;background:#fff3cd;color:#8a6d00;font-weight:700;text-align:center;padding:8px 12px;font-size:.85rem;border-bottom:2px solid #8a6d00}
.wrap{max-width:860px;margin:0 auto;padding:40px 24px 80px}
.back{color:var(--mut);text-decoration:none;font-size:.85rem}
.back:hover{color:var(--red)}
h1{font-size:2rem;letter-spacing:-.02em;margin:18px 0 8px}
h2{font-size:1.3rem;margin:40px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--red)}
h3{font-size:1.05rem;margin:26px 0 8px}
p{margin:10px 0}
a{color:var(--red)}
table{border-collapse:collapse;width:100%;margin:16px 0;font-size:.88rem;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:7px 10px;border-top:1px solid var(--line);text-align:left}
th{background:#f1ede7;font-weight:700}
code{background:#f1ede7;border-radius:4px;padding:1px 5px;font-size:.88em}
blockquote{margin:14px 0;padding:10px 16px;border-left:3px solid var(--red);background:var(--card);color:var(--mut)}
hr{border:none;border-top:1px solid var(--line);margin:28px 0}
ul,ol{padding-left:22px}
li{margin:4px 0}
strong{color:var(--ink)}
footer{margin-top:56px;color:var(--mut);font-size:.78rem;border-top:1px solid var(--line);padding-top:16px}
"""


def rewrite_links(html_body):
    # Cross-links to other converted docs: file.md -> file.html
    for fn in DOC_FILES:
        html_body = html_body.replace(f'href="{fn}"', f'href="{fn[:-3]}.html"')
    # Relative links that climb out of docs/ (e.g. ../analysis/foo.py) point at
    # repo source, not another site page -- resolve those to a real GitHub blob
    # URL instead, since a relative link like that 404s once served from Pages.
    html_body = re.sub(
        r'href="\.\./([^"]+)"',
        lambda m: f'href="{REPO_BLOB_BASE}{m.group(1)}"',
        html_body,
    )
    return html_body


def convert(md_filename):
    src_path = os.path.join(DOCS, md_filename)
    with open(src_path, encoding="utf-8") as f:
        text = f.read()

    text = LEADING_BANNER_RE.sub("", text, count=1)

    title_match = re.search(r"^#\s+(.+)$", text, re.M)
    title = title_match.group(1).strip() if title_match else md_filename

    body_html = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    body_html = rewrite_links(body_html)

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>[DRAFT] {title} — Canadian Nonprofit Data</title>
<style>{CSS}</style></head><body>
<div class="draft-banner">{DRAFT_BANNER_TEXT}</div>
<div class="wrap">
<a class="back" href="index.html">&larr; Canadian Nonprofit Data</a>
{body_html}
<footer><p>{DRAFT_FULL_TEXT}</p></footer>
</div>
</body></html>"""

    out_path = os.path.join(DOCS, md_filename[:-3] + ".html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path, len(html)


if __name__ == "__main__":
    for fn in DOC_FILES:
        path, size = convert(fn)
        print(f"wrote {path} ({size:,} bytes)")
