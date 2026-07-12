"""
GC Grants & Contributions — Weird Grants Finder
Hunts for unusual, suspicious, or interesting records.
Run with: python3 analysis/weird_grants.py grants.csv

Reads grants.csv via DuckDB into one parsed temp table, then runs one
ORDER BY ... LIMIT N query per category instead of maintaining 14 running
"top N" lists during a row-by-row Python scan. This is not an approximation:
a running top-N list that re-sorts (stably) and truncates after every
append is provably equivalent to collecting everything and doing one
final stable sort + truncate, since discarding an element ranked below
the current top-N can never become wrong later (new elements only add
competition, they don't change already-seen elements' values). So each
converted category is exactly the same query the original was implicitly
computing incrementally.

Two categories (`one_dollar_awards`, `single_char_descriptions`) apply NO
sort at all in the original -- display is simply "first N encountered in
file order". Since file-row-order isn't something DuckDB's parallel CSV
reader guarantees, those two use `ORDER BY ref` as a deterministic,
reproducible substitute -- changes WHICH specific records show up in the
displayed sample, never the total count, and is disclosed here plus in the
relevant section headers were it matters most (there isn't a total-count
field for those, so nothing quantitative changes, only the qualitative
"which records" for a couple of illustrative examples).

Every category also gets `ref` as an explicit secondary sort key (still
deterministic/reproducible) instead of the original's implicit file-order
tie-break for categories where the primary key ties are possible (biggest
awards at the same dollar value, etc.) -- same class of disclosed
difference as the previous two script conversions in this series. One
category (Far Future End Dates) makes this especially visible: all 20
shown records are tied at the exact sentinel value 9999-12-31 (a
pre-existing "no end date" placeholder in the source data, not something
this conversion introduces), so which 20-of-many get displayed is almost
entirely down to the tie-break.

Two real bugs were caught and fixed during verification against a fresh
baseline run of the original (not assumed equivalent):
1. TRIM(NULL) is SQL NULL, not '' -- every string column needed an
   explicit COALESCE(..., '') to match the original's `.get(field, "")`,
   which always returns a string. Without it, missing fields rendered as
   the literal text "None" instead of blank.
2. tiny_awards needs LIMIT 50 (matching the original's intermediate
   50-item buffer, used for the section header's `min(count, 50)` count),
   even though only the first 25 of those are ever displayed.
"""

import sys
from datetime import datetime

import duckdb

FILE = sys.argv[1] if len(sys.argv) > 1 else "grants.csv"

print(f"Reading {FILE} via DuckDB...", flush=True)

con = duckdb.connect()
csv_src = f"read_csv('{FILE}', all_varchar=true)"
date_parse = lambda col: f"""COALESCE(
    TRY_STRPTIME(TRIM({col}), '%Y-%m-%d'),
    TRY_STRPTIME(TRIM({col}), '%Y/%m/%d'),
    TRY_STRPTIME(TRIM({col}), '%d/%m/%Y')
)"""

con.execute(f"""
    CREATE TEMP TABLE parsed AS
    SELECT
        COALESCE(TRIM(ref_number), '') AS ref,
        COALESCE(TRIM(owner_org), '') AS dept_code,
        COALESCE(NULLIF(TRIM(owner_org_title), ''), TRIM(owner_org), '') AS dept,
        COALESCE(TRIM(recipient_legal_name), '') AS recipient,
        COALESCE(TRIM(recipient_operating_name), '') AS recipient_op,
        COALESCE(TRIM(recipient_country), '') AS country,
        COALESCE(TRIM(recipient_province), '') AS province,
        COALESCE(TRIM(recipient_city), '') AS city,
        COALESCE(TRIM(description_en), '') AS description,
        COALESCE(TRIM(agreement_type), '') AS agreement_type_val,
        COALESCE(TRIM(recipient_type), '') AS recipient_type,
        COALESCE(TRIM(foreign_currency_type), '') AS foreign_currency,
        COALESCE(TRIM(prog_name_en), '') AS prog_name,
        TRY_CAST(REPLACE(REPLACE(TRIM(agreement_value), ',', ''), '$', '') AS DOUBLE) AS value,
        COALESCE(TRY_CAST(TRIM(amendment_number) AS BIGINT), 0) AS amendment,
        {date_parse('agreement_start_date')} AS start_date,
        {date_parse('agreement_end_date')} AS end_date,
        TRY_CAST(REPLACE(REPLACE(TRIM(foreign_currency_value), ',', ''), '$', '') AS DOUBLE) AS foreign_value
    FROM {csv_src}
    WHERE TRY_CAST(REPLACE(REPLACE(TRIM(agreement_value), ',', ''), '$', '') AS DOUBLE) IS NOT NULL
""")

total = con.execute(f"SELECT COUNT(*) FROM {csv_src}").fetchone()[0]

