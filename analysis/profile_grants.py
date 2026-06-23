"""
GC Grants & Contributions — Dataset Profiler
Reads the full grants.csv and outputs a compact profile.
Run with: python profile_grants.py /path/to/grants.csv
"""

import sys
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime

# ── config ──────────────────────────────────────────────────────────────────
FILE = sys.argv[1] if len(sys.argv) > 1 else "grants.csv"
TOP_N = 20          # how many top values to show per field
SAMPLE_ROWS = None  # set to e.g. 500_000 to profile a subset; None = all rows

# Fields we care about for completeness audit
MANDATORY_FIELDS = [
    "ref_number", "recipient_legal_name", "recipient_country",
    "recipient_city", "agreement_value", "agreement_type",
    "agreement_start_date", "description_en", "description_fr",
]
CONDITIONAL_MANDATORY_POST_DEC2025 = [
    "recipient_type", "recipient_business_number", "recipient_postal_code",
    "federal_riding_number", "prog_name_en", "prog_name_fr",
    "prog_purpose_en", "prog_purpose_fr", "agreement_title_en",
    "agreement_title_fr", "agreement_end_date", "expected_results_en",
    "expected_results_fr",
]
QUALITY_TEXT_FIELDS = ["description_en", "prog_purpose_en", "expected_results_en"]

# ── helpers ──────────────────────────────────────────────────────────────────
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

