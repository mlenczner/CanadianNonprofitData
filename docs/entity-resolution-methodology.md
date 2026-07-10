# Entity Resolution Methodology

**Project:** Canadian Nonprofit Data
**Script:** [`analysis/build_entity_graph.py`](../analysis/build_entity_graph.py)
**Status:** First pass — thresholds and scope are documented below and can be revisited.

---

## Goal

Federal G&C, the CRA T3010 charity registry, and Canada Council for the Arts grants each name organizations independently, with no shared key linking them (only 44.7% of `grants.csv` records carry a business number). This pipeline links all three into one entity graph so a single organization — whichever name variant it appears under — is recognized consistently as a funder and/or recipient across all sources. This is what makes it possible to answer "how much did org X receive in total" or "which organizations both receive government funding and regrant it to others."

## Sources linked

| Source | File | Role |
|---|---|---|
| Federal Grants & Contributions | `grants.csv` | funder = federal department, recipient = org |
| CRA T3010 charity registry (2023) | `data/t3010_identification.csv` | anchor registry: BN ↔ legal name ↔ address |
| CRA T3010 Qualified Donees schedule | `data/t3010_qualified_donees.csv` | funder = charity, recipient = another qualified donee (often another charity) |
| CRA T3010 Non-Qualified Donees schedule | `data/t3010_non_qualified_donees.csv` | funder = charity, recipient = non-charity grantee (name only, no BN) |
| Canada Council for the Arts (2017–2025) | `data/canada_council_grants.csv` | funder = Canada Council, recipient = org/individual |

T3010 is scoped to the **most recent year only (2023)**. Each additional year is a separate set of ~19 CSVs; multi-year T3010 would be needed for financial trend analysis but isn't required for identity resolution, which is what this pass builds. Flagged as a known gap below.

## Matching approach

Applied in order, cheapest and most reliable first:

1. **Normalize.** Names: transliterate accents, uppercase, strip punctuation, drop legal-form stopwords (Inc./Ltd./Society/Foundation/Association and French equivalents) and common connector words (of/the/and/de/du/la). Business numbers: reduce to the 9-digit root, since one legal entity can hold multiple CRA program accounts (`870814944RR0001` → `870814944`). Note: Canada Council's "Business Number" column is not reliably a CRA BN — it contains a mix of real BNs, corporate program accounts, and what look like internal Canada Council client IDs (`429917-5`, `1167722652`, `S-50887`). Only strings matching a plausible 9-digit-root pattern are treated as BNs; everything else falls through to name matching.

2. **Exact BN match.** Any record with a normalized BN matching a T3010 charity's root BN is linked immediately — no ambiguity.

3. **Fuzzy name match**, only attempted for records that look like they could be nonprofits/charities (grants.csv recipient types `N`/`A`/`S` restricted to Canadian recipients; Canada Council type `Organization`; all T3010 donee records, since those are org-to-org by definition). Candidates are blocked by `(province, first 4 normalized-name characters)` to keep comparisons tractable, then scored with `rapidfuzz.token_sort_ratio` against T3010 charity names in that block:
   - **≥ 90** → accepted automatically
   - **< 90** → treated as unmatched (no "needs review" queue in this pass — see limitations)

4. **Residual entities.** Anything still unmatched becomes its own entity, deduplicated within the unmatched pool by `(normalized name, province)` so at least identical misspellings collapse into one entity across sources.

Every resolution decision is recorded in `entity_links` (`entity_id`, `source_dataset`, `raw_name`, `raw_bn`, `match_method`, `match_score`) so any match can be audited or re-scored later without rerunning the whole pipeline.

## Schema produced

- **`entities`** — one row per resolved organization: T3010 charities, federal departments (from `ref_number` prefixes), Canada Council, and any org found only in grants/CC data. `entity_kind` ∈ `charity` / `federal_dept` / `funder_org` / `other_org`.
- **`entity_links`** — audit trail of every match decision (see above).
- **`grants_unified`** — one row per grant/gift from any source, with `funder_entity_id` and `recipient_entity_id` both pointing into `entities`.
- **`entity_role_summary`** — per entity: `total_given`, `total_received`, `given_share`, and `role`:
  - `given_share ≥ 0.9` → `primarily_funder`
  - `given_share ≤ 0.1` → `primarily_recipient`
  - otherwise → `dual_role`
  
  The 90/10 split is what operationalizes "95% of the time an org is mainly one or the other" — it's a threshold, not a law, and can be tightened or loosened per analysis.
- **`entity_financials`** — T3010 line-code data joined to `entities` by BN root: total revenue (line 4700), total expenditures (4950/5100), total gifts to qualified donees (5050), revenue from federal/any Canadian government (4540/4570) — sourced from CRA's official [T3010 Open Data Dictionary](https://www.canadiancharitylaw.ca/wp-content/uploads/2025/02/CRA-open-data-data-dictionary-for-T3010.pdf), not guessed from field names.

## Known limitations

- **Single-year T3010 (2023).** A charity that deregistered before or registered after 2023 won't be in the registry snapshot, so grants to/from it will fall through to fuzzy or residual matching even though CRA has (or had) a BN on file for it.
- **No "needs review" tier.** Matches scoring 80–89 are currently discarded as unmatched rather than queued for manual review, per the plan's original design — this trades recall for not silently asserting a shaky match. Revisit if match rate turns out too low.
- **Blocking can miss reordered names.** Blocking by first-4-characters-of-normalized-name will miss a genuine match where word order differs (e.g. "Toronto Humane Society" vs "Humane Society of Toronto") if the first token differs after suffix-stripping.
- **Canada Council's Business Number field is unreliable**, as noted above — real BNs are mixed with internal IDs, and there's no field-level way to tell them apart except by shape.
- **Fuzzy matching is restricted to Canadian, nonprofit-shaped records.** For-profits, government recipients, and international recipients never get a fuzzy pass against the charity registry, by design — matching them would produce false positives with no real basis.
- **`t3010_non_qualified_donee` records never have a BN**, only a name — these grantees are frequently unregistered community groups or foreign organizations, so a meaningfully higher share stay in `other_org` residual entities than for the other sources.

## Verification performed

- Row counts checked against each source file's line count after load.
- Match-method breakdown (exact/fuzzy/unmatched) reported per source dataset.
- 20 random `fuzzy_accept` matches printed with their scores for manual eyeballing.
- Top dual-role entities by total flow inspected to confirm the classification surfaces real regranting organizations rather than data noise.
