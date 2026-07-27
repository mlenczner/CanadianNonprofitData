import duckdb, json, sys

CSV = sys.argv[1]
con = duckdb.connect()

print("scanning...", flush=True)
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

out = {}
out["n_records"] = con.execute("SELECT COUNT(*) FROM latest").fetchone()[0]
out["total_value"] = con.execute("SELECT SUM(COALESCE(val,0)) FROM latest").fetchone()[0]
out["n_depts_ge100"] = con.execute("SELECT COUNT(*) FROM (SELECT dept FROM latest GROUP BY dept HAVING COUNT(*)>=100)").fetchone()[0]

out["nodesc_en"] = con.execute("SELECT COUNT(*) FROM latest WHERE desc_en IS NULL").fetchone()[0]
out["nodesc_fr"] = con.execute("SELECT COUNT(*) FROM latest WHERE desc_fr IS NULL").fetchone()[0]
out["dirty_total"] = con.execute("SELECT COUNT(*) FROM latest WHERE dirtytype").fetchone()[0]
out["zeroneg_total"] = con.execute("SELECT COUNT(*) FROM latest WHERE val IS NOT NULL AND val<=0").fetchone()[0]
out["baddate_total"] = con.execute("SELECT COUNT(*) FROM latest WHERE baddate").fetchone()[0]
out["badgeo_total"] = con.execute("SELECT COUNT(*) FROM latest WHERE badprov OR badcountry").fetchone()[0]
out["nobn_total"] = con.execute("SELECT COUNT(*) FROM latest WHERE bn IS NULL").fetchone()[0]
out["short_total"] = con.execute("SELECT COUNT(*) FROM latest WHERE desc_en IS NOT NULL AND LENGTH(desc_en)<50").fetchone()[0]
out["min_val"] = con.execute("SELECT MIN(val) FROM latest").fetchone()[0]

# ---- Dec 2025 mandatory fields ----
DEC1 = "DATE '2025-12-01'"
out["postdec_n"] = con.execute(f"SELECT COUNT(*) FROM latest WHERE sd >= {DEC1}").fetchone()[0]
out["postdec_missing_riding"] = con.execute(f"SELECT COUNT(*) FROM latest WHERE sd >= {DEC1} AND riding IS NULL").fetchone()[0]
out["postdec_missing_bn"] = con.execute(f"SELECT COUNT(*) FROM latest WHERE sd >= {DEC1} AND bn IS NULL").fetchone()[0]
out["postdec_missing_postal"] = con.execute(f"SELECT COUNT(*) FROM latest WHERE sd >= {DEC1} AND postal IS NULL").fetchone()[0]

# by-dept post-dec breakdown (100+ post-dec records only would be too strict; use all with >=20 post-dec records for signal)
rows = con.execute(f"""
SELECT COALESCE(MAX(dept_title),dept) AS name, dept AS code, COUNT(*) AS n,
  100.0*SUM(CASE WHEN riding IS NULL THEN 1 ELSE 0 END)/COUNT(*) AS pct_riding,
  100.0*SUM(CASE WHEN bn IS NULL THEN 1 ELSE 0 END)/COUNT(*) AS pct_bn,
  100.0*SUM(CASE WHEN postal IS NULL THEN 1 ELSE 0 END)/COUNT(*) AS pct_postal
FROM latest WHERE sd >= {DEC1}
GROUP BY dept HAVING COUNT(*) >= 20
ORDER BY n DESC
""").fetchall()
out["postdec_by_dept"] = [
    {"name": (r[0] or "").split("|")[0].strip(), "code": r[1], "n": r[2],
     "pct_riding": round(r[3],1), "pct_bn": round(r[4],1), "pct_postal": round(r[5],1)}
    for r in rows
]

with open("analyze_out.json", "w") as f:
    json.dump(out, f, indent=2, default=str)
print(json.dumps({k:v for k,v in out.items() if k != "postdec_by_dept"}, indent=2, default=str))
print(f"{len(out['postdec_by_dept'])} depts with >=20 post-Dec-2025 records")
