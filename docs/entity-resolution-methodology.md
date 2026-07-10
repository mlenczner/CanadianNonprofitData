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

## Results from the first run

| Metric | Value |
|---|---|
| Entities resolved | 440,044 (80,452 charities, 120 federal departments, 1 Canada Council, 359,471 `other_org` residuals) |
| Grant/gift records linked | 1,592,451 |
| Match method (all sources combined) | exact BN 460,600 · fuzzy accept 32,851 · unmatched/residual 1,099,000 |
| `t3010_qualified_donee` exact-BN rate | 87.0% (304,416 / 349,846) — expected to be high since donees are by definition qualified donees |
| `federal_gc` exact+fuzzy rate | 14.9% of all recipient types (only N/A/S Canadian recipients get a fuzzy attempt at all, by design) |
| Entities classified `primarily_recipient` / `primarily_funder` / `dual_role` | 397,355 / 13,186 / 10,086 |
| Total dollar value linked | federal_gc $952.2B · t3010_qualified_donee $13.7B · canada_council $2.5B · t3010_non_qualified_donee $0.84B |

The federal_gc total ($952.2B) is close to but below the $972B headline figure in [`why-this-matters.md`](why-this-matters.md) — the ~$20B gap is fully explained by the 139,545 `grants.csv` records (10.7%) whose `ref_number` has no department-code prefix and are therefore excluded from `grants_unified` entirely (no funder entity to attach them to). This is a real gap, not a rounding difference — see limitations below.

The top `dual_role` entities by total flow are exactly the kind of organization this was meant to surface: Community Foundations of Canada, United Way of Canada, Aga Khan Foundation Canada, the Salvation Army, Calgary Homeless Foundation, UHN Foundation, United Way of Greater Toronto, Food Banks Canada, Jewish Community Foundation of Montreal — all known regranting intermediaries that receive government/donor funding and redistribute it to member agencies or smaller nonprofits.

Manual review of fuzzy matches turned up mostly correct near-misses (abbreviations, punctuation, accents, minor word-order/legal-suffix differences — e.g. `THE UNIVERSITY OF NEW BRUNSWICK (UNB)` → `UNIVERSITY OF NEW BRUNSWICK`, `Halifax Gay Mens Chorus` → `HALIFAX GAY MEN'S CHORUS SOCIETY`) but also **one confirmed false positive**: `ALBERTA CIRCUIT 5A OF JEHOVAH'S WITNESSES` matched to `Alberta Circuit 7A of Jehovah's Witnesses` at a 97.4 score — the branch number differs, so these are almost certainly different organizations, but `token_sort_ratio` barely penalizes a one-character/one-digit difference in an otherwise-long, otherwise-identical string. This is a real, observed failure mode, not a hypothetical one: **fuzzy matching is unreliable for organizations that differ only by a trailing number or code** (branch/circuit/chapter/district numbers, school division numbers). A future pass should either boost the weight of numeric tokens in scoring or require exact match on any numeric token before accepting.

## Known limitations

- **Single-year T3010 (2023).** A charity that deregistered before or registered after 2023 won't be in the registry snapshot, so grants to/from it will fall through to fuzzy or residual matching even though CRA has (or had) a BN on file for it.
- **No "needs review" tier.** Matches scoring 80–89 are currently discarded as unmatched rather than queued for manual review, per the plan's original design — this trades recall for not silently asserting a shaky match. Revisit if match rate turns out too low.
- **Blocking can miss reordered names.** Blocking by first-4-characters-of-normalized-name will miss a genuine match where word order differs (e.g. "Toronto Humane Society" vs "Humane Society of Toronto") if the first token differs after suffix-stripping.
- **Canada Council's Business Number field is unreliable**, as noted above — real BNs are mixed with internal IDs, and there's no field-level way to tell them apart except by shape.
- **Fuzzy matching is restricted to Canadian, nonprofit-shaped records.** For-profits, government recipients, and international recipients never get a fuzzy pass against the charity registry, by design — matching them would produce false positives with no real basis.
- **`t3010_non_qualified_donee` records never have a BN**, only a name — these grantees are frequently unregistered community groups or foreign organizations, so a meaningfully higher share stay in `other_org` residual entities than for the other sources.
- **139,545 `grants.csv` records (10.7%) are silently excluded from `grants_unified`** because their `ref_number` has no `-` separator, so no department code (and therefore no funder entity) can be derived, mirroring the same defensive check `profile_grants.py` already applies to its department breakdown. This accounts for the ~$20B gap between this pipeline's federal_gc total ($952.2B) and the $972B headline figure elsewhere in the docs.
- **Numeric-suffix false positives.** Observed directly in this run: `token_sort_ratio` scored `ALBERTA CIRCUIT 5A OF JEHOVAH'S WITNESSES` vs `Alberta Circuit 7A of Jehovah's Witnesses` at 97.4 despite the branch number differing — organizations that differ only in a trailing number/code are at real risk of false-positive matches. Not yet mitigated.

## Verification performed

- Row counts checked against each source file's line count after load.
- Match-method breakdown (exact/fuzzy/unmatched) reported per source dataset.
- 20 random `fuzzy_accept` matches printed with their scores for manual eyeballing.
- Top dual-role entities by total flow inspected to confirm the classification surfaces real regranting organizations rather than data noise.
