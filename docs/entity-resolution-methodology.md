# Entity Resolution Methodology

**Project:** Canadian Nonprofit Data
**Script:** [`analysis/build_entity_graph.py`](../analysis/build_entity_graph.py)
**Status:** Iterated past the first pass — multi-year T3010 (2013–2024), a digit-token gate for numeric-suffix false positives (plus two refinements), and a documented-but-unresolved reject-row data-loss gap. Thresholds and scope below can still be revisited.

---

## Goal

Federal G&C, the CRA T3010 charity registry, and Canada Council for the Arts grants each name organizations independently, with no shared key linking them (only 44.7% of `grants.csv` records carry a business number). This pipeline links all three into one entity graph so a single organization — whichever name variant it appears under — is recognized consistently as a funder and/or recipient across all sources. This is what makes it possible to answer "how much did org X receive in total" or "which organizations both receive government funding and regrant it to others."

## Sources linked

| Source | File | Role |
|---|---|---|
| Federal Grants & Contributions | `grants.csv` | funder = federal department, recipient = org |
| CRA T3010 charity registry (2013–2024) | `data/t3010/identification_*.csv` | anchor registry: BN ↔ legal name ↔ address, latest filing year per BN wins |
| CRA T3010 Qualified Donees schedule (2013–2024) | `data/t3010/qualified_donees_*.csv` | funder = charity, recipient = another qualified donee (often another charity) |
| CRA T3010 Non-Qualified Donees schedule (2023–2024 only) | `data/t3010/non_qualified_donees_*.csv` | funder = charity, recipient = non-charity grantee (name only, no BN) |
| Canada Council for the Arts (2017–2025) | `data/canada_council_grants.csv` | funder = Canada Council, recipient = org/individual |

T3010 now spans **12 years (2013–2024)**, not just the most recent filing year — this was a known gap in the first pass, since resolved (see multi-year ingestion in `_load_t3010_table`). Each year is a separate set of CSVs per schedule, unioned by column name since the form's columns changed shape over time (e.g. line codes 5045/5840-5843 only exist from 2023 onward); `identification_*.csv` seeds one charity entity per BN using the **latest** filing year, so charities that deregistered before 2024 still get a registry entry.

## Matching approach

Applied in order, cheapest and most reliable first:

1. **Normalize.** Names: transliterate accents, uppercase, strip punctuation, drop legal-form stopwords (Inc./Ltd./Society/Foundation/Association and French equivalents) and common connector words (of/the/and/de/du/la). Business numbers: reduce to the 9-digit root, since one legal entity can hold multiple CRA program accounts (`870814944RR0001` → `870814944`). Note: Canada Council's "Business Number" column is not reliably a CRA BN — it contains a mix of real BNs, corporate program accounts, and what look like internal Canada Council client IDs (`429917-5`, `1167722652`, `S-50887`). Only strings matching a plausible 9-digit-root pattern are treated as BNs; everything else falls through to name matching.

2. **Exact BN match.** Any record with a normalized BN matching a T3010 charity's root BN is linked immediately — no ambiguity.

3. **Fuzzy name match**, only attempted for records that look like they could be nonprofits/charities (grants.csv recipient types `N`/`A`/`S` restricted to Canadian recipients; Canada Council type `Organization`; all T3010 donee records, since those are org-to-org by definition). Candidates are blocked by `(province, first 4 normalized-name characters)` to keep comparisons tractable, then scored with `rapidfuzz.token_sort_ratio` against T3010 charity names in that block:
   - **≥ 90** → accepted, *but only if it also passes the digit-token gate below*
   - **< 90** → treated as unmatched (no "needs review" queue in this pass — see limitations)

   **Digit-token gate.** A confirmed false positive (`ALBERTA CIRCUIT 5A OF JEHOVAH'S WITNESSES` matching `Alberta Circuit 7A of Jehovah's Witnesses` at 97.4 — see Results below) showed that `token_sort_ratio` barely penalizes a differing branch/circuit/chapter/district number in an otherwise-long, otherwise-identical name. `Resolver.resolve()` now requires a candidate's digit-bearing tokens (`digit_tokens()`) to match the incoming record's before a fuzzy match is scored at all — `5A` and `7A` never even reach the `token_sort_ratio` comparison. Two refinements on top of the base gate, both confirmed necessary against real false-reject patterns:
   - **Digit-letter fusion** (`_fuse_digit_letter_tokens`): joins a standalone single-letter token onto an immediately-preceding digit token (`"1-B"` / `"1 B"` → `"1B"`), so the same branch number written differently across sources (hyphenated vs. fused) isn't wrongly split apart. Deliberately whitespace-based, not a `\d+` regex — a regex would collapse `"5A"` to `"5"` and fail to distinguish it from `"5B"`/`"7A"`, reintroducing the original bug.
   - **Year tolerance** (`digit_tokens_match`): a 4-digit token in the 1800–2099 range is treated as an incidental incorporation/founding year (e.g. `"Soup Kitchen Association 2013"`) and ignored when only one side of the comparison has one — but kept as a differentiator when *both* sides carry a differing year, so two same-named orgs distinguished only by year aren't merged.

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
- **`entity_financials`** — one row per entity: T3010 line-code data for its latest filed fiscal year, joined to `entities` by BN root: total revenue (line 4700), total expenditures (4950/5100), total gifts to qualified donees (5050), revenue from federal/any Canadian government (4540/4570) — sourced from CRA's official [T3010 Open Data Dictionary](https://www.canadiancharitylaw.ca/wp-content/uploads/2025/02/CRA-open-data-data-dictionary-for-T3010.pdf), not guessed from field names. `raw_t3010_fin` spans 2013-2024 (up to 12 filings per entity); only the latest `source_year` per `bn_root` is kept before joining.

