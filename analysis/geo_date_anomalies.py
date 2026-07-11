"""
GC Grants & Contributions — Date and Geography Anomaly Analysis
Identifies departments responsible for garbage dates and province/country contamination.
Run with: python3 analysis/geo_date_anomalies.py grants.csv

Reads grants.csv via DuckDB instead of streaming it row-by-row through
Python's csv module. Bulk aggregation (dept totals, excel-null-date counts,
lowercase-country counts, bad-province counts) happens in SQL; the anomalous
past/future-date rows and the foreign-vs-invalid province classification are
fetched as raw rows and assembled in Python using the exact same logic as
the original (these sets are small -- anomalies, not the bulk of the data --
so there's no performance cost to keeping that part identical/verified-safe).

Verified byte-for-byte identical against the original except one class of
difference: when a Counter's most_common(N) truncates a group of values tied
at the same count, WHICH members of that tied group appear is a tie-break
that depended on original CSV row order in the row-by-row version (arbitrary,
coincidental) -- every SQL query here has an explicit ORDER BY so the same
tie-break is now alphabetical instead (deterministic, reproducible across
runs, unlike depending on DuckDB's otherwise-unspecified GROUP BY order).
All non-tied values, and every total/count, are identical.
"""

import sys
from collections import defaultdict, Counter
from datetime import datetime

import duckdb

FILE = sys.argv[1] if len(sys.argv) > 1 else "grants.csv"

VALID_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"
}
MIN_VALID_DATE = datetime(1990, 1, 1)
MAX_VALID_DATE = datetime(2026, 12, 31)
EXCEL_NULL_DATE = datetime(1899, 12, 30)

dept_names = {}
dept_total = defaultdict(int)
dept_date_past = defaultdict(list)
dept_date_future = defaultdict(list)
dept_excel_null = defaultdict(int)
dept_lowercase_country = defaultdict(int)
dept_invalid_province = defaultdict(Counter)
dept_foreign_in_province = defaultdict(Counter)
all_bad_provinces = Counter()
all_bad_countries = Counter()

print(f"Reading {FILE} via DuckDB...", flush=True)

con = duckdb.connect()
csv_src = f"read_csv('{FILE}', all_varchar=true)"
date_parse = """COALESCE(
    TRY_STRPTIME(TRIM(agreement_start_date), '%Y-%m-%d'),
    TRY_STRPTIME(TRIM(agreement_start_date), '%Y/%m/%d'),
    TRY_STRPTIME(TRIM(agreement_start_date), '%d/%m/%Y')
)"""

# ── dept totals + names ──────────────────────────────────────────────────────
for dept, title, total_n in con.execute(f"""
    SELECT TRIM(owner_org) AS dept, MAX(NULLIF(TRIM(owner_org_title), '')) AS title, COUNT(*) AS n
    FROM {csv_src} GROUP BY 1 ORDER BY 1
""").fetchall():
    dept_total[dept] = total_n
    if dept and title:
        dept_names[dept] = title
total = sum(dept_total.values())

# ── excel-null-date counts ───────────────────────────────────────────────────
for dept, n in con.execute(f"""
    WITH parsed AS (SELECT TRIM(owner_org) AS dept, {date_parse} AS d FROM {csv_src})
    SELECT dept, COUNT(*) FROM parsed WHERE d = DATE '1899-12-30' GROUP BY 1 ORDER BY 1
""").fetchall():
    dept_excel_null[dept] = n

# ── past/future anomalous date rows (raw, assembled in Python like the original) ──
for dept, date_str, ref in con.execute(f"""
    WITH parsed AS (
        SELECT TRIM(owner_org) AS dept, TRIM(agreement_start_date) AS date_str,
               TRIM(ref_number) AS ref, {date_parse} AS d
        FROM {csv_src}
    )
    SELECT dept, date_str, ref FROM parsed
    WHERE d IS NOT NULL AND d < DATE '1990-01-01' AND d != DATE '1899-12-30'
    ORDER BY dept, date_str, ref
""").fetchall():
    dept_date_past[dept].append((date_str, ref))

for dept, date_str, ref in con.execute(f"""
    WITH parsed AS (
        SELECT TRIM(owner_org) AS dept, TRIM(agreement_start_date) AS date_str,
               TRIM(ref_number) AS ref, {date_parse} AS d
        FROM {csv_src}
    )
    SELECT dept, date_str, ref FROM parsed WHERE d IS NOT NULL AND d > DATE '2026-12-31'
    ORDER BY dept, date_str, ref
""").fetchall():
    dept_date_future[dept].append((date_str, ref))

# ── lowercase country codes ──────────────────────────────────────────────────
for dept, country, n in con.execute(f"""
    WITH parsed AS (SELECT TRIM(owner_org) AS dept, TRIM(recipient_country) AS country FROM {csv_src})
    SELECT dept, country, COUNT(*) FROM parsed
    WHERE country != '' AND country != UPPER(country) GROUP BY 1, 2 ORDER BY 1, 2
""").fetchall():
    dept_lowercase_country[dept] += n
    all_bad_countries[country] += n

