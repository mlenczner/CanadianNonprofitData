"""Build a self-contained HTML dashboard from grants.csv.
All dollar figures use latest-amendment-per-(owner_org, ref_number) dedup —
the same corrected logic as analysis/build_entity_graph.py. Run:
python3 build_dashboard.py /path/to/grants.csv /path/to/output.html
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
  NULLIF(SUBSTR(TRIM(description_en),1,300),'') AS desc_en,
  NULLIF(TRIM(agreement_type),'') AS atype,
  SUBSTR(TRIM(recipient_legal_name),1,120) AS recip,
  COALESCE(
    TRY_STRPTIME(TRIM(agreement_start_date),'%Y-%m-%d'),
    TRY_STRPTIME(TRIM(agreement_start_date),'%Y/%m/%d'),
    TRY_STRPTIME(TRIM(agreement_start_date),'%d/%m/%Y')) AS sd,
  COALESCE(
    TRY_STRPTIME(TRIM(agreement_end_date),'%Y-%m-%d'),
    TRY_STRPTIME(TRIM(agreement_end_date),'%Y/%m/%d'),
    TRY_STRPTIME(TRIM(agreement_end_date),'%d/%m/%Y')) AS ed
FROM read_csv('{CSV}', all_varchar=true)
""")
con.execute("""
CREATE TEMP TABLE latest AS
SELECT * FROM base
QUALIFY ROW_NUMBER() OVER (PARTITION BY dept, refnum ORDER BY amend DESC) = 1
""")

raw_n = con.execute("SELECT COUNT(*) FROM base").fetchone()[0]
hero = con.execute("""
SELECT COUNT(*), SUM(val), COUNT(DISTINCT dept),
  MIN(CASE WHEN sd >= DATE '2000-01-01' THEN sd END),
  MAX(CASE WHEN sd <= DATE '2027-12-31' THEN sd END)
FROM latest""").fetchone()

years = con.execute("""
SELECT CASE WHEN MONTH(sd)>=4 THEN YEAR(sd) ELSE YEAR(sd)-1 END AS fy,
       COUNT(*), SUM(COALESCE(val,0))
FROM latest WHERE sd IS NOT NULL
  AND sd BETWEEN DATE '2005-04-01' AND DATE '2027-03-31'
GROUP BY 1 ORDER BY 1""").fetchall()

depts = con.execute("""
SELECT dept, MAX(dept_title) AS title, COUNT(*) AS n, SUM(COALESCE(val,0)) AS dollars,
  100.0*SUM(CASE WHEN desc_en IS NULL THEN 1 ELSE 0 END)/COUNT(*) AS pct_nodesc,
  100.0*SUM(CASE WHEN desc_en IS NOT NULL AND LENGTH(desc_en)<50 THEN 1 ELSE 0 END)/COUNT(*) AS pct_short,
  100.0*SUM(CASE WHEN atype IS NOT NULL AND atype NOT IN ('C','G','O') THEN 1 ELSE 0 END)/COUNT(*) AS pct_dirty,
  100.0*SUM(CASE WHEN val IS NOT NULL AND val<=0 THEN 1 ELSE 0 END)/COUNT(*) AS pct_zeroneg
FROM latest GROUP BY dept HAVING COUNT(*) >= 100 ORDER BY dollars DESC""").fetchall()

facts = {}
facts["one_dollar"] = con.execute("SELECT COUNT(*) FROM latest WHERE val=1.0").fetchone()[0]
facts["tiny"] = con.execute("SELECT COUNT(*) FROM latest WHERE val>0 AND val<100").fetchone()[0]
neg = con.execute("SELECT COUNT(*), COALESCE(SUM(val),0) FROM latest WHERE val<0").fetchone()
facts["neg_n"], facts["neg_sum"] = neg[0], neg[1]
facts["excel"] = con.execute("SELECT COUNT(*) FROM latest WHERE sd=DATE '1899-12-30'").fetchone()[0]
facts["farfuture"] = con.execute("SELECT COUNT(*) FROM latest WHERE ed>=DATE '2040-01-01'").fetchone()[0]
ma = con.execute("""SELECT refnum, MAX(amend), MAX(recip) FROM base
GROUP BY refnum ORDER BY 2 DESC LIMIT 1""").fetchone()
facts["most_amended"] = {"ref": ma[0], "n": ma[1], "recip": ma[2]}
facts["superseded"] = raw_n - hero[0]

biggest = con.execute("""
SELECT recip, COALESCE(dept_title,dept), val,
  CASE WHEN sd IS NOT NULL THEN YEAR(sd) END, SUBSTR(COALESCE(desc_en,''),1,140)
FROM latest WHERE val IS NOT NULL ORDER BY val DESC LIMIT 5""").fetchall()

dept_rows = []
for d, title, n, dollars, p1, p2, p3, p4 in depts:
    name = (title or d).split("|")[0].strip()[:58]
    dept_rows.append({"code": d, "name": name, "n": n, "dollars": dollars,
        "nodesc": round(p1,1), "short": round(p2,1), "dirty": round(p3,1),
        "zeroneg": round(p4,1)})