## Results from the latest run (12-year T3010, digit-token gate in place)

| Metric | Value |
|---|---|
| Entities resolved | 578,975 (97,072 charities, 120 federal departments, 1 Canada Council, 481,782 `other_org` residuals) |
| Grant/gift records linked | 5,003,355 |
| Match method (all sources combined) | exact BN 3,451,852 · fuzzy accept 117,890 · unmatched/residual 1,433,613 |
| Digit-token gate rejects | 1,164 candidates scored ≥90 pre-gate but were split apart by differing digit tokens — see QA sample below |
| `t3010_qualified_donee` exact-BN rate | 87.9% (3,292,321 / 3,744,650) — expected to be high since donees are by definition qualified donees |
| `federal_gc` exact+fuzzy rate | 13.6% of all recipient types (only N/A/S Canadian recipients get a fuzzy attempt at all, by design) |
| Entities classified `primarily_recipient` / `primarily_funder` / `dual_role` | 523,855 / 18,697 / 20,324 (16,099 `no_flows`) |
| Total dollar value linked | federal_gc $952.2B · t3010_qualified_donee $129.2B · canada_council $2.5B · t3010_non_qualified_donee $2.3B |

These numbers grew substantially from the first (single-year T3010) pass not because matching got looser, but because there's simply far more T3010 data now — 12 years of qualified-donee gifts instead of 1, for instance, is most of why `t3010_qualified_donee`'s linked dollar value jumped from $13.7B to $129.2B. The `federal_gc` total is essentially unchanged ($952.2B, same as the first pass) since `grants.csv` itself didn't change — it's still close to but below the $972B headline figure in [`why-this-matters.md`](why-this-matters.md); the gap is fully explained by the same 139,545 `grants.csv` records (10.7%, unchanged from the first pass) whose `ref_number` has no department-code prefix and are therefore excluded from `grants_unified` entirely — see limitations below.

The top `dual_role` entities by total flow are exactly the kind of organization this was meant to surface: the Salvation Army, United Jewish Appeal of Greater Toronto, UHN Foundation, United Way of Greater Toronto, The Hospital for Sick Children Foundation, The Princess Margaret Cancer Foundation, Jewish Community Foundation of Montreal, the Canadian Red Cross Society, Community Foundations of Canada — all known regranting intermediaries that receive government/donor funding and redistribute it to member agencies or smaller nonprofits.

### The numeric-suffix false positive — found, fixed, and refined

Manual review of fuzzy matches in the first pass turned up mostly correct near-misses (abbreviations, punctuation, accents, minor word-order/legal-suffix differences — e.g. `THE UNIVERSITY OF NEW BRUNSWICK (UNB)` → `UNIVERSITY OF NEW BRUNSWICK`, `Halifax Gay Mens Chorus` → `HALIFAX GAY MEN'S CHORUS SOCIETY`) but also **one confirmed false positive**: `ALBERTA CIRCUIT 5A OF JEHOVAH'S WITNESSES` matched `Alberta Circuit 7A of Jehovah's Witnesses` at a 97.4 score — the branch number differs, so these are almost certainly different organizations, but `token_sort_ratio` barely penalizes a one-character/one-digit difference in an otherwise-long, otherwise-identical string.

This was fixed with the digit-token gate described above. A 20-row QA sample of this run's 1,164 gate rejects (`_fuzzy_gate_rejects`, sampled in `print_report`) confirmed the gate is doing its job in the large majority of cases — correctly splitting differing Royal Canadian Legion branch numbers (`Branch 103` vs `Branch 500`), differing school divisions (`Peace Wapiti School Division No. 76` vs the un-numbered `The Peace Wapiti School Division`), and both English and French Jehovah's Witnesses circuit-number mismatches.

