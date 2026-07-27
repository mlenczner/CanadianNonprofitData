"""Build a self-contained HTML data-publishing-quality report from grants.csv.

Written for an external policy audience (Treasury Board's open-government /
open-data leadership), not just internal receipts -- so this generates an
executive summary and a set of concrete recommendations directly from the
same query results that drive the evidence tables below them, an explicit
"Post-December 2025 mandatory fields" section (the newest, most directly
TBS-attributable compliance question this dataset can answer), and an
expanded, generalized specimen jar (batch-reporting recipients, extreme
amendment volume, placeholder/sentinel dates, nominal-dollar awards,
punctuation-only descriptions) instead of one-off anecdotes. No blended
score or letter grade, and no "worst culprits" framing -- see AGENTS.md for
why. Latest-amendment dedup per (owner_org, ref_number), consistent with
analysis/build_entity_graph.py -- this matters enough to earn its own
methodology note below, since one frequently-cited "negative value" example
from an earlier draft of this analysis (a -$74M WHO record) turned out to be
a superseded amendment, not a live problem, once that dedup was applied.

Run:
    python3 build_quality_report.py /path/to/grants.csv /path/to/output.html
"""
import duckdb, json, sys
from datetime import datetime

DRAFT_BANNER_TEXT = "DRAFT — research prototype, not for circulation"
DRAFT_FULL_TEXT = (
    "DRAFT — research prototype. This is an unreleased working draft produced for "
    "research purposes only. Figures are derived from public data using experimental "
    "methods, contain known data-quality limitations, and have not been reviewed for "
    "publication. Do not cite, circulate, or rely on any figure or claim in this document."
)

CSV, OUT = sys.argv[1], sys.argv[2]
con = duckdb.connect()

print("scanning grants.csv ...", flush=True)
con.execute(f"""
CREATE TEMP TABLE base AS
SELECT
  TRIM(owner_org) AS dept,
  NULLIF(TRIM(owner_org_title),'') AS dept_title,
  TRIM(ref_number) AS refnum,
  COALESCE(TRY_CAST(NULLIF(TRIM(amendment_number),'') AS INTEGER),0) AS amend,
  TRY_CAST(REPLACE(REPLACE(TRIM(agreement_value),',',''),'$','') AS DOUBLE) AS val,
  NULLIF(SUBSTR(TRIM(description_en),1,200),'') AS desc_en,
  NULLIF(SUBSTR(TRIM(description_fr),1,20),'') AS desc_fr,
  NULLIF(TRIM(agreement_type),'') AS atype,
  NULLIF(TRIM(recipient_business_number),'') AS bn,
  NULLIF(TRIM(recipient_province),'') AS prov,
  NULLIF(TRIM(recipient_country),'') AS country,
  SUBSTR(TRIM(recipient_legal_name),1,90) AS recip,
  TRIM(agreement_start_date) AS sd_raw,
  TRIM(agreement_end_date) AS ed_raw,
  NULLIF(TRIM(recipient_postal_code),'') AS postal,
  NULLIF(TRIM(federal_riding_number),'') AS riding,
  COALESCE(
    TRY_STRPTIME(TRIM(agreement_start_date),'%Y-%m-%d'),
    TRY_STRPTIME(TRIM(agreement_start_date),'%Y/%m/%d'),
    TRY_STRPTIME(TRIM(agreement_start_date),'%d/%m/%Y')) AS sd
FROM read_csv('{CSV}', all_varchar=true)
""")
con.execute("""
CREATE TEMP TABLE latest AS
SELECT *,
  (sd = DATE '1899-12-30' OR sd < DATE '1990-01-01' OR sd > DATE '2026-12-31') AS baddate,
  (prov IS NOT NULL AND prov NOT IN
   ('AB','BC','MB','NB','NL','NS','NT','NU','ON','PE','QC','SK','YT')) AS badprov,
  (country IS NOT NULL AND (country != UPPER(country) OR LENGTH(country) != 2)) AS badcountry,
  (atype IS NOT NULL AND atype NOT IN ('C','G','O')) AS dirtytype
FROM base
QUALIFY ROW_NUMBER() OVER (PARTITION BY dept, refnum ORDER BY amend DESC) = 1
""")

# ── Department-level Missing/Messy table (unchanged from the prior version) ─

depts = con.execute("""
SELECT dept, MAX(dept_title), COUNT(*) AS n, SUM(COALESCE(val,0)),
  100.0*SUM(CASE WHEN desc_en IS NULL THEN 1 ELSE 0 END)/COUNT(*),
  100.0*SUM(CASE WHEN desc_fr IS NULL THEN 1 ELSE 0 END)/COUNT(*),
  100.0*SUM(CASE WHEN desc_en IS NOT NULL AND LENGTH(desc_en)<50 THEN 1 ELSE 0 END)/COUNT(*),
  100.0*SUM(CASE WHEN dirtytype THEN 1 ELSE 0 END)/COUNT(*),
  100.0*SUM(CASE WHEN val IS NOT NULL AND val<=0 THEN 1 ELSE 0 END)/COUNT(*),
  100.0*SUM(CASE WHEN baddate THEN 1 ELSE 0 END)/COUNT(*),
  100.0*SUM(CASE WHEN badprov OR badcountry THEN 1 ELSE 0 END)/COUNT(*),
  100.0*SUM(CASE WHEN bn IS NULL THEN 1 ELSE 0 END)/COUNT(*)
FROM latest GROUP BY dept HAVING COUNT(*) >= 100
""").fetchall()

