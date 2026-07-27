"""
GC Grants & Contributions — Dataset Profiler
Reads the full grants.csv and outputs a compact profile.
Run with: python profile_grants.py /path/to/grants.csv

Reads grants.csv via DuckDB instead of streaming it row-by-row through
Python's csv module -- verified byte-for-byte identical stdout output
against the original row-by-row version. Per-field completeness and
category value-counts are dynamic SQL aggregations; money/description-length
statistics (median, percentiles) are computed in Python on a plain fetched
list of values, using the exact same positional-index arithmetic as the
original (values_sorted[int(n*pct/100)], not an interpolated percentile),
so those numbers are guaranteed identical, not just close.
"""

import sys
from collections import Counter, defaultdict
from datetime import datetime

import duckdb

FILE = sys.argv[1] if len(sys.argv) > 1 else "grants.csv"
TOP_N = 20
BOILERPLATE_THRESHOLD = 50

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

print(f"\n{'='*60}")
print(f"GC GRANTS & CONTRIBUTIONS — DATASET PROFILE")
print(f"File: {FILE}")
print(f"Run:  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"{'='*60}\n")

con = duckdb.connect()
csv_src = f"read_csv('{FILE}', all_varchar=true)"

columns = [d[0] for d in con.execute(f"SELECT * FROM {csv_src} LIMIT 0").description]
total_rows = con.execute(f"SELECT COUNT(*) FROM {csv_src}").fetchone()[0]

# ── per-field completeness (dynamic across all discovered columns) ──────────
missing_exprs = ", ".join(
    f'SUM(CASE WHEN "{c}" IS NULL OR TRIM("{c}") = \'\' THEN 1 ELSE 0 END) AS "{c}"' for c in columns
)
missing_row = con.execute(f"SELECT {missing_exprs} FROM {csv_src}").fetchone()
empty_counts = dict(zip(columns, missing_row))

# ── category value counts ────────────────────────────────────────────────────
def value_counter(expr, where=""):
    rows = con.execute(f"SELECT {expr} AS v, COUNT(*) AS c FROM {csv_src} {where} GROUP BY 1").fetchall()
    counter = Counter()
    for v, c in rows:
        counter[v or ""] = c
    return counter

dept_rows = con.execute(f"""
    SELECT split_part(ref_number, '-', 1) AS dept, COUNT(*) AS c
    FROM {csv_src} WHERE ref_number IS NOT NULL AND ref_number LIKE '%-%'
    GROUP BY 1
""").fetchall()
dept_counter = Counter({d: c for d, c in dept_rows})

agreement_type_counter = value_counter('TRIM(agreement_type)')
recipient_type_counter = value_counter('TRIM(recipient_type)')
country_counter = value_counter('TRIM(recipient_country)')
province_counter = value_counter('TRIM(recipient_province)')
amendment_counter = value_counter('TRIM(amendment_number)')

foreign_currency_count = con.execute(
    f"SELECT COUNT(*) FROM {csv_src} WHERE foreign_currency_type IS NOT NULL AND TRIM(foreign_currency_type) != ''"
).fetchone()[0]

# ── money values (list, same statistics arithmetic as the original) ─────────
values = [
    row[0] for row in con.execute(f"""
        SELECT TRY_CAST(REPLACE(REPLACE(TRIM(agreement_value), ',', ''), '$', '') AS DOUBLE) AS v
        FROM {csv_src}
    """).fetchall()
    if row[0] is not None
]

# ── description lengths (list, same arithmetic as the original) ─────────────
description_lengths = [
    row[0] for row in con.execute(f"""
        SELECT LENGTH(TRIM(description_en)) AS len
        FROM {csv_src}
        WHERE description_en IS NOT NULL AND TRIM(description_en) != ''
    """).fetchall()
]
boilerplate_suspects = sum(1 for l in description_lengths if l < BOILERPLATE_THRESHOLD)

# ── dates: fiscal year counter, multi-year, post-Dec-2025 ────────────────────
date_rows = con.execute(f"""
    WITH parsed AS (
        SELECT
            COALESCE(
                TRY_STRPTIME(TRIM(agreement_start_date), '%Y-%m-%d'),
                TRY_STRPTIME(TRIM(agreement_start_date), '%Y/%m/%d'),
                TRY_STRPTIME(TRIM(agreement_start_date), '%d/%m/%Y')
            ) AS sd,
            COALESCE(
                TRY_STRPTIME(TRIM(agreement_end_date), '%Y-%m-%d'),
                TRY_STRPTIME(TRIM(agreement_end_date), '%Y/%m/%d'),
                TRY_STRPTIME(TRIM(agreement_end_date), '%d/%m/%Y')
            ) AS ed
        FROM {csv_src}
    )
    SELECT sd, ed FROM parsed WHERE sd IS NOT NULL
""").fetchall()

start_dates = [sd for sd, ed in date_rows]
fiscal_year_counter = Counter()
multi_year_count = 0
DEC2025 = datetime(2025, 12, 1)
for sd, ed in date_rows:
    fy_year = sd.year if sd.month >= 4 else sd.year - 1
    fiscal_year_counter[f"{fy_year}-{fy_year+1}"] += 1
    if ed and ed.year > sd.year + (1 if sd.month >= 4 else 0):
        multi_year_count += 1

post_dec2025_count = sum(1 for sd, ed in date_rows if sd >= DEC2025)
post_dec2025_missing = defaultdict(int)
if post_dec2025_count:
    pd_exprs = ", ".join(
        f'SUM(CASE WHEN sd >= DATE \'2025-12-01\' AND ("{f}" IS NULL OR TRIM("{f}") = \'\') THEN 1 ELSE 0 END) AS "{f}"'
        for f in CONDITIONAL_MANDATORY_POST_DEC2025
    )
    pd_row = con.execute(f"""
        WITH parsed AS (
            SELECT *, COALESCE(
                TRY_STRPTIME(TRIM(agreement_start_date), '%Y-%m-%d'),
                TRY_STRPTIME(TRIM(agreement_start_date), '%Y/%m/%d'),
                TRY_STRPTIME(TRIM(agreement_start_date), '%d/%m/%Y')
            ) AS sd
            FROM {csv_src}
        )
        SELECT {pd_exprs} FROM parsed
    """).fetchone()
    post_dec2025_missing = dict(zip(CONDITIONAL_MANDATORY_POST_DEC2025, pd_row))

# ── OUTPUT (unchanged from the original) ─────────────────────────────────────

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
    for pct in [25, 75, 90, 95, 99]:
        idx = int(n * pct / 100)
        print(f"  {pct}th percentile:    ${values_sorted[idx]:,.0f}")
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
