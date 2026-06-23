"""
GC Grants & Contributions — Date and Geography Anomaly Analysis
Identifies departments responsible for garbage dates and province/country contamination.
Run with: python3 analysis/geo_date_anomalies.py grants.csv
"""

import sys
import csv
from collections import defaultdict, Counter
from datetime import datetime

FILE = sys.argv[1] if len(sys.argv) > 1 else "grants.csv"

def parse_date(val):
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(val.strip(), fmt)
        except Exception:
            pass
    return None

# Valid values
VALID_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"
}
VALID_COUNTRY_CODES = set()  # we'll treat anything not uppercase 2-letter as suspicious
LOWERCASE_CA = "ca"

# Date thresholds
MIN_VALID_DATE = datetime(1990, 1, 1)
MAX_VALID_DATE = datetime(2026, 12, 31)  # anything beyond current year + 1 is suspicious
EXCEL_NULL_DATE = datetime(1899, 12, 30)

# Per-dept accumulators
dept_names = {}
dept_total = defaultdict(int)

# Date anomalies
dept_date_past = defaultdict(list)    # dept -> list of (date_str, ref_number)
dept_date_future = defaultdict(list)  # dept -> list of (date_str, ref_number)
dept_excel_null = defaultdict(int)    # dept -> count of 1899-12-30

# Geography anomalies
dept_lowercase_country = defaultdict(int)    # dept -> count of lowercase country codes
dept_invalid_province = defaultdict(lambda: Counter())  # dept -> Counter of bad province values
dept_foreign_in_province = defaultdict(lambda: Counter())  # dept -> Counter of foreign codes in province

# All bad province values seen
all_bad_provinces = Counter()
all_bad_countries = Counter()

print(f"Reading {FILE}...", flush=True)

total = 0
with open(FILE, newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        total += 1
        if total % 200_000 == 0:
            print(f"  ... {total:,} rows", flush=True)

        dept = row.get("owner_org", "").strip()
        title = row.get("owner_org_title", "").strip()
        if dept and title:
            dept_names[dept] = title
        dept_total[dept] += 1

        ref = row.get("ref_number", "").strip()

        # ── Date anomalies ──────────────────────────────────────────────────
        date_str = row.get("agreement_start_date", "").strip()
        if date_str:
            d = parse_date(date_str)
            if d:
                if d == EXCEL_NULL_DATE:
                    dept_excel_null[dept] += 1
                elif d < MIN_VALID_DATE:
                    dept_date_past[dept].append((date_str, ref))
                elif d > MAX_VALID_DATE:
                    dept_date_future[dept].append((date_str, ref))

        # ── Country anomalies ───────────────────────────────────────────────
        country = row.get("recipient_country", "").strip()
        if country:
            # Lowercase codes
            if country != country.upper():
                dept_lowercase_country[dept] += 1
                all_bad_countries[country] += 1

        # ── Province anomalies ──────────────────────────────────────────────
        province = row.get("recipient_province", "").strip()
        country_upper = country.upper() if country else ""

        if province and province not in VALID_PROVINCES:
            all_bad_provinces[province] += 1
            # Is it a foreign state/country code?
            if len(province) == 2 and province.upper() == province:
                dept_foreign_in_province[dept][province] += 1
            else:
                dept_invalid_province[dept][province] += 1

print(f"\nTotal rows processed: {total:,}\n")

# ── OUTPUT ───────────────────────────────────────────────────────────────────
timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

lines = []
lines.append(f"# GC Grants — Date and Geography Anomaly Report")
lines.append(f"Generated: {timestamp}  |  Total records: {total:,}\n")

# ── SECTION 1: Date anomalies ─────────────────────────────────────────────
lines.append("## 1. Garbage Dates\n")

# Excel null dates
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

# Past dates (pre-1990, excluding Excel null)
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

# Future dates
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

# ── SECTION 2: Geography anomalies ───────────────────────────────────────────
lines.append(f"\n## 2. Geographic Field Contamination\n")

# Lowercase country codes
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

# Foreign codes in province field
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