def by_dept(sql, k=3):
    out = {}
    for row in con.execute(sql).fetchall():
        out.setdefault(row[0], []).append(list(row[1:]))
    return {d: v[:k] for d, v in out.items()}

dirty_vals = by_dept("""SELECT dept, atype, COUNT(*) FROM latest WHERE dirtytype
  GROUP BY 1,2 QUALIFY ROW_NUMBER() OVER (PARTITION BY dept ORDER BY COUNT(*) DESC)<=3
  ORDER BY dept, 3 DESC""")
bad_provs = by_dept("""SELECT dept, prov, COUNT(*) FROM latest WHERE badprov
  GROUP BY 1,2 QUALIFY ROW_NUMBER() OVER (PARTITION BY dept ORDER BY COUNT(*) DESC)<=3
  ORDER BY dept, 3 DESC""")
bad_countries = by_dept("""SELECT dept, country, COUNT(*) FROM latest WHERE badcountry
  GROUP BY 1,2 QUALIFY ROW_NUMBER() OVER (PARTITION BY dept ORDER BY COUNT(*) DESC)<=3
  ORDER BY dept, 3 DESC""")
bad_dates = by_dept("""SELECT dept, sd_raw, refnum FROM latest WHERE baddate
  QUALIFY ROW_NUMBER() OVER (PARTITION BY dept ORDER BY sd)<=2 ORDER BY dept""", 2)
nodesc_big = by_dept("""SELECT dept, refnum, recip, val FROM latest
  WHERE desc_en IS NULL AND val IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY dept ORDER BY val DESC)=1 ORDER BY dept""", 1)

PCT_FIELDS = ["nodesc_en", "nodesc_fr", "short", "dirty", "zeroneg", "baddate", "badgeo", "nobn"]

rows = []
for (d, title, n, dollars, p_en, p_fr, p_sh, p_dt, p_zn, p_bd, p_bg, p_bn) in depts:
    pcts = dict(zip(PCT_FIELDS, [p_en, p_fr, p_sh, p_dt, p_zn, p_bd, p_bg, p_bn]))
    det = {
      "dirty": [[v, c] for v, c in dirty_vals.get(d, [])],
      "provs": [[v, c] for v, c in bad_provs.get(d, [])],
      "countries": [[v, c] for v, c in bad_countries.get(d, [])],
      "dates": [[s, r] for s, r in bad_dates.get(d, [])],
      "nodesc": nodesc_big.get(d, []),
    }
    name = (title or d).split("|")[0].strip()[:58]
    rows.append({"code": d, "name": name, "n": n, "dollars": dollars,
      "p": {k: round(v, 1) for k, v in pcts.items()}, "det": det})
rows.sort(key=lambda r: r["name"])

# ── Post-December-2025 mandatory-fields table (new) ─────────────────────────
# December 1, 2025 is when riding number, business number, postal code (among
# other fields) became mandatory for new agreements -- see the methodology
# section's policy citation. This is the freshest, most directly
# TBS-attributable compliance question this dataset can answer: a policy TBS
# itself introduced, months old, still being measured against real data.

DEC1 = "DATE '2025-12-01'"
postdec_n = con.execute(f"SELECT COUNT(*) FROM latest WHERE sd >= {DEC1}").fetchone()[0]
postdec_missing_riding = con.execute(f"SELECT COUNT(*) FROM latest WHERE sd >= {DEC1} AND riding IS NULL").fetchone()[0]
postdec_missing_bn = con.execute(f"SELECT COUNT(*) FROM latest WHERE sd >= {DEC1} AND bn IS NULL").fetchone()[0]
postdec_missing_postal = con.execute(f"SELECT COUNT(*) FROM latest WHERE sd >= {DEC1} AND postal IS NULL").fetchone()[0]

postdec_dept_rows = con.execute(f"""
SELECT COALESCE(MAX(dept_title),dept) AS title, dept, COUNT(*) AS n,
  100.0*SUM(CASE WHEN riding IS NULL THEN 1 ELSE 0 END)/COUNT(*) AS pct_riding,
  100.0*SUM(CASE WHEN bn IS NULL THEN 1 ELSE 0 END)/COUNT(*) AS pct_bn,
  100.0*SUM(CASE WHEN postal IS NULL THEN 1 ELSE 0 END)/COUNT(*) AS pct_postal
FROM latest WHERE sd >= {DEC1}
GROUP BY dept HAVING COUNT(*) >= 10
ORDER BY n DESC
""").fetchall()
postdec_rows = [
    {"code": code, "name": (title or code).split("|")[0].strip()[:58], "n": n,
     "pct_riding": round(pr, 1), "pct_bn": round(pb, 1), "pct_postal": round(pp, 1)}
    for (title, code, n, pr, pb, pp) in postdec_dept_rows
]

