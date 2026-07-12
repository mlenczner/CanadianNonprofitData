"""Build a self-contained HTML data-publishing-quality report from grants.csv.
Ranks departments best-to-worst on publishing quality, with per-department
problem breakdowns and real example records. Latest-amendment dedup per
(owner_org, ref_number), consistent with analysis/build_entity_graph.py. Run:
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

# per-dept detail evidence
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

# global specimen jar
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

WEIGHTS = [("nodesc_en",25),("nodesc_fr",10),("short",15),("dirty",10),
           ("zeroneg",10),("baddate",10),("badgeo",10),("nobn",10)]
def grade(s):
    for g, t in [("A+",99),("A",97),("A-",95),("B+",92),("B",88),("C",80),("D",65)]:
        if s >= t: return g
    return "F"

rows = []
for (d, title, n, dollars, p_en, p_fr, p_sh, p_dt, p_zn, p_bd, p_bg, p_bn) in depts:
    pcts = dict(zip([w[0] for w in WEIGHTS], [p_en,p_fr,p_sh,p_dt,p_zn,p_bd,p_bg,p_bn]))
    score = 100 - sum(w * pcts[k] for k, w in WEIGHTS) / 100
    det = {
      "dirty": [[v, c] for v, c in dirty_vals.get(d, [])],
      "provs": [[v, c] for v, c in bad_provs.get(d, [])],
      "countries": [[v, c] for v, c in bad_countries.get(d, [])],
      "dates": [[s, r] for s, r in bad_dates.get(d, [])],
      "nodesc": nodesc_big.get(d, []),
    }
    name = (title or d).split("|")[0].strip()[:58]
    rows.append({"code": d, "name": name, "n": n, "dollars": dollars,
      "score": round(score,1), "grade": grade(score),
      "p": {k: round(v,1) for k, v in pcts.items()}, "det": det})
rows.sort(key=lambda r: -r["score"])
for i, r in enumerate(rows): r["rank"] = i + 1

def spec(rowset, kind):
    return [{"dept": (a or "").split("|")[0].strip(), "ref": b, "recip": c, "val": d, "note": str(e)}
            for a,b,c,d,e in rowset][:4] if kind else []

data = {
  "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
  "n_depts": len(rows), "rows": rows,
  "best": [r["name"] for r in rows[:3]], "worst": [r["name"] for r in rows[-3:]][::-1],
  "specimens": {
    "nodesc": spec(spec_nodesc, 1), "neg": spec(spec_neg, 1),
    "dates": spec(spec_dates, 1), "geo": spec(spec_geo, 1)},
}

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>[DRAFT] Who Publishes Clean Data? — Federal G&amp;C Publishing Quality</title>
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
h1 .maple{color:var(--red)}
.sub{color:var(--mut);margin:6px 0 0;max-width:680px}
h2{font-size:1.1rem;margin:44px 0 14px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);border-bottom:2px solid var(--red);display:inline-block;padding-bottom:4px}
.podium{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:26px}
.pod{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.pod h3{font-size:.8rem;text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px}
.pod.good h3{color:#1b7a2d}.pod.bad h3{color:var(--red)}
.pod ol{margin:0 0 0 20px;font-size:.92rem}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:.84rem}
th{background:#f1ede7;text-align:left;padding:9px 9px;cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--red)}
td{padding:8px 9px;border-top:1px solid var(--line)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr.main{cursor:pointer}
tr.main:hover td{background:#f7f4ef}
tr.det td{background:#fbf9f6;font-size:.8rem;color:#444;padding:12px 16px 14px}
tr.det .chips span{display:inline-block;background:#fff;border:1px solid var(--line);border-radius:6px;padding:2px 8px;margin:2px 4px 2px 0}
tr.det b{color:var(--ink)}
.g{display:inline-block;min-width:34px;text-align:center;padding:2px 7px;border-radius:6px;font-weight:700;font-size:.8rem}
.gA{background:#e2f2e4;color:#1b7a2d}.gB{background:#fdf3d7;color:#8a6d00}
.gC{background:#fde4cf;color:#a35200}.gD{background:#fcd9d5;color:#b3261e}.gF{background:var(--red);color:#fff}
.hint{color:var(--mut);font-size:.8rem;margin:8px 0 12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.spec{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.spec h3{font-size:.82rem;text-transform:uppercase;letter-spacing:.06em;color:var(--red);margin-bottom:8px}
.spec li{list-style:none;padding:8px 0;border-top:1px solid var(--line);font-size:.82rem}
.spec li:first-of-type{border-top:none}
.spec .amt{font-weight:700}
.spec .meta{color:var(--mut);font-size:.76rem}
footer{margin-top:56px;color:var(--mut);font-size:.78rem;border-top:1px solid var(--line);padding-top:16px}
</style></head><body>
<div class="draft-banner">__DRAFT_BANNER__</div>
<div class="draft-watermark">DRAFT</div>
<div class="wrap">
<header>
<h1>Who Publishes Clean Data? <span class="maple">&#127809;</span></h1>
<p class="sub">Every federal department is required to proactively disclose its grants &amp; contributions.
This report grades how well each one actually fills in the form — descriptions, dates, geography,
business numbers — ranked best to worst. Click any department for its specific offences, with real record refs.</p>
</header>
<div class="podium">
<div class="pod good"><h3>Cleanest publishers</h3><ol id="best"></ol></div>
<div class="pod bad"><h3>Worst culprits</h3><ol id="worst"></ol></div>
</div>
<h2>Full ranking</h2>
<p class="hint">Departments with 100+ agreements. Score = 100 &minus; weighted problem rates:
missing EN description 25 &middot; missing FR description 10 &middot; description under 50 chars 15 &middot;
non-standard agreement type 10 &middot; zero/negative value 10 &middot; implausible start date 10 &middot;
invalid province/country 10 &middot; missing business number 10. Click a row to expand the evidence; click a header to sort.</p>
<table id="tbl"><thead><tr>
<th class="num" data-k="rank">#</th><th data-k="name">Department</th>
<th class="num" data-k="n">Agreements</th><th class="num" data-k="dollars">Dollars</th>
<th class="num" data-k="nodesc_en">No EN desc %</th><th class="num" data-k="nodesc_fr">No FR desc %</th>
<th class="num" data-k="short">Short %</th><th class="num" data-k="nobn">No BN %</th>
<th class="num" data-k="badgeo">Bad geo %</th><th class="num" data-k="score">Score</th>
<th data-k="grade">Grade</th></tr></thead><tbody></tbody></table>
<h2>Specimen jar — real records, real refs</h2>
<div class="cards" id="specs"></div>
<footer id="foot"></footer>
</div>
<script>
const D = __DATA__;
const fmt$ = v => {
  const a = Math.abs(v);
  if (a >= 1e9) return "$" + (v/1e9).toFixed(1) + "B";
  if (a >= 1e6) return "$" + (v/1e6).toFixed(1) + "M";
  return "$" + Math.round(v).toLocaleString();
};
document.getElementById("best").innerHTML = D.best.map(n=>`<li>${n}</li>`).join("");
document.getElementById("worst").innerHTML = D.worst.map(n=>`<li>${n}</li>`).join("");
let sortK="rank", sortAsc=true, open=null;
function detail(r){
  const d=r.det, bits=[];
  if(r.p.nodesc_en>0 && d.nodesc.length){const [ref,rec,val]=d.nodesc[0];
    bits.push(`<b>${r.p.nodesc_en}% missing EN descriptions</b> — largest: ${fmt$(val)} to ${rec||"?"} <span class="meta">(${ref})</span>`);}
  if(d.dirty.length) bits.push(`<b>Non-standard agreement types:</b> <span class="chips">${
    d.dirty.map(([v,c])=>`<span>'${v}' ×${c.toLocaleString()}</span>`).join("")}</span> — schema allows only C, G, O`);
  if(d.provs.length) bits.push(`<b>Invalid province codes:</b> <span class="chips">${
    d.provs.map(([v,c])=>`<span>'${v}' ×${c.toLocaleString()}</span>`).join("")}</span>`);
  if(d.countries.length) bits.push(`<b>Malformed country codes:</b> <span class="chips">${
    d.countries.map(([v,c])=>`<span>'${v}' ×${c.toLocaleString()}</span>`).join("")}</span>`);
  if(d.dates.length) bits.push(`<b>Implausible dates:</b> ${
    d.dates.map(([s,ref])=>`'${s}' <span class="meta">(${ref})</span>`).join(" · ")}`);
  if(r.p.nobn>0) bits.push(`<b>${r.p.nobn}%</b> of records missing a recipient business number`);
  if(r.p.zeroneg>0) bits.push(`<b>${r.p.zeroneg}%</b> zero or negative agreement values`);
  return bits.length? bits.join("<br>") : "No notable offences beyond the percentages shown. Respect.";
}
function render(){
  const rows=[...D.rows].sort((a,b)=>{
    const x = a[sortK]??a.p[sortK], y = b[sortK]??b.p[sortK];
    return (typeof x==="string"? x.localeCompare(y) : x-y)*(sortAsc?1:-1);
  });
  document.querySelector("#tbl tbody").innerHTML = rows.map(r=>
   `<tr class="main" data-c="${r.code}"><td class="num">${r.rank}</td>
    <td title="${r.code}">${r.name}</td><td class="num">${r.n.toLocaleString()}</td>
    <td class="num">${fmt$(r.dollars)}</td><td class="num">${r.p.nodesc_en}</td>
    <td class="num">${r.p.nodesc_fr}</td><td class="num">${r.p.short}</td>
    <td class="num">${r.p.nobn}</td><td class="num">${r.p.badgeo}</td>
    <td class="num">${r.score}</td><td><span class="g g${r.grade[0]}">${r.grade}</span></td></tr>`+
    (open===r.code? `<tr class="det"><td colspan="11">${detail(r)}</td></tr>`:"")
  ).join("");
  document.querySelectorAll("tr.main").forEach(tr=>tr.onclick=()=>{
    open = open===tr.dataset.c? null : tr.dataset.c; render();});
}
document.querySelectorAll("#tbl th").forEach(th=>th.onclick=e=>{
  const k=th.dataset.k;
  if(k===sortK) sortAsc=!sortAsc; else {sortK=k; sortAsc=(k==="name"||k==="grade"||k==="rank");}
  render();
});
render();
const S=D.specimens, jar=[
 ["Eight figures, no explanation", S.nodesc, s=>`<span class="amt">${fmt$(s.val)}</span> to ${s.recip||"?"} — description: “${s.note}”`],
 ["Negative money", S.neg, s=>`<span class="amt">${fmt$(s.val)}</span> to ${s.recip||"?"}${s.note?" — “"+s.note+"”":""}`],
 ["Dates from another timeline", S.dates, s=>`start date <span class="amt">'${s.note}'</span> — ${fmt$(s.val||0)} to ${s.recip||"?"}`],
 ["Geography, freestyle", S.geo, s=>`<span class="amt">${s.note}</span> — ${fmt$(s.val||0)} to ${s.recip||"?"}`],
];
document.getElementById("specs").innerHTML = jar.map(([t,items,f])=>
 `<div class="spec"><h3>${t}</h3><ul style="margin:0;padding:0">${
   items.map(s=>`<li>${f(s)}<div class="meta">${s.dept} · ${s.ref}</div></li>`).join("")}</ul></div>`).join("");
document.getElementById("foot").innerHTML =
 `<p class="draft-footer-notice">__DRAFT_FULL__</p>
 Generated ${D.generated} from the Government of Canada Proactive Disclosure — Grants and Contributions dataset
 (open.canada.ca). ${D.n_depts} departments with 100+ agreements ranked. One row per agreement
 (latest amendment per department + ref_number). “Implausible date” = before 1990, after 2026, or the
 1899-12-30 Excel null. Weights are editorial judgment, not TBS policy — see docs/data-publishing-problems.md
 in the Canadian Nonprofit Data repository for the full problem taxonomy.`;
</script></body></html>"""

html = (TEMPLATE.replace("__DATA__", json.dumps(data))
        .replace("__DRAFT_BANNER__", DRAFT_BANNER_TEXT)
        .replace("__DRAFT_FULL__", DRAFT_FULL_TEXT))
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"wrote {OUT} ({len(html):,} bytes)")
print("best:", data["best"])
print("worst:", data["worst"])