data = {
  "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
  "hero": {"agreements": hero[0], "dollars": hero[1], "depts": hero[2],
           "from": str(hero[3])[:10], "to": str(hero[4])[:10], "superseded": facts["superseded"]},
  "years": [[int(y), int(c), float(v)] for y, c, v in years],
  "depts": dept_rows,
  "facts": facts,
  "biggest": [{"recip": (r or "").split("|")[0].strip(), "dept": (dt or "").split("|")[0].strip(),
               "val": v, "year": y, "desc": ds}
              for r, dt, v, y, ds in biggest],
}

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>[DRAFT] Federal Grants &amp; Contributions: Overview</title>
<style>
:root{--red:#d52b1e;--ink:#1a1a1a;--mut:#6b6b6b;--bg:#faf8f5;--card:#fff;--line:#e8e4de}
*{box-sizing:border-box;margin:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink);line-height:1.5;padding-top:40px}
.draft-banner{position:fixed;top:0;left:0;right:0;z-index:1000;background:#fff3cd;color:#8a6d00;font-weight:700;text-align:center;padding:8px 12px;font-size:.85rem;border-bottom:2px solid #8a6d00}
.draft-watermark{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) rotate(-30deg);font-size:9rem;font-weight:800;color:#000;opacity:.03;pointer-events:none;z-index:0;white-space:nowrap;user-select:none}
.draft-footer-notice{background:#fff3cd;color:#8a6d00;border:1px solid #8a6d00;border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:.8rem}
.wrap{max-width:1060px;margin:0 auto;padding:0 24px 80px}
header{padding:56px 0 8px}
h1{font-size:2.4rem;letter-spacing:-.02em}
.sub{color:var(--mut);margin:6px 0 0;max-width:640px}
h2{font-size:1.15rem;margin:48px 0 16px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);border-bottom:2px solid var(--red);display:inline-block;padding-bottom:4px}
.heroes{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-top:28px}
.hero{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px}
.hero b{display:block;font-size:1.7rem;letter-spacing:-.02em}
.hero span{color:var(--mut);font-size:.82rem}
.bars{display:flex;align-items:flex-end;gap:6px;height:190px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 20px 34px;position:relative}
.bar{flex:1;background:var(--red);opacity:.85;border-radius:3px 3px 0 0;position:relative;min-height:2px;transition:opacity .15s}
.bar:hover{opacity:1}
.bar i{position:absolute;bottom:-24px;left:50%;transform:translateX(-50%);font-style:normal;font-size:.62rem;color:var(--mut);white-space:nowrap}
.bar u{display:none;position:absolute;bottom:calc(100% + 6px);left:50%;transform:translateX(-50%);background:var(--ink);color:#fff;font-size:.7rem;padding:3px 8px;border-radius:5px;text-decoration:none;white-space:nowrap;z-index:3}
.bar:hover u{display:block}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:.85rem}
th{background:#f1ede7;text-align:left;padding:9px 10px;cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--red)}
td{padding:8px 10px;border-top:1px solid var(--line)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.card b{font-size:1.35rem;display:block}
.card span{color:var(--mut);font-size:.83rem}
.big{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:4px 18px;margin-bottom:0}
.big li{list-style:none;padding:13px 0;border-bottom:1px solid var(--line)}
.big li:last-child{border:none}
.big .amt{color:var(--red);font-weight:700}
.big .who{font-weight:600}
.big .meta{color:var(--mut);font-size:.82rem}
footer{margin-top:56px;color:var(--mut);font-size:.78rem;border-top:1px solid var(--line);padding-top:16px}
@media(max-width:640px){.bar i{display:none}}
</style></head><body>
<div class="draft-banner">__DRAFT_BANNER__</div>
<div class="draft-watermark">DRAFT</div>
<div class="wrap">
<header>
<h1>Federal Grants &amp; Contributions: Overview</h1>
<p class="sub">A statistical overview of Canada&rsquo;s federal Grants &amp; Contributions disclosure data.
Amendment-deduplicated: one row per agreement, latest state only.</p>
</header>
<div class="heroes" id="heroes"></div>
<h2>Funding by fiscal year</h2>
<div class="bars" id="bars"></div>
<h2>Disclosure completeness by department</h2>
<p class="sub" style="margin-bottom:12px">Departments and agencies with 100+ agreements, by share of records with
missing descriptions, sub-50-character descriptions, non-standard agreement types, and zero/negative values.
Click a column to sort.</p>
<table id="tbl"><thead><tr>
<th data-k="name">Department</th><th class="num" data-k="n">Agreements</th>
<th class="num" data-k="dollars">Dollars</th><th class="num" data-k="nodesc">No desc %</th>
<th class="num" data-k="short">Short desc %</th><th class="num" data-k="dirty">Dirty type %</th>
<th class="num" data-k="zeroneg">Zero/neg %</th></tr></thead><tbody></tbody></table>
<h2>Largest single agreements</h2>
<ul class="big" id="big"></ul>
<h2>Additional patterns</h2>
<div class="cards" id="cards"></div>
<footer id="foot"></footer>
</div>
<script>
const D = __DATA__;
const fmt$ = v => {
  const a = Math.abs(v);
  if (a >= 1e9) return "$" + (v/1e9).toFixed(a>=1e10?0:1) + "B";
  if (a >= 1e6) return "$" + (v/1e6).toFixed(1) + "M";
  return "$" + Math.round(v).toLocaleString();
};
const fmtN = v => v.toLocaleString();
// heroes
const H = D.hero;
document.getElementById("heroes").innerHTML = [
  [fmt$(H.dollars), "total committed (latest amendment per agreement)"],
  [fmtN(H.agreements), "distinct agreements"],
  [H.depts, "departments & agencies"],
  [fmtN(H.superseded), "superseded amendment rows excluded"],
].map(([b,s])=>`<div class="hero"><b>${b}</b><span>${s}</span></div>`).join("");
// year bars
const ys = D.years.filter(y=>y[0]>=2008);
const mx = Math.max(...ys.map(y=>y[2]));
document.getElementById("bars").innerHTML = ys.map(([fy,n,v])=>
  `<div class="bar" style="height:${Math.max(2,100*v/mx)}%">
   <u>FY${fy}&ndash;${(fy+1)%100}: ${fmt$(v)} &middot; ${fmtN(n)} agreements</u>
   <i>${String(fy).slice(2)}/${String(fy+1).slice(2)}</i></div>`).join("");
// dept table
let sortK = "dollars", sortAsc = false;
function renderTbl(){
  const rows=[...D.depts].sort((a,b)=>{
    const x=a[sortK],y=b[sortK];
    return (typeof x==="string" ? x.localeCompare(y) : x-y) * (sortAsc?1:-1);
  });
  document.querySelector("#tbl tbody").innerHTML = rows.map(d=>
   `<tr><td title="${d.code}">${d.name}</td><td class="num">${fmtN(d.n)}</td>
    <td class="num">${fmt$(d.dollars)}</td><td class="num">${d.nodesc}</td>
    <td class="num">${d.short}</td><td class="num">${d.dirty}</td>
    <td class="num">${d.zeroneg}</td></tr>`).join("");
}
document.querySelectorAll("#tbl th").forEach(th=>th.onclick=()=>{
  const k=th.dataset.k;
  if(k===sortK) sortAsc=!sortAsc; else {sortK=k; sortAsc=(k==="name");}
  renderTbl();
});
renderTbl();
// biggest
document.getElementById("big").innerHTML = D.biggest.map(b=>
 `<li><span class="amt">${fmt$(b.val)}</span> &mdash; <span class="who">${b.recip||"(unnamed)"}</span>
  <div class="meta">${b.dept}${b.year?" &middot; "+b.year:""}${b.desc?" &middot; "+b.desc+"&hellip;":""}</div></li>`).join("");
// additional patterns
const F = D.facts;
document.getElementById("cards").innerHTML = [
  [fmtN(F.one_dollar), "agreements worth exactly $1"],
  [fmtN(F.tiny), "agreements under $100"],
  [`${fmtN(F.neg_n)} / ${fmt$(F.neg_sum)}`, "negative-value records (schema says value must be > 0)"],
  [fmtN(F.excel), "agreements dated 1899-12-30 — the Excel null date"],
  [fmtN(F.farfuture), "agreements scheduled to end in 2040 or later"],
  [F.most_amended.n + "×", `most-amended agreement (${F.most_amended.recip||F.most_amended.ref})`],
].map(([b,s])=>`<div class="card"><b>${b}</b><span>${s}</span></div>`).join("");
document.getElementById("foot").innerHTML =
 `<p class="draft-footer-notice">__DRAFT_FULL__</p>
 Generated ${D.generated} from the Government of Canada Proactive Disclosure &mdash; Grants and Contributions dataset
 (open.canada.ca). Coverage ${H.from} to ${H.to}. Dollar figures keep only the latest amendment per
 (department, ref_number); amendment rows restate agreement values, so summing all rows would overcount.
 Some departments publish amendment deltas instead of restated totals, so totals are best-available approximations.
 Methodology &amp; caveats: see docs/ in the Canadian Nonprofit Data repository.`;
</script></body></html>"""

html = (TEMPLATE.replace("__DATA__", json.dumps(data))
        .replace("__DRAFT_BANNER__", DRAFT_BANNER_TEXT)
        .replace("__DRAFT_FULL__", DRAFT_FULL_TEXT))
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"wrote {OUT} ({len(html):,} bytes)")
print(json.dumps({k: v for k, v in data.items() if k in ("hero", "facts")}, indent=1)[:900])