# ── bad province values, classified foreign vs invalid (same logic as original) ──
for dept, province, n in con.execute(f"""
    WITH parsed AS (SELECT TRIM(owner_org) AS dept, TRIM(recipient_province) AS province FROM {csv_src})
    SELECT dept, province, COUNT(*) FROM parsed
    WHERE province != '' AND province NOT IN ({",".join(repr(p) for p in VALID_PROVINCES)})
    GROUP BY 1, 2 ORDER BY 1, 2
""").fetchall():
    all_bad_provinces[province] += n
    if len(province) == 2 and province.upper() == province:
        dept_foreign_in_province[dept][province] += n
    else:
        dept_invalid_province[dept][province] += n

print(f"\nTotal rows processed: {total:,}\n")

# ── OUTPUT (unchanged from the original) ─────────────────────────────────────
timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

lines = []
lines.append(f"# GC Grants — Date and Geography Anomaly Report")
lines.append(f"Generated: {timestamp}  |  Total records: {total:,}\n")

lines.append("## 1. Garbage Dates\n")

excel_depts = [(d, c) for d, c in dept_excel_null.items() if c > 0]
excel_depts.sort(key=lambda x: x[1], reverse=True)
total_excel = sum(dept_excel_null.values())

lines.append(f"### 1a. Excel null date (1899-12-30) — {total_excel:,} total records\n")
if excel_depts:
    lines.append("| Dept | Department | Count |")
    lines.append("|---|---|---|")
    for dept, count in excel_depts:
        name = dept_names.get(dept, dept)
        lines.append(f"| {dept} | {name} | {count:,} |")
else:
    lines.append("None found.")

past_depts = [(d, v) for d, v in dept_date_past.items() if v]
past_depts.sort(key=lambda x: len(x[1]), reverse=True)
total_past = sum(len(v) for _, v in past_depts)

lines.append(f"\n### 1b. Implausibly old dates (before 1990, excluding 1899-12-30) — {total_past:,} total records\n")
if past_depts:
    lines.append("| Dept | Department | Count | Example dates |")
    lines.append("|---|---|---|---|")
    for dept, records in past_depts[:20]:
        name = dept_names.get(dept, dept)
        examples = ", ".join(sorted(set(r[0] for r in records))[:5])
        lines.append(f"| {dept} | {name} | {len(records):,} | {examples} |")
else:
    lines.append("None found.")

future_depts = [(d, v) for d, v in dept_date_future.items() if v]
future_depts.sort(key=lambda x: len(x[1]), reverse=True)
total_future = sum(len(v) for _, v in future_depts)

lines.append(f"\n### 1c. Future dates (after {MAX_VALID_DATE.year}) — {total_future:,} total records\n")
if future_depts:
    lines.append("| Dept | Department | Count | Example dates |")
    lines.append("|---|---|---|---|")
    for dept, records in future_depts[:20]:
        name = dept_names.get(dept, dept)
        examples = ", ".join(sorted(set(r[0] for r in records))[:5])
        lines.append(f"| {dept} | {name} | {len(records):,} | {examples} |")
else:
    lines.append("None found.")

lines.append(f"\n## 2. Geographic Field Contamination\n")

lower_depts = [(d, c) for d, c in dept_lowercase_country.items() if c > 0]
lower_depts.sort(key=lambda x: x[1], reverse=True)
total_lower = sum(dept_lowercase_country.values())

lines.append(f"### 2a. Lowercase country codes — {total_lower:,} total records\n")
lines.append(f"All bad country values seen: {dict(all_bad_countries.most_common(20))}\n")
if lower_depts:
    lines.append("| Dept | Department | Count |")
    lines.append("|---|---|---|")
    for dept, count in lower_depts[:20]:
        name = dept_names.get(dept, dept)
        lines.append(f"| {dept} | {name} | {count:,} |")
else:
    lines.append("None found.")

foreign_prov_depts = [(d, c) for d, c in dept_foreign_in_province.items() if c]
foreign_prov_depts.sort(key=lambda x: sum(x[1].values()), reverse=True)
total_foreign_prov = sum(sum(c.values()) for _, c in foreign_prov_depts)

lines.append(f"\n### 2b. Foreign/invalid codes in recipient_province field — {total_foreign_prov:,} total records\n")
lines.append(f"All bad province values seen: {dict(all_bad_provinces.most_common(30))}\n")
if foreign_prov_depts:
    lines.append("| Dept | Department | Count | Values seen |")
    lines.append("|---|---|---|---|")
    for dept, counter in foreign_prov_depts[:20]:
        name = dept_names.get(dept, dept)
        total_count = sum(counter.values())
        values = ", ".join(f"{k}:{v}" for k, v in counter.most_common(10))
        lines.append(f"| {dept} | {name} | {total_count:,} | {values} |")
else:
    lines.append("None found.")

lines.append(f"\n---\n*Generated by Canadian Nonprofit Data project — github.com/mlenczner/CanadianNonprofitData*")

outfile = "docs/geo-date-anomaly-report.md"
with open(outfile, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"Report written to: {outfile}")