# ── Generalized specimen jar (new categories, computed rather than anecdotal) ─

spec_nodesc = con.execute("""SELECT COALESCE(dept_title,dept), refnum, recip, val,
  COALESCE(desc_en,'(no description at all)') FROM latest
  WHERE val > 10000000 AND (desc_en IS NULL OR LENGTH(desc_en) < 15)
  ORDER BY val DESC LIMIT 4""").fetchall()
spec_neg = con.execute("""SELECT COALESCE(dept_title,dept), refnum, recip, val,
  SUBSTR(COALESCE(desc_en,''),1,90) FROM latest WHERE val < 0 ORDER BY val LIMIT 3""").fetchall()
spec_dates = con.execute("""SELECT COALESCE(dept_title,dept), refnum, recip, val, sd_raw
  FROM latest WHERE baddate AND sd_raw IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY sd_raw ORDER BY COALESCE(val,0) DESC)=1
  ORDER BY sd LIMIT 4""").fetchall()
spec_geo = con.execute("""SELECT COALESCE(dept_title,dept), refnum, recip, val,
  'province: '''||prov||'''' FROM latest WHERE badprov
  QUALIFY ROW_NUMBER() OVER (PARTITION BY prov ORDER BY COALESCE(val,0) DESC)=1
  ORDER BY COALESCE(val,0) DESC LIMIT 4""").fetchall()

# Batch/aggregate reporting: a recipient name that is itself a placeholder
# ("batch report│rapport en lots") rather than a real organization -- masks
# recipient-level accountability regardless of dollar amount. Real, and not
# limited to one department: 11 departments do this.
batch_n, batch_total = con.execute("""SELECT COUNT(*), SUM(val) FROM latest
  WHERE recip ILIKE '%batch report%'""").fetchone()
spec_batch = con.execute("""SELECT COALESCE(dept_title,dept), refnum, recip, val, ''
  FROM latest WHERE recip ILIKE '%batch report%' ORDER BY val DESC LIMIT 4""").fetchall()
batch_by_dept = con.execute("""SELECT COALESCE(dept_title,dept), COUNT(*), SUM(val)
  FROM latest WHERE recip ILIKE '%batch report%' GROUP BY 1 ORDER BY 3 DESC""").fetchall()

# Extreme amendment volume: `amend` on the kept (latest) row already equals
# the total amendment count for that ref, since amendment numbers are
# sequential -- no separate aggregation needed.
spec_amend = con.execute("""SELECT COALESCE(dept_title,dept), refnum, recip, val,
  amend::VARCHAR || ' amendments' FROM latest ORDER BY amend DESC LIMIT 4""").fetchall()

# Placeholder end dates: 9999-12-31 is a common database sentinel for "no
# fixed end date" -- distinct from Problem 5's Excel-null *start* dates, and
# not caught by the baddate flag above (which only checks agreement_start_date).
placeholder_n = con.execute("SELECT COUNT(*) FROM latest WHERE ed_raw LIKE '9999%'").fetchone()[0]
spec_placeholder = con.execute("""SELECT COALESCE(dept_title,dept), refnum, recip, val, sd_raw
  FROM latest WHERE ed_raw LIKE '9999%' ORDER BY val DESC LIMIT 4""").fetchall()

# Nominal-dollar awards: positive but symbolic ($0 < value < $10) -- distinct
# from Problem 4's zero/negative values, which are a different failure mode
# (a positive-but-trivial value is deliberate, not a data error).
nominal_n = con.execute("SELECT COUNT(*) FROM latest WHERE val > 0 AND val < 10").fetchone()[0]
spec_nominal = con.execute("""SELECT COALESCE(dept_title,dept), refnum, recip, val,
  SUBSTR(COALESCE(desc_en,''),1,90) FROM latest WHERE val > 0 AND val < 10
  ORDER BY val ASC, refnum LIMIT 4""").fetchall()

# Punctuation-only descriptions: technically satisfies "non-empty", provides
# nothing -- a stricter, generalized version of Problem 7's "boilerplate"
# finding (this catches ";", "-", "n/a"-shaped noise specifically, not just
# short-but-real text).
punct_n = con.execute("""SELECT COUNT(*) FROM latest
  WHERE desc_en IS NOT NULL AND regexp_matches(desc_en, '^[^a-zA-Z0-9]+$')""").fetchone()[0]
spec_punct = con.execute("""SELECT COALESCE(dept_title,dept), refnum, recip, val, desc_en
  FROM latest WHERE desc_en IS NOT NULL AND regexp_matches(desc_en, '^[^a-zA-Z0-9]+$')
  ORDER BY val DESC LIMIT 4""").fetchall()