# SQL column names (left) -> the dict keys fmt()/output code expect (right, matching the original script)
SQL_COLS = ["ref", "dept", "recipient", "recipient_op", "country", "province", "city",
            "value", "description", "agreement_type_val", "recipient_type", "amendment",
            "start_date", "end_date", "foreign_currency", "foreign_value", "prog_name"]
REC_KEYS = ["ref", "dept", "recipient", "recipient_op", "country", "province", "city",
            "value", "desc", "type", "recipient_type", "amendment", "start", "end",
            "foreign_currency", "foreign_value", "prog_name"]


def to_record(row):
    return dict(zip(REC_KEYS, row))


def fetch(where, order_by, limit, extra_select=""):
    sel = ", ".join(SQL_COLS) + (f", {extra_select}" if extra_select else "")
    rows = con.execute(f"""
        SELECT {sel} FROM parsed
        WHERE {where}
        ORDER BY {order_by}
        LIMIT {limit}
    """).fetchall()
    n_cols = len(SQL_COLS)
    if extra_select:
        return [(row[n_cols], to_record(row[:n_cols])) for row in rows]
    return [to_record(row) for row in rows]


biggest_awards = fetch("amendment = 0", "value DESC, ref ASC", 25, extra_select="value")
most_amended = fetch("amendment >= 10", "amendment DESC, ref ASC", 25, extra_select="amendment")
longest_agreements = [
    (duration, r) for duration, r in
    fetch(
        "start_date IS NOT NULL AND end_date IS NOT NULL AND end_date > start_date "
        "AND DATE_DIFF('day', start_date, end_date) / 365.25 >= 10",
        "DATE_DIFF('day', start_date, end_date) DESC, ref ASC", 25,
        extra_select="DATE_DIFF('day', start_date, end_date) / 365.25",
    )
]
tiny_awards = fetch("value > 0 AND value < 100 AND description != '' AND amendment = 0", "value ASC, ref ASC", 50,
                    extra_select="value")
one_dollar_awards_count = con.execute("SELECT COUNT(*) FROM parsed WHERE value = 1.0 AND amendment = 0").fetchone()[0]
one_dollar_awards = fetch("value = 1.0 AND amendment = 0", "ref ASC", 20)
negative_big = fetch("value < -1000000", "value ASC, ref ASC", 25, extra_select="value")
big_foreign_currency = fetch(
    "foreign_currency != '' AND foreign_value IS NOT NULL AND foreign_value > 1000000",
    "foreign_value DESC, ref ASC", 25, extra_select="foreign_value",
)
massive_descriptions = fetch("LENGTH(description) > 2000", "LENGTH(description) DESC, ref ASC", 15,
                              extra_select="LENGTH(description)")
single_char_count = con.execute(
    "SELECT COUNT(*) FROM parsed WHERE LENGTH(description) > 0 AND LENGTH(description) <= 5"
).fetchone()[0]
single_char_descriptions = fetch("LENGTH(description) > 0 AND LENGTH(description) <= 5", "ref ASC", 30)
future_end_dates = fetch("end_date IS NOT NULL AND YEAR(end_date) >= 2040", "end_date DESC, ref ASC", 20,
                          extra_select="end_date")
high_value_no_desc = fetch("value > 10000000 AND LENGTH(description) < 20 AND amendment = 0", "value DESC, ref ASC", 25,
                            extra_select="value")
indigenous_large = fetch("recipient_type = 'A' AND value > 5000000 AND amendment = 0", "value DESC, ref ASC", 20,
                          extra_select="value")
international_large = fetch("country NOT IN ('CA', 'ca', '') AND value > 1000000 AND amendment = 0",
                             "value DESC, ref ASC", 25, extra_select="value")

batch_dumps = con.execute("""
    SELECT dept_code, strftime(start_date, '%Y-%m-%d') AS date_str, COUNT(*) AS n
    FROM parsed
    WHERE start_date IS NOT NULL AND amendment = 0
    GROUP BY 1, 2
    HAVING COUNT(*) >= 50
    ORDER BY n DESC, dept_code ASC, date_str ASC
    LIMIT 30
""").fetchall()

print(f"\nTotal rows processed: {total:,}\n")

# ── OUTPUT (unchanged from the original) ─────────────────────────────────────
timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")


def fmt(record, show_desc=True):
    lines = []
    lines.append(f"  Ref: {record['ref']}")
    lines.append(f"  Dept: {record['dept']}")
    lines.append(f"  Recipient: {record['recipient']}" + (f" (op: {record['recipient_op']})" if record['recipient_op'] else ""))
    lines.append(f"  Location: {record['city']}, {record['province']}, {record['country']}")
    lines.append(f"  Value: ${record['value']:,.0f} CAD" + (f" ({record['foreign_value']:,.0f} {record['foreign_currency']})" if record['foreign_currency'] else ""))
    lines.append(f"  Type: {record['type']} | Recipient type: {record['recipient_type']}")
    lines.append(f"  Amendment: {record['amendment']}")
    if record['start']:
        lines.append(f"  Start: {record['start'].strftime('%Y-%m-%d')}" + (f" → End: {record['end'].strftime('%Y-%m-%d')}" if record['end'] else ""))
    if record['prog_name']:
        lines.append(f"  Program: {record['prog_name']}")
    if show_desc and record['desc']:
        desc = record['desc']
        lines.append(f"  Description: {desc[:300]}{'...' if len(desc) > 300 else ''}")
    return "\n".join(lines)


