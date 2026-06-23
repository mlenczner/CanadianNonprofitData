"""
GC Grants & Contributions — Weird Grants Finder
Hunts for unusual, suspicious, or interesting records.
Run with: python3 analysis/weird_grants.py grants.csv
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

def parse_money(val):
    try:
        return float(str(val).replace(",", "").replace("$", "").strip())
    except Exception:
        return None

def years_between(d1, d2):
    return (d2 - d1).days / 365.25

print(f"Reading {FILE}...", flush=True)

total = 0

# Weird categories
biggest_awards = []             # top 25 by value
most_amended = []               # top 25 by amendment number
longest_agreements = []         # top 25 by duration (years)
tiny_awards = []                # awards under $100 with a description
big_foreign_currency = []       # large foreign currency awards
weird_recipients = []           # recipients with unusual names or patterns
one_dollar_awards = []          # exactly $1
massive_descriptions = []       # descriptions over 2000 chars (unusually detailed)
single_char_descriptions = []   # descriptions that are 1-5 chars
negative_big = []               # large negative values
very_old_agreements = []        # agreements running 20+ years
future_end_dates = []           # end dates far in future (2040+)
same_day_many = defaultdict(list)  # dept+date -> list of records (potential batch dumps)
high_value_no_desc = []         # large awards with no or tiny description
indigenous_large = []           # large Indigenous recipient awards
international_large = []        # large international awards
amendment_value_changes = defaultdict(list)  # ref base -> list of amendment values

# For same-day batch detection
dept_date_counter = defaultdict(int)

with open(FILE, newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        total += 1
        if total % 200_000 == 0:
            print(f"  ... {total:,} rows", flush=True)

        ref = row.get("ref_number", "").strip()
        dept = row.get("owner_org", "").strip()
        dept_title = row.get("owner_org_title", "").strip()
        recipient = row.get("recipient_legal_name", "").strip()
        recipient_op = row.get("recipient_operating_name", "").strip()
        country = row.get("recipient_country", "").strip()
        province = row.get("recipient_province", "").strip()
        city = row.get("recipient_city", "").strip()
        desc_en = row.get("description_en", "").strip()
        agreement_type = row.get("agreement_type", "").strip()
        recipient_type = row.get("recipient_type", "").strip()
        foreign_currency = row.get("foreign_currency_type", "").strip()
        prog_name = row.get("prog_name_en", "").strip()

        value = parse_money(row.get("agreement_value", ""))
        amendment_num = row.get("amendment_number", "").strip()
        start_date = parse_date(row.get("agreement_start_date", ""))
        end_date = parse_date(row.get("agreement_end_date", ""))
        foreign_value = parse_money(row.get("foreign_currency_value", ""))

        try:
            amend_int = int(amendment_num) if amendment_num else 0
        except Exception:
            amend_int = 0

        record = {
            "ref": ref,
            "dept": dept_title or dept,
            "recipient": recipient,
            "recipient_op": recipient_op,
            "country": country,
            "province": province,
            "city": city,
            "value": value,
            "desc": desc_en,
            "type": agreement_type,
            "recipient_type": recipient_type,
            "amendment": amend_int,
            "start": start_date,
            "end": end_date,
            "foreign_currency": foreign_currency,
            "foreign_value": foreign_value,
            "prog_name": prog_name,
        }

        if value is None:
            continue

        # Biggest awards (original records only)
        if amend_int == 0:
            biggest_awards.append((value, record))
            biggest_awards.sort(key=lambda x: x[0], reverse=True)
            biggest_awards = biggest_awards[:25]

        # Most amended
        if amend_int >= 10:
            most_amended.append((amend_int, record))
            most_amended.sort(key=lambda x: x[0], reverse=True)
            most_amended = most_amended[:25]

        # Longest running agreements
        if start_date and end_date and end_date > start_date:
            duration = years_between(start_date, end_date)
            if duration >= 10:
                longest_agreements.append((duration, record))
                longest_agreements.sort(key=lambda x: x[0], reverse=True)
                longest_agreements = longest_agreements[:25]

        # Tiny awards (under $100, with description, original)
        if 0 < value < 100 and desc_en and amend_int == 0:
            tiny_awards.append((value, record))
            if len(tiny_awards) > 50:
                tiny_awards.sort(key=lambda x: x[0])
                tiny_awards = tiny_awards[:50]

        # Exactly $1
        if value == 1.0 and amend_int == 0:
            one_dollar_awards.append(record)

        # Large negative values
        if value < -1_000_000:
            negative_big.append((value, record))
            negative_big.sort(key=lambda x: x[0])
            negative_big = negative_big[:25]

        # Big foreign currency
        if foreign_currency and foreign_value and foreign_value > 1_000_000:
            big_foreign_currency.append((foreign_value, record))
            big_foreign_currency.sort(key=lambda x: x[0], reverse=True)
            big_foreign_currency = big_foreign_currency[:25]

        # Massive descriptions
        if len(desc_en) > 2000:
            massive_descriptions.append((len(desc_en), record))
            massive_descriptions.sort(key=lambda x: x[0], reverse=True)
            massive_descriptions = massive_descriptions[:15]

        # Single char descriptions
        if 0 < len(desc_en) <= 5:
            single_char_descriptions.append(record)

        # Far future end dates
        if end_date and end_date.year >= 2040:
            future_end_dates.append((end_date, record))
            future_end_dates.sort(key=lambda x: x[0], reverse=True)
            future_end_dates = future_end_dates[:20]

        # High value, no or tiny description (over $10M, desc under 20 chars)
        if value > 10_000_000 and len(desc_en) < 20 and amend_int == 0:
            high_value_no_desc.append((value, record))
            high_value_no_desc.sort(key=lambda x: x[0], reverse=True)
            high_value_no_desc = high_value_no_desc[:25]

        # Large Indigenous awards
        if recipient_type == "A" and value > 5_000_000 and amend_int == 0:
            indigenous_large.append((value, record))
            indigenous_large.sort(key=lambda x: x[0], reverse=True)
            indigenous_large = indigenous_large[:20]

        # Large international awards
        if country not in ("CA", "ca", "") and value > 1_000_000 and amend_int == 0:
            international_large.append((value, record))
            international_large.sort(key=lambda x: x[0], reverse=True)
            international_large = international_large[:25]

        # Same-day batch detection
        if start_date and amend_int == 0:
            key = (dept, start_date.strftime("%Y-%m-%d"))
            dept_date_counter[key] += 1

# Find same-day batch dumps (50+ awards same dept same day)
batch_dumps = [(k, v) for k, v in dept_date_counter.items() if v >= 50]
batch_dumps.sort(key=lambda x: x[1], reverse=True)

print(f"\nTotal rows processed: {total:,}\n")

# ── OUTPUT ───────────────────────────────────────────────────────────────────
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
for value, r in sorted(tiny_awards, key=lambda x: x[0])[:25]:
    out.append(f"**${value:.2f}**")
    out.append(fmt(r))
    out.append("")

out.append("---\n")
out.append(f"## 5. Exactly $1 Awards ({len(one_dollar_awards)} total, showing first 20)\n")
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
out.append(f"## 9. Suspiciously Short Descriptions (1-5 chars, {len(single_char_descriptions)} total, showing first 30)\n")
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
for (dept, date), count in batch_dumps[:30]:
    out.append(f"| {dept} | {date} | {count:,} |")

out.append(f"\n---\n*Generated by Canadian Nonprofit Data project — github.com/mlenczner/CanadianNonprofitData*")

outfile = "docs/weird-grants.md"
with open(outfile, "w", encoding="utf-8") as f:
    f.write("\n".join(out))

print(f"Report written to: {outfile}")