def spec(rowset):
    return [{"dept": (a or "").split("|")[0].strip(), "ref": b, "recip": c, "val": d, "note": str(e)}
            for a, b, c, d, e in rowset]

# ── Headline totals for the executive summary ───────────────────────────────

n_records, total_value = con.execute("SELECT COUNT(*), SUM(COALESCE(val,0)) FROM latest").fetchone()
nodesc_en_n = con.execute("SELECT COUNT(*) FROM latest WHERE desc_en IS NULL").fetchone()[0]
nobn_n = con.execute("SELECT COUNT(*) FROM latest WHERE bn IS NULL").fetchone()[0]
short_n = con.execute("SELECT COUNT(*) FROM latest WHERE desc_en IS NOT NULL AND LENGTH(desc_en)<50").fetchone()[0]

headline = {
  "n_records": n_records, "total_value": total_value,
  "nodesc_en_n": nodesc_en_n, "nodesc_en_pct": round(100 * nodesc_en_n / n_records, 1),
  "nobn_n": nobn_n, "nobn_pct": round(100 * nobn_n / n_records, 1),
  "short_n": short_n, "short_pct": round(100 * short_n / n_records, 1),
  "postdec_n": postdec_n,
  "postdec_missing_riding_pct": round(100 * postdec_missing_riding / postdec_n, 1) if postdec_n else 0,
  "postdec_missing_bn_pct": round(100 * postdec_missing_bn / postdec_n, 1) if postdec_n else 0,
  "postdec_missing_postal_pct": round(100 * postdec_missing_postal / postdec_n, 1) if postdec_n else 0,
  "batch_n": batch_n, "batch_total": batch_total, "batch_depts": len(batch_by_dept),
  "placeholder_n": placeholder_n, "nominal_n": nominal_n, "punct_n": punct_n,
}

data = {
  "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
  "n_depts": len(rows), "rows": rows,
  "postdec_n_depts": len(postdec_rows), "postdec_rows": postdec_rows,
  "headline": headline,
  "specimens": {
    "nodesc": spec(spec_nodesc), "neg": spec(spec_neg),
    "dates": spec(spec_dates), "geo": spec(spec_geo),
    "batch": spec(spec_batch), "amend": spec(spec_amend),
    "placeholder": spec(spec_placeholder), "nominal": spec(spec_nominal),
    "punct": spec(spec_punct),
  },
}

# ── Recommendations: authored content, not derived from a query -- kept as
# plain data (not raw HTML) so the template's own escaping/rendering stays in
# one place rather than splitting "trusted" vs "untrusted" HTML construction
# across two different code paths. ───────────────────────────────────────────