A follow-up regression scan (recomputing what the pre-fusion, no-year-tolerance gate would have decided for every one of the 1,164 rejects) found the digit-letter fusion refinement introduces its own narrow false-reject risk: **3 of 1,164 pairs (0.6%)** would have matched under the simpler pre-refinement logic but are now rejected. Of those:
- **1 is a confirmed genuine regression**, and a new failure mode not anticipated by the original design: a T3010 donee-name field truncated mid-word (`"Board Of Education Of The Saskatoon School Division No. 13 T"`, cut off before `TRUST FUND`) has its trailing truncated letter fused onto the preceding digit (`"13"` + `"T"` → `"13T"`), which no longer matches the untruncated registry name's bare `"13"`. Confirmed recurring, not a one-off: multiple independent donee records for this same school division are truncated at exactly 60 characters (`"...No. 13 T"`, `"...No.13 (Henry Kelsey S"`), while other organizations' names run to 100+ characters untruncated — this points to a filer/year/submission-pathway-specific truncation, not a global field limit, so it could plausibly recur for other long organization names too. Not yet fixed.
- **2 are genuinely ambiguous**, not confirmed regressions: `"Nova Scotia Circuit 1-B of Jehovah's Witnesses"` vs `"Nova Scotia Circuit 1 of Jehovah's Witnesses"`, and the French-language equivalent for Quebec circuit `2-A`/`2`. Given this same dataset shows JW circuits genuinely subdividing into distinct lettered sub-circuits elsewhere (`5A`/`7A`, `11B`/`1B` are confirmed-different organizations), whether a bare circuit number and its lettered variant are the same congregation or a parent/sub-circuit pair isn't something this pipeline can determine with confidence either way.

## Known limitations

- **No "needs review" tier.** Matches scoring 80–89 are currently discarded as unmatched rather than queued for manual review, per the plan's original design — this trades recall for not silently asserting a shaky match. Revisit if match rate turns out too low.
- **Blocking can miss reordered names.** Blocking by first-4-characters-of-normalized-name will miss a genuine match where word order differs (e.g. "Toronto Humane Society" vs "Humane Society of Toronto") if the first token differs after suffix-stripping.
- **Canada Council's Business Number field is unreliable**, as noted above — real BNs are mixed with internal IDs, and there's no field-level way to tell them apart except by shape.
- **Fuzzy matching is restricted to Canadian, nonprofit-shaped records.** For-profits, government recipients, and international recipients never get a fuzzy pass against the charity registry, by design — matching them would produce false positives with no real basis.
- **`t3010_non_qualified_donee` records never have a BN**, only a name — these grantees are frequently unregistered community groups or foreign organizations, so a meaningfully higher share stay in `other_org` residual entities than for the other sources.
- **139,545 `grants.csv` records (10.7%) are silently excluded from `grants_unified`** because their `ref_number` has no `-` separator, so no department code (and therefore no funder entity) can be derived, mirroring the same defensive check `profile_grants.py` already applies to its department breakdown. This accounts for the ~$20B gap between this pipeline's federal_gc total ($952.2B) and the $972B headline figure elsewhere in the docs.
- **Numeric-suffix false positives — fixed, with a small residual gap.** See "The numeric-suffix false positive" above for the full history. The digit-token gate and its fusion/year-tolerance refinements fix the large majority of cases; a follow-up regression scan found the fusion refinement itself introduces a narrow false-reject risk (0.6% of gate rejects in the latest run), concretely observed when a T3010 donee-name field is truncated mid-word right after a digit. Not yet fixed.
- **~20k `financials_2013`-`2017` and ~1,444 `qualified_donees_2014/2017/2018` rows are dropped** by `ignore_errors=true` during CSV load and not yet recovered — investigated in depth (two unrelated root causes: genuine Latin-1 encoding for the financials files, a CSV quote-dialect issue for the rest), but no fix was found safe enough to ship without risking silent column misalignment. See `AGENTS.md` open issue #1 for the full investigation and why the obvious fixes (forcing quote/escape, `strict_mode=false`) were rejected.

## Verification performed

- Row counts checked against each source file's line count after load.
- Match-method breakdown (exact/fuzzy/unmatched) reported per source dataset.
- 20 random `fuzzy_accept` matches printed with their scores for manual eyeballing.
- Top dual-role entities by total flow inspected to confirm the classification surfaces real regranting organizations rather than data noise.
- 20 random digit-token-gate rejects sampled per run (`_fuzzy_gate_rejects`) and manually reviewed — not just counted — to distinguish the gate correctly splitting true branch/circuit-number mismatches from it wrongly splitting genuine near-duplicates.
- A dedicated regression test suite (`tests/test_digit_token_gate.py`, 19 cases, CI-enforced) covers the original bug case, the fusion and year-tolerance refinements, and every documented legit near-miss.
- A full-dataset regression scan after the fusion/year refinements landed: recomputed pre-refinement digit-token equality against every one of the run's actual gate rejects to find cases where the refinement newly rejects something the simpler logic would have matched — surfaced 3 of 1,164 (0.6%), manually reviewed rather than just counted (see "The numeric-suffix false positive" above).