# ── main ─────────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"GC GRANTS & CONTRIBUTIONS — DATASET PROFILE")
print(f"File: {FILE}")
print(f"Run:  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"{'='*60}\n")

total_rows = 0
columns = []

# Counters
empty_counts = defaultdict(int)       # field -> count of empty values
value_counters = defaultdict(Counter) # field -> Counter of values

# Specific trackers
values = []            # all agreement_value floats
start_dates = []       # all parsed start dates
dept_counter = Counter()
agreement_type_counter = Counter()
recipient_type_counter = Counter()
country_counter = Counter()
province_counter = Counter()
amendment_counter = Counter()
fiscal_year_counter = Counter()
multi_year_count = 0
foreign_currency_count = 0
post_dec2025_count = 0
post_dec2025_missing = defaultdict(int)
description_lengths = []
boilerplate_suspects = 0
BOILERPLATE_THRESHOLD = 50  # chars

DEC2025 = datetime(2025, 12, 1)

try:
    with open(FILE, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []

        for row in reader:
            total_rows += 1
            if SAMPLE_ROWS and total_rows > SAMPLE_ROWS:
                break

            if total_rows % 100_000 == 0:
                print(f"  ... processed {total_rows:,} rows", flush=True)

            # Completeness
            for field in columns:
                if is_empty(row.get(field)):
                    empty_counts[field] += 1

            # Department (from ref_number prefix)
            ref = row.get("ref_number", "")
            if ref and "-" in ref:
                dept_counter[ref.split("-")[0]] += 1

            # Agreement type
            agreement_type_counter[row.get("agreement_type", "").strip()] += 1

            # Recipient type
            recipient_type_counter[row.get("recipient_type", "").strip()] += 1

            # Country
            country_counter[row.get("recipient_country", "").strip()] += 1

            # Province
            province_counter[row.get("recipient_province", "").strip()] += 1

            # Amendment
            amend = row.get("amendment_number", "").strip()
            amendment_counter[amend] += 1

            # Value
            v = parse_money(row.get("agreement_value", ""))
            if v is not None:
                values.append(v)

            # Start date
            sd = parse_date(row.get("agreement_start_date", ""))
            if sd:
                start_dates.append(sd)
                fy_year = sd.year if sd.month >= 4 else sd.year - 1
                fiscal_year_counter[f"{fy_year}-{fy_year+1}"] += 1

                # Multi-year: end date exists and is in different fiscal year
                ed = parse_date(row.get("agreement_end_date", ""))
                if ed and ed.year > sd.year + (1 if sd.month >= 4 else 0):
                    multi_year_count += 1

                # Post-Dec 2025 compliance check
                if sd >= DEC2025:
                    post_dec2025_count += 1
                    for field in CONDITIONAL_MANDATORY_POST_DEC2025:
                        if is_empty(row.get(field)):
                            post_dec2025_missing[field] += 1

            # Foreign currency
            if not is_empty(row.get("foreign_currency_type", "")):
                foreign_currency_count += 1

            # Description quality (length proxy)
            desc = row.get("description_en", "").strip()
            if desc:
                description_lengths.append(len(desc))
                if len(desc) < BOILERPLATE_THRESHOLD:
                    boilerplate_suspects += 1

except FileNotFoundError:
    print(f"ERROR: File not found: {FILE}")
    sys.exit(1)

# ── OUTPUT ───────────────────────────────────────────────────────────────────

print(f"\n── BASIC STATS ──────────────────────────────────────────")
print(f"Total rows:        {total_rows:,}")
print(f"Columns detected:  {len(columns)}")
print(f"Columns: {', '.join(columns)}\n")

print(f"\n── FIELD COMPLETENESS (mandatory fields) ────────────────")
print(f"{'Field':<40} {'Missing':>10} {'% Complete':>12}")
print("-" * 65)
for field in MANDATORY_FIELDS:
    missing = empty_counts.get(field, 0)
    pct = 100 * (1 - missing / total_rows) if total_rows else 0
    flag = " ⚠" if pct < 95 else ""
    print(f"{field:<40} {missing:>10,} {pct:>11.1f}%{flag}")

print(f"\n── POST-DEC 2025 COMPLIANCE ({post_dec2025_count:,} records) ─────────")
if post_dec2025_count > 0:
    print(f"{'Field':<40} {'Missing':>10} {'% Complete':>12}")
    print("-" * 65)
    for field in CONDITIONAL_MANDATORY_POST_DEC2025:
        missing = post_dec2025_missing.get(field, 0)
        pct = 100 * (1 - missing / post_dec2025_count)
        flag = " ⚠" if pct < 95 else ""
        print(f"{field:<40} {missing:>10,} {pct:>11.1f}%{flag}")
else:
    print("  No records with agreement_start_date >= 2025-12-01 found.")

print(f"\n── ALL FIELDS COMPLETENESS (sorted by % missing) ────────")
print(f"{'Field':<40} {'Missing':>10} {'% Complete':>12}")
print("-" * 65)
sorted_fields = sorted(columns, key=lambda f: empty_counts.get(f, 0), reverse=True)
for field in sorted_fields:
    missing = empty_counts.get(field, 0)
    pct = 100 * (1 - missing / total_rows) if total_rows else 0
    print(f"{field:<40} {missing:>10,} {pct:>11.1f}%")

print(f"\n── AGREEMENT VALUES (CAD) ───────────────────────────────")
if values:
    values_sorted = sorted(values)
    n = len(values_sorted)
    total_val = sum(values_sorted)
    print(f"Count with value:    {n:,}")
    print(f"Total value:         ${total_val:,.0f}")
    print(f"Mean:                ${total_val/n:,.0f}")
    print(f"Median:              ${values_sorted[n//2]:,.0f}")
    print(f"Min:                 ${values_sorted[0]:,.0f}")
    print(f"Max:                 ${values_sorted[-1]:,.0f}")
    # Percentiles
    for pct in [25, 75, 90, 95, 99]:
        idx = int(n * pct / 100)
        print(f"  {pct}th percentile:    ${values_sorted[idx]:,.0f}")
    # Zero or negative
    zero_neg = sum(1 for v in values_sorted if v <= 0)
    print(f"Zero or negative:    {zero_neg:,}")
else:
    print("  No parseable values found.")

print(f"\n── DATE RANGE ───────────────────────────────────────────")
if start_dates:
    print(f"Earliest start date: {min(start_dates).strftime('%Y-%m-%d')}")
    print(f"Latest start date:   {max(start_dates).strftime('%Y-%m-%d')}")
print(f"Multi-year awards:   {multi_year_count:,} ({100*multi_year_count/total_rows:.1f}% of total)")
print(f"Foreign currency:    {foreign_currency_count:,} ({100*foreign_currency_count/total_rows:.1f}% of total)")

print(f"\n── TOP {TOP_N} FISCAL YEARS BY RECORD COUNT ─────────────────")
for fy, count in fiscal_year_counter.most_common(TOP_N):
    print(f"  {fy}: {count:,}")

print(f"\n── AGREEMENT TYPE BREAKDOWN ─────────────────────────────")
for k, v in agreement_type_counter.most_common():
    print(f"  {k or '(empty)'}: {v:,} ({100*v/total_rows:.1f}%)")

print(f"\n── RECIPIENT TYPE BREAKDOWN ─────────────────────────────")
for k, v in recipient_type_counter.most_common():
    label = {
        "A": "Indigenous", "F": "For-profit", "G": "Government",
        "I": "International (non-govt)", "N": "NFP/Charity",
        "O": "Other", "P": "Individual/Sole proprietor", "S": "Academia",
        "": "(empty)"
    }.get(k, k)
    print(f"  {k or '(empty)'} ({label}): {v:,} ({100*v/total_rows:.1f}%)")

print(f"\n── TOP {TOP_N} DEPARTMENTS (by ref_number prefix) ───────────")
for dept, count in dept_counter.most_common(TOP_N):
    print(f"  {dept}: {count:,}")

print(f"\n── TOP {TOP_N} RECIPIENT COUNTRIES ──────────────────────────")
for country, count in country_counter.most_common(TOP_N):
    print(f"  {country or '(empty)'}: {count:,}")

print(f"\n── TOP {TOP_N} RECIPIENT PROVINCES ──────────────────────────")
for prov, count in province_counter.most_common(TOP_N):
    print(f"  {prov or '(empty)'}: {count:,}")

print(f"\n── AMENDMENT DISTRIBUTION ───────────────────────────────")
for amend, count in sorted(amendment_counter.items(), key=lambda x: (len(x[0]), x[0]))[:15]:
    print(f"  amendment_number={amend or '(empty)'}: {count:,}")

print(f"\n── DESCRIPTION QUALITY (description_en) ─────────────────")
if description_lengths:
    dl = sorted(description_lengths)
    n = len(dl)
    print(f"  Records with description: {n:,} ({100*n/total_rows:.1f}%)")
    print(f"  Median length (chars):    {dl[n//2]:,}")
    print(f"  Mean length (chars):      {sum(dl)//n:,}")
    print(f"  Under {BOILERPLATE_THRESHOLD} chars (likely boilerplate): {boilerplate_suspects:,} ({100*boilerplate_suspects/n:.1f}%)")
else:
    print("  No descriptions found.")

print(f"\n{'='*60}")
print("PROFILE COMPLETE")
print(f"{'='*60}\n")