RECOMMENDATIONS = [
    ("Make business number a hard-blocking mandatory field.",
     "It's already mandatory for post-Dec-2025 agreements in policy, but "
     f"{headline['postdec_missing_bn_pct']}% of those records are still missing it. Business number is what "
     "makes it possible to link a disclosed grant to the recipient's own CRA charity or corporate filings -- "
     "without it, no outside party can verify or cross-reference a recipient's total federal funding. Recommend "
     "the central submission system reject a post-Dec-2025 record outright when this field is blank, rather than "
     "accepting it and relying on policy compliance alone."),
    ("Enforce the same hard block for federal riding number and postal code.",
     f"{headline['postdec_missing_riding_pct']}% of post-Dec-2025 records are still missing riding number, and "
     f"{headline['postdec_missing_postal_pct']}% are missing postal code, months after both became mandatory. A "
     "handful of departments (Employment and Social Development Canada, Prairies Economic Development Canada, "
     "Pacific Economic Development Canada) are already at or near 100% compliance on all three fields, which "
     "suggests this is achievable with the current schema and isn't blocked by a technical limitation -- the gap "
     "looks like enforcement, not capability."),
    ("Validate agreement_type against its controlled list at submission, not after.",
     "Free-text variants (\"Contribution\", \"CONTRIBUTION\", \"Grant\") appear instead of the required C/G/O "
     "codes, concentrated almost entirely in three departments. A dropdown or server-side validation at intake "
     "would prevent this outright rather than requiring downstream data users to normalize it themselves."),
    ("Require amendment_number whenever a value is corrected, and retire offsetting negative rows as a workaround.",
     "The schema already supports amendments for exactly this purpose. One widely-usable example of an apparent "
     "negative-value violation (a -$74M record to the World Health Organization) turned out, on inspection of its "
     "full amendment history, to have been corrected to +$75M in the very next amendment -- the mechanism worked "
     "as intended. The problem is the records where a negative or zero value appears with no amendment trail at "
     "all, making it impossible to tell whether that's the same kind of in-progress correction or a genuine "
     "publishing error."),
    ("Add a minimum-content check for description fields, not just a minimum-length one.",
     f"{headline['punct_n']} records carry a description that is nothing but punctuation (a single semicolon is "
     "the most common form) -- these pass any length-based validation while providing zero accountability value. "
     "A simple check for at least one alphanumeric character would catch this specific pattern without requiring "
     "subjective judgment about description quality."),
    ("Set a policy floor on batch/aggregate reporting.",
     f"{headline['batch_n']:,} records across {headline['batch_depts']} departments name the recipient as a "
     "literal placeholder (\"batch report│rapport en lots\"), collectively representing "
     f"${headline['batch_total']/1e9:.1f}B in disclosed value with no recipient-level detail at all. This may be "
     "an intentional design choice for high-volume benefit programs, but as published it's indistinguishable from "
     "a recipient-identification failure. Recommend departments using this pattern be required to note it "
     "explicitly (so it doesn't read as a data gap) and, where feasible, publish a supplementary recipient-level "
     "breakdown even on a less frequent cadence."),
    ("Replace 9999-12-31 as an end-date sentinel with an explicit \"no fixed end date\" flag.",
     f"{headline['placeholder_n']:,} records use this value, producing agreement durations of thousands of years "
     "in the published data and breaking any duration-based analysis. A boolean field or a genuinely blank end "
     "date would carry the same real-world meaning without corrupting downstream calculations."),
]

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>[DRAFT] Federal Grants &amp; Contributions: Disclosure Quality</title>
<style>
:root{--red:#d52b1e;--ink:#1a1a1a;--mut:#6b6b6b;--bg:#faf8f5;--card:#fff;--line:#e8e4de}
*{box-sizing:border-box;margin:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);line-height:1.5;padding-top:40px}
.draft-banner{position:fixed;top:0;left:0;right:0;z-index:1000;background:#fff3cd;color:#8a6d00;font-weight:700;text-align:center;padding:8px 12px;font-size:.85rem;border-bottom:2px solid #8a6d00}
.draft-watermark{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) rotate(-30deg);font-size:9rem;font-weight:800;color:#000;opacity:.03;pointer-events:none;z-index:0;white-space:nowrap;user-select:none}
.draft-footer-notice{background:#fff3cd;color:#8a6d00;border:1px solid #8a6d00;border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:.8rem}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px 80px}
header{padding:56px 0 8px}
h1{font-size:2.2rem;letter-spacing:-.02em}
.sub{color:var(--mut);margin:6px 0 0;max-width:720px}
h2{font-size:1.1rem;margin:44px 0 14px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);border-bottom:2px solid var(--red);display:inline-block;padding-bottom:4px}
h3{font-size:1rem;margin:20px 0 8px;color:var(--ink)}
p{margin:0 0 12px;max-width:720px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.stat b{display:block;font-size:1.5rem;letter-spacing:-.01em}
.stat span{color:var(--mut);font-size:.78rem}
.reco{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--red);border-radius:6px;padding:14px 18px;margin-bottom:12px}
.reco h3{margin:0 0 6px}
.reco p{margin:0;color:var(--ink);font-size:.92rem}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:.84rem;margin-top:22px}
th{background:#f1ede7;text-align:left;padding:9px 9px;cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--red)}
th.grp{text-align:center;cursor:default;background:#ece6dc;font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:var(--mut)}
th.grp:hover{color:var(--mut)}
td{padding:8px 9px;border-top:1px solid var(--line)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr.main{cursor:pointer}
tr.main:hover td{background:#f7f4ef}
tr.det td{background:#fbf9f6;font-size:.8rem;color:#444;padding:12px 16px 14px}
tr.det .chips span{display:inline-block;background:#fff;border:1px solid var(--line);border-radius:6px;padding:2px 8px;margin:2px 4px 2px 0}
tr.det b{color:var(--ink)}
.hint{color:var(--mut);font-size:.8rem;margin:8px 0 12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.spec{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.spec h3{font-size:.82rem;text-transform:uppercase;letter-spacing:.06em;color:var(--red);margin:0 0 8px}
.spec li{list-style:none;padding:8px 0;border-top:1px solid var(--line);font-size:.82rem}
.spec li:first-of-type{border-top:none}
.spec .amt{font-weight:700}
.spec .meta{color:var(--mut);font-size:.76rem}
.toc{margin:18px 0 0;font-size:.86rem}
.toc a{color:var(--red);text-decoration:none;font-weight:600;margin-right:16px}
.toc a:hover{text-decoration:underline}
footer{margin-top:56px;color:var(--mut);font-size:.78rem;border-top:1px solid var(--line);padding-top:16px}
</style></head><body>
<div class="draft-banner">__DRAFT_BANNER__</div>
<div class="draft-watermark">DRAFT</div>
<div class="wrap">
<header>
<h1>Federal Grants &amp; Contributions: Disclosure Quality</h1>
<p class="sub">Every federal department is required to proactively disclose its grants &amp; contributions
under the Policy on Transfer Payments. This report checks that disclosure against the schema's own
requirements — what's missing outright, what's present but questionable, and how departments are doing
against the mandatory fields that took effect December 1, 2025 — with real records and refs behind every
claim, and concrete recommendations rather than a scorecard.</p>
<p class="toc"><a href="#summary">Summary</a><a href="#recommendations">Recommendations</a><a href="#postdec">Dec 2025 mandatory fields</a><a href="#listing">Full department listing</a><a href="#specimens">Patterns worth flagging</a><a href="#methodology">Methodology &amp; limitations</a></p>
</header>

<h2 id="summary">Summary</h2>
<div class="stats" id="summary-stats"></div>
<p id="summary-text"></p>

<h2 id="recommendations">Recommendations</h2>
<p>Directed at the schema and the centralized publishing system, since that's where each of these is fixable
once rather than department-by-department.</p>
<div id="reco-list">__RECOMMENDATIONS_HTML__</div>

<h2 id="postdec">Post-December 2025 mandatory fields</h2>
<p class="hint">Several fields — recipient business number, postal code, and federal riding number among them —
became mandatory for any agreement with a start date on or after December 1, 2025. This section covers only
that cohort, so it measures compliance with a specific, recent, named policy change rather than the dataset's
history as a whole. Departments below have 10+ post-Dec-2025 records. Click a header to sort.</p>
<table id="postdec-tbl"><thead><tr>
<th data-k="name">Department</th><th class="num" data-k="n">Post-Dec-2025 records</th>
<th class="num" data-k="pct_riding">Missing riding %</th>
<th class="num" data-k="pct_bn">Missing BN %</th>
<th class="num" data-k="pct_postal">Missing postal %</th>
</tr></thead><tbody></tbody></table>

<h2 id="listing">Full department listing</h2>
<p class="hint">All departments with 100+ agreements, across the full dataset (not just post-Dec-2025).
<b>Missing</b> = the field was left blank entirely. <b>Messy</b> = something was entered, but it's off (a
description under 50 characters, or a province/country code that isn't valid). Click a row to expand the
evidence — non-standard agreement types, implausible dates, and zero/negative values are covered there.
Click a header to sort.</p>
<table id="tbl"><thead>
<tr><th></th><th class="num"></th><th class="num"></th>
<th class="num grp" colspan="3">Missing</th><th class="num grp" colspan="2">Messy</th></tr>
<tr>
<th data-k="name">Department</th>
<th class="num" data-k="n">Agreements</th><th class="num" data-k="dollars">Dollars</th>
<th class="num" data-k="nodesc_en">No EN desc %</th><th class="num" data-k="nodesc_fr">No FR desc %</th>
<th class="num" data-k="nobn">No BN %</th>
<th class="num" data-k="short">Short %</th><th class="num" data-k="badgeo">Bad geo %</th>
</tr></thead><tbody></tbody></table>

<h2 id="specimens">Recurring patterns, with records and refs</h2>
<p class="hint">Each category below is a pattern detected across the whole dataset, not a single example — the
count in each heading is the full count; the cards under it show a small sample, not the entire list.</p>
<div class="cards" id="specs"></div>

<h2 id="methodology">Methodology &amp; limitations</h2>
<div class="content" id="methodology-text"></div>

<footer id="foot"></footer>
</div>
<script>
const D = __DATA__;
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
const fmt$ = v => {
  const a = Math.abs(v);
  if (a >= 1e9) return "$" + (v/1e9).toFixed(1) + "B";
  if (a >= 1e6) return "$" + (v/1e6).toFixed(1) + "M";
  return "$" + Math.round(v).toLocaleString();
};
const H = D.headline;

// ── Summary ──────────────────────────────────────────────────────────────
document.getElementById("summary-stats").innerHTML = `
<div class="stat"><b>${H.n_records.toLocaleString()}</b><span>agreements covered</span></div>
<div class="stat"><b>${fmt$(H.total_value)}</b><span>total disclosed value</span></div>
<div class="stat"><b>${H.nodesc_en_pct}%</b><span>missing a required English description</span></div>
<div class="stat"><b>${H.nobn_pct}%</b><span>missing a recipient business number</span></div>
`;
document.getElementById("summary-text").innerHTML =
 `Of ${H.n_records.toLocaleString()} agreements across ${D.n_depts} departments with 100+ agreements each,
  ${H.nodesc_en_n.toLocaleString()} (${H.nodesc_en_pct}%) are missing the mandatory English description, and
  ${H.nobn_n.toLocaleString()} (${H.nobn_pct}%) have no recipient business number — the field that makes it
  possible to link a disclosed grant back to the recipient's own charity or corporate registration. Both
  problems persist into the newest, most tightly-specified part of the schema: of the ${H.postdec_n.toLocaleString()}
  agreements disclosed since the December 1, 2025 mandatory-fields update, ${H.postdec_missing_bn_pct}% are still
  missing business number and ${H.postdec_missing_riding_pct}% are still missing federal riding number, months
  after both became required. A handful of departments are already at or near full compliance on the same
  fields, which is the clearest evidence that the gap elsewhere is enforcement, not feasibility.`;

// ── Recommendations are rendered server-side into #reco-list above — nothing to do here.

// ── Post-Dec-2025 table ─────────────────────────────────────────────────
let pdSortK="n", pdSortAsc=false;
function renderPostdec(){
  const rows=[...D.postdec_rows].sort((a,b)=>{
    const x=a[pdSortK], y=b[pdSortK];
    return (typeof x==="string"? x.localeCompare(y) : x-y)*(pdSortAsc?1:-1);
  });
  document.querySelector("#postdec-tbl tbody").innerHTML = rows.map(r=>
   `<tr><td title="${esc(r.code)}">${esc(r.name)}</td><td class="num">${r.n.toLocaleString()}</td>
    <td class="num">${r.pct_riding}</td><td class="num">${r.pct_bn}</td><td class="num">${r.pct_postal}</td></tr>`
  ).join("");
}
document.querySelectorAll("#postdec-tbl th[data-k]").forEach(th=>th.onclick=()=>{
  const k=th.dataset.k;
  if(k===pdSortK) pdSortAsc=!pdSortAsc; else {pdSortK=k; pdSortAsc=(k==="name");}
  renderPostdec();
});
renderPostdec();

// ── Full department listing ─────────────────────────────────────────────
let sortK="name", sortAsc=true, open=null;
function detail(r){
  const d=r.det, bits=[];
  if(r.p.nodesc_en>0 && d.nodesc.length){const [ref,rec,val]=d.nodesc[0];
    bits.push(`<b>${r.p.nodesc_en}% missing EN descriptions</b> — largest: ${fmt$(val)} to ${esc(rec)||"?"} <span class="meta">(${esc(ref)})</span>`);}
  if(d.dirty.length) bits.push(`<b>Non-standard agreement types:</b> <span class="chips">${
    d.dirty.map(([v,c])=>`<span>'${esc(v)}' ×${c.toLocaleString()}</span>`).join("")}</span> — schema allows only C, G, O`);
  if(d.provs.length) bits.push(`<b>Invalid province codes:</b> <span class="chips">${
    d.provs.map(([v,c])=>`<span>'${esc(v)}' ×${c.toLocaleString()}</span>`).join("")}</span>`);
  if(d.countries.length) bits.push(`<b>Malformed country codes:</b> <span class="chips">${
    d.countries.map(([v,c])=>`<span>'${esc(v)}' ×${c.toLocaleString()}</span>`).join("")}</span>`);
  if(d.dates.length) bits.push(`<b>Implausible dates:</b> ${
    d.dates.map(([s,ref])=>`'${esc(s)}' <span class="meta">(${esc(ref)})</span>`).join(" · ")}`);
  if(r.p.nobn>0) bits.push(`<b>${r.p.nobn}%</b> of records missing a recipient business number`);
  if(r.p.zeroneg>0) bits.push(`<b>${r.p.zeroneg}%</b> zero or negative agreement values`);
  return bits.length? bits.join("<br>") : "No notable issues beyond the percentages shown.";
}
function render(){
  const rows=[...D.rows].sort((a,b)=>{
    const x = a[sortK]??a.p[sortK], y = b[sortK]??b.p[sortK];
    return (typeof x==="string"? x.localeCompare(y) : x-y)*(sortAsc?1:-1);
  });
  document.querySelector("#tbl tbody").innerHTML = rows.map(r=>
   `<tr class="main" data-c="${esc(r.code)}">
    <td title="${esc(r.code)}">${esc(r.name)}</td><td class="num">${r.n.toLocaleString()}</td>
    <td class="num">${fmt$(r.dollars)}</td><td class="num">${r.p.nodesc_en}</td>
    <td class="num">${r.p.nodesc_fr}</td><td class="num">${r.p.nobn}</td>
    <td class="num">${r.p.short}</td><td class="num">${r.p.badgeo}</td></tr>`+
    (open===r.code? `<tr class="det"><td colspan="8">${detail(r)}</td></tr>`:"")
  ).join("");
  document.querySelectorAll("tr.main").forEach(tr=>tr.onclick=()=>{
    open = open===tr.dataset.c? null : tr.dataset.c; render();});
}
document.querySelectorAll("#tbl th[data-k]").forEach(th=>th.onclick=()=>{
  const k=th.dataset.k;
  if(k===sortK) sortAsc=!sortAsc; else {sortK=k; sortAsc=(k==="name");}
  render();
});
render();

// ── Specimen jar ─────────────────────────────────────────────────────────
const S=D.specimens, jar=[
 [`Large agreements with no description`, S.nodesc, s=>`<span class="amt">${fmt$(s.val)}</span> to ${esc(s.recip)||"?"} — description: “${esc(s.note)}”`],
 [`Negative-value agreements`, S.neg, s=>`<span class="amt">${fmt$(s.val)}</span> to ${esc(s.recip)||"?"}${s.note?" — “"+esc(s.note)+"”":""}`],
 [`Implausible agreement dates`, S.dates, s=>`start date <span class="amt">'${esc(s.note)}'</span> — ${fmt$(s.val||0)} to ${esc(s.recip)||"?"}`],
 [`Invalid geography codes`, S.geo, s=>`<span class="amt">${esc(s.note)}</span> — ${fmt$(s.val||0)} to ${esc(s.recip)||"?"}`],
 [`Batch/aggregate reporting (${H.batch_n.toLocaleString()} records, ${fmt$(H.batch_total)} across ${H.batch_depts} departments)`, S.batch, s=>`<span class="amt">${fmt$(s.val)}</span> reported under the recipient name "${esc(s.recip)}", not an identifiable organization`],
 [`High amendment counts`, S.amend, s=>`<span class="amt">${esc(s.note)}</span> — ${esc(s.recip)||"?"}, current value ${fmt$(s.val||0)}`],
 [`Placeholder end dates (${H.placeholder_n.toLocaleString()} records use 9999-12-31)`, S.placeholder, s=>`<span class="amt">${fmt$(s.val||0)}</span> to ${esc(s.recip)||"?"} — end date recorded as 9999-12-31`],
 [`Nominal-dollar awards (${H.nominal_n.toLocaleString()} records under $10)`, S.nominal, s=>`<span class="amt">${fmt$(s.val)}</span> to ${esc(s.recip)||"?"}${s.note?" — “"+esc(s.note)+"”":""}`],
 [`Punctuation-only descriptions (${H.punct_n} records)`, S.punct, s=>`<span class="amt">${fmt$(s.val||0)}</span> to ${esc(s.recip)||"?"} — description: “${esc(s.note)}”`],
];
document.getElementById("specs").innerHTML = jar.filter(([,items])=>items.length).map(([t,items,f])=>
 `<div class="spec"><h3>${t}</h3><ul style="margin:0;padding:0">${
   items.map(s=>`<li>${f(s)}<div class="meta">${esc(s.dept)} · ${esc(s.ref)}</div></li>`).join("")}</ul></div>`).join("");

// ── Methodology ──────────────────────────────────────────────────────────
document.getElementById("methodology-text").innerHTML = `
<p><b>Source and scope.</b> Government of Canada Proactive Disclosure — Grants and Contributions, downloaded
from open.canada.ca. Every department/agreement-number pair is deduplicated to its latest amendment before
anything else is computed, matching the requirement in the <i>Policy on Transfer Payments</i> that amendments
supersede the original record rather than stack alongside it. This isn't a cosmetic choice: one apparent
negative-value violation found in an earlier pass of this analysis (a -$74M record to the World Health
Organization) turned out, once its full amendment history was checked, to have been corrected to +$75M in the
very next amendment — treating that as a live problem would have been wrong. Departments with fewer than 100
agreements total are excluded from the full listing to avoid single-digit-record departments dominating a
percentage column; the post-Dec-2025 table uses a lower 10-record floor since that cohort is much smaller.</p>
<p><b>What's a policy requirement vs. an editorial judgment call.</b> English/French description,
agreement_type's three-code vocabulary, and the December 1, 2025 cohort (business number, postal code, federal
riding number, and several other fields) are actual mandatory-field requirements under the schema TBS
publishes. The "under 50 characters" boilerplate threshold, the "$10" nominal-award cutoff, and the
punctuation-only description pattern are this analysis's own editorial judgment calls for flagging records that
technically satisfy a non-empty requirement while providing little real accountability value — not a claim
about what TBS policy explicitly requires.</p>
<p><b>Known limitations.</b> "Bad geo" combines invalid province codes and invalid/malformed country codes into
one figure; some flagged province values (US state abbreviations, mostly) may reflect a genuine foreign-address
convention rather than department error, and haven't been individually adjudicated. Garbage-date detection only
covers agreement_start_date, not agreement_end_date (covered separately by the placeholder-end-date pattern
above). This report does not attempt to measure whether problems are improving or worsening over time — that
requires comparing published snapshots across multiple points in time, which this single-snapshot analysis
doesn't do.</p>
`;

document.getElementById("foot").innerHTML =
 `<p class="draft-footer-notice">__DRAFT_FULL__</p>
 Generated ${D.generated} from the Government of Canada Proactive Disclosure — Grants and Contributions dataset
 (open.canada.ca). ${D.n_depts} departments with 100+ agreements covered in the full listing;
 ${D.postdec_n_depts} departments with 10+ post-Dec-2025 records covered in that section. One row per agreement
 (latest amendment per department + ref_number). See docs/data-publishing-problems.md in the Canadian Nonprofit
 Data repository for the full problem taxonomy this report draws on.`;
</script></body></html>"""

reco_html = "".join(
    f'<div class="reco"><h3>{i+1}. {issue}</h3><p>{rec}</p></div>'
    for i, (issue, rec) in enumerate(RECOMMENDATIONS)
)

html = (TEMPLATE.replace("__DATA__", json.dumps(data))
        .replace("__DRAFT_BANNER__", DRAFT_BANNER_TEXT)
        .replace("__DRAFT_FULL__", DRAFT_FULL_TEXT)
        .replace("__RECOMMENDATIONS_HTML__", reco_html))
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"wrote {OUT} ({len(html):,} bytes)")
print(f"{len(rows)} departments in full listing, {len(postdec_rows)} in post-Dec-2025 table")
print(f"headline: {json.dumps(headline, default=str)}")
