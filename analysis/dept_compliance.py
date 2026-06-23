"""
GC Grants & Contributions — Per-Department Compliance Breakdown
Outputs a detailed compliance report by department.
Run with: python3 analysis/dept_compliance.py grants.csv
"""

import sys
import csv
import json
from collections import defaultdict
from datetime import datetime

FILE = sys.argv[1] if len(sys.argv) > 1 else "grants.csv"

DEC2025 = datetime(2025, 12, 1)

def is_empty(val):
    return val is None or str(val).strip() == ""

def parse_date(val):
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(val.strip(), fmt)
        except Exception:
            pass
    return None

def parse_money(val):
    try:
        return float(str(val).replace(",", "").replace("$", "").strip())
    except Exception:
        return None

# Per-department accumulators
dept_names = {}          # owner_org -> owner_org_title
dept_total = defaultdict(int)
dept_value = defaultdict(float)

# Compliance fields to check for ALL records
all_record_fields = [
    "description_en",
    "description_fr",
    "agreement_type",
]

# Post-Dec 2025 mandatory fields
post_dec_fields = [
    "recipient_type",
    "recipient_business_number",
    "recipient_postal_code",
    "federal_riding_number",
    "prog_name_en",
    "prog_name_fr",
    "prog_purpose_en",
    "prog_purpose_fr",
    "agreement_title_en",
    "agreement_title_fr",
    "agreement_end_date",
    "expected_results_en",
    "expected_results_fr",
]

VALID_AGREEMENT_TYPES = {"C", "G", "O"}

# Per-dept counters
dept_missing = defaultdict(lambda: defaultdict(int))       # dept -> field -> missing count
dept_dirty_type = defaultdict(int)                         # dept -> count of non-standard agreement_type
dept_short_desc = defaultdict(int)                         # dept -> count of descriptions < 50 chars
dept_zero_neg_value = defaultdict(int)                     # dept -> count of zero/negative values
dept_post_dec_total = defaultdict(int)                     # dept -> post-dec record count
dept_post_dec_missing = defaultdict(lambda: defaultdict(int))  # dept -> field -> missing count

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

        v = parse_money(row.get("agreement_value", ""))
        if v is not None:
            dept_value[dept] += v
            if v <= 0:
                dept_zero_neg_value[dept] += 1

        # All-record field checks
        for field in all_record_fields:
            if is_empty(row.get(field)):
                dept_missing[dept][field] += 1

        # Agreement type cleanliness
        atype = row.get("agreement_type", "").strip()
        if atype and atype not in VALID_AGREEMENT_TYPES:
            dept_dirty_type[dept] += 1

        # Short description
        desc = row.get("description_en", "").strip()
        if desc and len(desc) < 50:
            dept_short_desc[dept] += 1

        # Post-Dec 2025
        sd = parse_date(row.get("agreement_start_date", ""))
        if sd and sd >= DEC2025:
            dept_post_dec_total[dept] += 1
            for field in post_dec_fields:
                if is_empty(row.get(field)):
                    dept_post_dec_missing[dept][field] += 1

print(f"\nTotal rows: {total:,}")
print(f"Departments found: {len(dept_total)}\n")

# ── OUTPUT ───────────────────────────────────────────────────────────────────

timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

lines = []
lines.append(f"# GC Grants — Per-Department Compliance Report")
lines.append(f"Generated: {timestamp}  |  Total records: {total:,}  |  Departments: {len(dept_total)}\n")

# ── SECTION 1: Overall summary table ────────────────────────────────────────
lines.append("## 1. Overall Compliance Summary (all departments)\n")
lines.append(f"| Dept Code | Department | Records | Total Value (CAD) | Missing desc_en | Missing desc_fr | Dirty type | Short desc | Zero/Neg value |")
lines.append(f"|---|---|---|---|---|---|---|---|---|")

# Sort by record count descending
for dept in sorted(dept_total, key=lambda d: dept_total[d], reverse=True):
    n = dept_total[dept]
    name = dept_names.get(dept, dept)[:60]
    val = dept_value[dept]
    miss_en = dept_missing[dept].get("description_en", 0)
    miss_fr = dept_missing[dept].get("description_fr", 0)
    dirty = dept_dirty_type[dept]
    short = dept_short_desc[dept]
    zeroneg = dept_zero_neg_value[dept]

    pct_en = 100 * miss_en / n if n else 0
    pct_fr = 100 * miss_fr / n if n else 0

    flag_en = f"{miss_en:,} ({pct_en:.0f}%)" if miss_en else "✓"
    flag_fr = f"{miss_fr:,} ({pct_fr:.0f}%)" if miss_fr else "✓"
    flag_dirty = f"{dirty:,}" if dirty else "✓"
    flag_short = f"{short:,} ({100*short/n:.0f}%)" if short else "✓"
    flag_zeroneg = f"{zeroneg:,}" if zeroneg else "✓"

    lines.append(f"| {dept} | {name} | {n:,} | ${val:,.0f} | {flag_en} | {flag_fr} | {flag_dirty} | {flag_short} | {flag_zeroneg} |")

# ── SECTION 2: Post-Dec 2025 compliance by department ───────────────────────
lines.append(f"\n## 2. Post-December 2025 Compliance (new mandatory fields)\n")
lines.append("Only departments with at least 1 record with agreement_start_date >= 2025-12-01 are shown.\n")

post_dec_depts = [(d, dept_post_dec_total[d]) for d in dept_post_dec_total if dept_post_dec_total[d] > 0]
post_dec_depts.sort(key=lambda x: x[1], reverse=True)

# Header
header_fields = ["recipient_type", "recipient_business_number", "recipient_postal_code",
                 "federal_riding_number", "prog_name_en", "agreement_end_date", "expected_results_en"]