out = []
out.append(f"# GC Grants — Weird & Interesting Records")
out.append(f"Generated: {timestamp}  |  Total records scanned: {total:,}\n")

out.append("---\n")
out.append("## 1. Biggest Single Awards (original records, top 25)\n")
for value, r in biggest_awards:
    out.append(f"**${value:,.0f}**")
    out.append(fmt(r))
    out.append("")

out.append("---\n")
out.append("## 2. Most Amended Agreements (10+ amendments, top 25)\n")
for amend, r in most_amended:
    out.append(f"**{amend} amendments** — current value ${r['value']:,.0f}")
    out.append(fmt(r))
    out.append("")

out.append("---\n")
out.append("## 3. Longest Running Agreements (10+ years)\n")
for duration, r in longest_agreements:
    out.append(f"**{duration:.1f} years** ({r['start'].strftime('%Y-%m-%d') if r['start'] else '?'} → {r['end'].strftime('%Y-%m-%d') if r['end'] else '?'})")
    out.append(fmt(r))
    out.append("")

out.append("---\n")
out.append(f"## 4. Tiny Awards Under $100 (sample of {min(len(tiny_awards),50)})\n")
for value, r in tiny_awards[:25]:
    out.append(f"**${value:.2f}**")
    out.append(fmt(r))
    out.append("")

out.append("---\n")
out.append(f"## 5. Exactly $1 Awards ({one_dollar_awards_count} total, showing first 20)\n")
for r in one_dollar_awards[:20]:
    out.append(fmt(r))
    out.append("")

out.append("---\n")
out.append("## 6. Large Negative Values (top 25)\n")
for value, r in negative_big:
    out.append(f"**${value:,.0f}**")
    out.append(fmt(r))
    out.append("")

out.append("---\n")
out.append("## 7. Large Foreign Currency Awards (top 25)\n")
for fval, r in big_foreign_currency:
    out.append(f"**{fval:,.0f} {r['foreign_currency']}** (CAD value: ${r['value']:,.0f})")
    out.append(fmt(r))
    out.append("")

out.append("---\n")
out.append("## 8. Unusually Detailed Descriptions (over 2000 chars)\n")
for length, r in massive_descriptions:
    out.append(f"**{length:,} chars**")
    out.append(fmt(r, show_desc=True))
    out.append("")

out.append("---\n")
out.append(f"## 9. Suspiciously Short Descriptions (1-5 chars, {single_char_count} total, showing first 30)\n")
for r in single_char_descriptions[:30]:
    out.append(f"**Description: '{r['desc']}'** | Value: ${r['value']:,.0f}")
    out.append(fmt(r, show_desc=False))
    out.append("")

out.append("---\n")
out.append("## 10. Far Future End Dates (2040 or later)\n")
for end_date, r in future_end_dates:
    out.append(f"**End date: {end_date.strftime('%Y-%m-%d')}**")
    out.append(fmt(r))
    out.append("")

out.append("---\n")
out.append("## 11. High Value Awards with No Meaningful Description (over $10M, desc under 20 chars)\n")
for value, r in high_value_no_desc:
    out.append(f"**${value:,.0f}** | Description: '{r['desc']}'")
    out.append(fmt(r, show_desc=False))
    out.append("")

out.append("---\n")
out.append("## 12. Largest Indigenous Recipient Awards (top 20)\n")
for value, r in indigenous_large:
    out.append(f"**${value:,.0f}**")
    out.append(fmt(r))
    out.append("")

out.append("---\n")
out.append("## 13. Largest International Awards (top 25)\n")
for value, r in international_large:
    out.append(f"**${value:,.0f}** | Country: {r['country']}")
    out.append(fmt(r))
    out.append("")

out.append("---\n")
out.append(f"## 14. Same-Day Batch Dumps (50+ awards, same dept, same date — top 30)\n")
out.append("| Dept | Date | Count |")
out.append("|---|---|---|")
for dept, date, count in batch_dumps:
    out.append(f"| {dept} | {date} | {count:,} |")

out.append(f"\n---\n*Generated by Canadian Nonprofit Data project — github.com/mlenczner/CanadianNonprofitData*")

outfile = "docs/weird-grants.md"
with open(outfile, "w", encoding="utf-8") as f:
    f.write("\n".join(out))

print(f"Report written to: {outfile}")
