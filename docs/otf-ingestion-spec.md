# Spec: Ingest Ontario Trillium Foundation (OTF) open grants data

Add OTF's published grants (2000–present) as a fifth source dataset in the entity graph,
following the existing `canada_council` integration pattern in `analysis/build_entity_graph.py`.

**Source file:** `OTF-Grants_since2000.csv` — currently sitting at the repo root; move it to
`data/otf_grants.csv` as part of this work (and make sure `.gitignore` covers it).
Published at https://otf.ca/document/664 (linked from https://otf.ca/open), Open Government
Licence – Ontario.

## Verified facts about the file (checked 2026-07-12, use for reconciliation)

- 32,842 rows, header row present, comma-delimited, `"` quote/escape, UTF-8. DuckDB's
  `sniff_csv` handles it with defaults — no `ignore_errors` needed. If any row is rejected,
  stop and investigate; do not blanket-suppress (see AGENTS.md open issue #1 for why).
- `Fiscal Year:Année fiscal` spans `"1999-2000"` … `"2025-2026"` (string, always `YYYY-YYYY`).
- `Amount Awarded:Montant décerné`: min $500, max $2,400,000, no nulls, no zero/negative
  values. **Gross total: $2,973.0M.**
- `Rescinded/Recovered:Révoqué/récupéré` is TRUE on **2,618 rows**;
  `Amount Rescinded/Recovered` sums to **$30.52M** across them.
- Charitable Registration Number present on **19,843 rows (60%)**; **18,727** match
  `^\d{9}` with an `RR0001`-style suffix (e.g. `848778866RR0001`). The remainder are
  malformed or free-text — validate before use.
- `Identifier:Identificateur` is **not unique**: 5 identifiers cover 11 rows. The `CIM*`
  ones are shared across genuinely different recipient organizations (collaborative
  grants) — different orgs, cities, sometimes different fiscal years. These are distinct
  grants, not duplicates.
- `Funding Org` is the same single value on all rows: `Ontario Trillium Foundation`.
- `Grant Status`: Closed 31,502 / Active 1,335 / NULL 5. All rows are approved grants
  (OTF publishes approved only); no filtering needed on this column.
- Exactly 1 row has a pipe-formatted bilingual org name — `normalize_name()` already
  handles the `English|Français` split.

## Column mapping

Headers are bilingual with embedded colons — read with `all_varchar=true` and rename
positionally or by exact header string, same as the `raw_cc` pattern.

| CSV column (English half) | Use as |
|---|---|
| `Fiscal Year:Année fiscal` | `fiscal_year` = `int(value[:4])` (start year — matches the `canada_council` convention at build_entity_graph.py:543) |
| `Organization name:Nom d'organisme` | recipient raw name |
| `Recipient Org:Charitable Registration Number: …` | recipient BN. Strip whitespace; accept only values matching `^\d{9}(RR\d{4})?$`; take the first 9 digits as `bn_root`; treat everything else as missing (count and print how many were discarded) |
| `Recipient Org:City: …` | recipient city |
| `Amount Awarded:Montant décerné` | see Amounts below |
| `Amount Rescinded/Recovered:…` | see Amounts below |
| `Grant Programme:Title:…` | `program_name` |
| `Description (English/Anglais)` | `description` |
| `Identifier:Identificateur` | keep in the raw table for audit; **never a dedup key** (see verified facts) |

**Do not use** `Recipient Org:Incorporation Number` as a BN — it's an Ontario
corporation number, a different identifier system entirely.

Recipient province: hardcode `ON`. (`Province Served` is `Ontario` on all 32,842 rows,
and OTF only grants within Ontario.)

## Amounts — DECIDED: net of rescinded

`amount_cad = awarded − COALESCE(amount_rescinded, 0)`, reflecting money that actually
flowed. Same reasoning as the amendment dedup fix (AGENTS.md issue #3): grants_unified
records flows, not announcements.

- Verify `amount_rescinded <= awarded` on every row before applying; if any row violates
  this, floor at 0 and print the rows — do not let negatives into `grants_unified`.
- Check for rows where the rescinded flag is TRUE but the rescinded amount is NULL;
  treat as 0 but print the count (these are "recovered, amount unrecorded" — a known
  unknown, worth a line in the methodology doc).
- **Expected net total: ≈ $2,942.5M** ($2,973.0M − $30.52M). Reconcile the built
  `grants_unified` sum against an independently computed figure, per repo practice.

## Entity resolution

- `source_dataset` = `"otf"` in both `entity_links` and `grants_unified`.
- **Funder:** resolve once with name `Ontario Trillium Foundation`, BN `108091091`,
  province `ON` → must land on existing entity **253071** via `exact_bn`. Assert this in
  verification (it should not create a new entity). Known pre-existing fragments 411970
  and 424951 (no-BN residuals) are out of scope — note them, don't chase them.
- **Recipients:** resolve with `bn_root` when the CRN validated, else fuzzy name +
  province `ON`, exactly like the `canada_council` recipient path (`r.resolve(...)`).
  Expect roughly 60/40 exact-BN/fuzzy split; the fuzzy 40% skews to non-charities
  (municipalities, school boards, First Nations) that will mostly land as
  `unmatched_new` `other_org` rows — that's correct behavior, not a failure.
- Record real match rates in `docs/entity-resolution-methodology.md` after the rebuild,
  same format as the existing sources.

## Also add

- `analysis/download_sources.py`: add the OTF download (https://otf.ca/document/664 →
  `data/otf_grants.csv`). Use the existing `curl -sL` pattern. Note: this URL was
  unreachable from a sandboxed environment during spec-writing; if the download fails,
  print the manual-download instruction (grab it from https://otf.ca/open) rather than
  failing the whole script.
- **Tests** (`tests/test_otf_ingestion.py`), CI-enforced like the existing suites:
  CRN validation (valid RR-suffixed, bare 9-digit, malformed, empty); fiscal-year
  parsing (`"1999-2000"` → 1999); net-amount computation incl. NULL-rescinded and the
  floor-at-zero case; collaborative-grant identifiers producing distinct grant rows.
- **Docs:** update AGENTS.md (source list, `source_dataset` enum values, row counts),
  README.md (repo structure + data section), and the methodology doc (match rates +
  the net-of-rescinded decision and its $30.5M magnitude).

## Verification (before calling it done — repo standard, see issue #3 for the bar)

1. `grants_unified` WHERE `source_dataset='otf'`: row count = 32,842 (minus any rows
   deliberately excluded — there should be none; every row is an approved grant).
2. Net sum reconciles to the independently computed ≈ $2,942.5M figure exactly.
3. Funder side: all otf rows share one `funder_entity_id` = 253071.
4. Duplicate-BN entity check still passes: no `bn_root` maps to >1 entity (the issue #3
   regression gate).
5. Match-rate summary printed and recorded: exact_bn / fuzzy_accept / unmatched_new
   counts for the otf source.
6. Spot-check 3 recipients by hand: one exact-BN charity, one fuzzy-matched charity,
   one unmatched municipality — confirm the entity linkage is sane.

## Out of scope (deliberately)

- No changes to resolver thresholds or the digit-token gate.
- No attempt to merge OTF's pre-existing no-BN fragments (411970, 424951) — same family
  as the Salvation Army / Prince Rupert gap (issue #3), next-rebuild territory.
- No `source_ref` column on `grants_unified` (issue #4) — if a rebuild happens anyway,
  it's a good moment to do issue #4 too, but decide that separately.