short_labels = ["recip_type", "biz_num", "postal", "riding_num", "prog_name", "end_date", "exp_results"]

lines.append(f"| Dept | Department | Post-Dec Records | " + " | ".join(short_labels) + " |")
lines.append(f"|---|---|---|" + "|".join(["---"] * len(short_labels)) + "|")

for dept, n in post_dec_depts:
    name = dept_names.get(dept, dept)[:50]
    cells = []
    for field in header_fields:
        missing = dept_post_dec_missing[dept].get(field, 0)
        pct = 100 * (1 - missing / n) if n else 0
        if missing == 0:
            cells.append("✓")
        else:
            cells.append(f"{pct:.0f}%")
    lines.append(f"| {dept} | {name} | {n:,} | " + " | ".join(cells) + " |")

# ── SECTION 3: Worst offenders ───────────────────────────────────────────────
lines.append(f"\n## 3. Worst Offenders\n")

lines.append("### Missing description_en (mandatory field, all records)\n")
lines.append(f"| Dept | Department | Records | Missing | % Missing |")
lines.append(f"|---|---|---|---|---|")
offenders = [(d, dept_missing[d].get("description_en", 0)) for d in dept_total]
offenders.sort(key=lambda x: x[1], reverse=True)
for dept, missing in offenders[:20]:
    if missing == 0:
        break
    n = dept_total[dept]
    name = dept_names.get(dept, dept)[:60]
    pct = 100 * missing / n
    lines.append(f"| {dept} | {name} | {n:,} | {missing:,} | {pct:.1f}% |")

lines.append(f"\n### Short descriptions under 50 chars (description_en)\n")
lines.append(f"| Dept | Department | Records | Short desc | % Short |")
lines.append(f"|---|---|---|---|---|")
short_offenders = [(d, dept_short_desc[d]) for d in dept_total if dept_short_desc[d] > 0]
short_offenders.sort(key=lambda x: x[1], reverse=True)
for dept, short in short_offenders[:20]:
    n = dept_total[dept]
    name = dept_names.get(dept, dept)[:60]
    pct = 100 * short / n
    lines.append(f"| {dept} | {name} | {n:,} | {short:,} | {pct:.1f}% |")

lines.append(f"\n### Dirty agreement_type values (non-standard codes)\n")
lines.append(f"| Dept | Department | Records | Dirty type count |")
lines.append(f"|---|---|---|---|")
dirty_offenders = [(d, dept_dirty_type[d]) for d in dept_total if dept_dirty_type[d] > 0]
dirty_offenders.sort(key=lambda x: x[1], reverse=True)
for dept, dirty in dirty_offenders[:20]:
    n = dept_total[dept]
    name = dept_names.get(dept, dept)[:60]
    lines.append(f"| {dept} | {name} | {n:,} | {dirty:,} |")

lines.append(f"\n### Missing federal_riding_number (post-Dec 2025, mandatory)\n")
lines.append(f"| Dept | Department | Post-Dec Records | Missing riding | % Missing |")
lines.append(f"|---|---|---|---|---|")
riding_offenders = [(d, dept_post_dec_missing[d].get("federal_riding_number", 0))
                    for d in dept_post_dec_total if dept_post_dec_missing[d].get("federal_riding_number", 0) > 0]
riding_offenders.sort(key=lambda x: x[1], reverse=True)
for dept, missing in riding_offenders[:20]:
    n = dept_post_dec_total[dept]
    name = dept_names.get(dept, dept)[:60]
    pct = 100 * missing / n
    lines.append(f"| {dept} | {name} | {n:,} | {missing:,} | {pct:.1f}% |")

lines.append(f"\n### Missing recipient_business_number (post-Dec 2025, mandatory)\n")
lines.append(f"| Dept | Department | Post-Dec Records | Missing biz# | % Missing |")
lines.append(f"|---|---|---|---|---|")
biz_offenders = [(d, dept_post_dec_missing[d].get("recipient_business_number", 0))
                 for d in dept_post_dec_total if dept_post_dec_missing[d].get("recipient_business_number", 0) > 0]
biz_offenders.sort(key=lambda x: x[1], reverse=True)
for dept, missing in biz_offenders[:20]:
    n = dept_post_dec_total[dept]
    name = dept_names.get(dept, dept)[:60]
    pct = 100 * missing / n
    lines.append(f"| {dept} | {name} | {n:,} | {missing:,} | {pct:.1f}% |")

# ── SECTION 4: Exemplary departments ─────────────────────────────────────────
lines.append(f"\n## 4. Exemplary Departments (100% on all checked fields)\n")
lines.append(f"Departments with 50+ records, no missing descriptions, no dirty type values, no short descriptions.\n")
lines.append(f"| Dept | Department | Records | Total Value (CAD) |")
lines.append(f"|---|---|---|---|")
for dept in sorted(dept_total, key=lambda d: dept_total[d], reverse=True):
    n = dept_total[dept]
    if n < 50:
        continue
    if (dept_missing[dept].get("description_en", 0) == 0 and
        dept_missing[dept].get("description_fr", 0) == 0 and
        dept_dirty_type[dept] == 0 and
        dept_short_desc[dept] == 0):
        name = dept_names.get(dept, dept)[:60]
        val = dept_value[dept]
        lines.append(f"| {dept} | {name} | {n:,} | ${val:,.0f} |")

lines.append(f"\n---\n*Generated by Canadian Nonprofit Data project — github.com/mlenczner/CanadianNonprofitData*")

# Write output
outfile = "docs/dept-compliance-report.md"
with open(outfile, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"\nReport written to: {outfile}")
print("Push to GitHub with: git add docs/dept-compliance-report.md && git commit -m 'Add dept compliance report' && git push")
