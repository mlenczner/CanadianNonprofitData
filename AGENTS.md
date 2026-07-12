# Canadian Nonprofit Data

Links federal Grants & Contributions (`grants.csv`), the CRA T3010 charity registry (`data/t3010/`), and Canada Council for the Arts grants (`data/canada_council_grants.csv`) into one entity graph in `nonprofit_network.duckdb`, built by `analysis/build_entity_graph.py`. See `docs/entity-resolution-methodology.md` for the matching approach and match-rate results.

## Setup & commands

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # requirements.txt (runtime) + pytest (dev/test)
```

Build the entity graph (fetches T3010 + Canada Council data first, then links everything):

```bash
.venv/bin/python analysis/download_sources.py
.venv/bin/python analysis/build_entity_graph.py
```

Run tests:

```bash
.venv/bin/python -m pytest tests/
```

## Rule: never read the raw data files directly

`grants.csv` (~2GB), `nonprofit_network.duckdb`/`.wal`, and everything under `data/t3010/` (48 files, 12 years × 4 kinds) are large. **Never read them directly — always query via DuckDB aggregates**, e.g.:

```bash
.venv/bin/python -c "import duckdb; con = duckdb.connect('nonprofit_network.duckdb', read_only=True); print(con.execute('SELECT ... LIMIT 20').fetchall())"
```

## Table schemas (`nonprofit_network.duckdb`)

**`entities`** — one row per resolved organization
- `entity_id` INTEGER, `bn_root` VARCHAR (9-digit CRA business number root, nullable), `canonical_name` VARCHAR, `city` VARCHAR, `province` VARCHAR, `entity_kind` VARCHAR (`charity` / `federal_dept` / `funder_org` / `other_org`)

**`entity_links`** — audit trail of every match decision
- `entity_id` INTEGER, `source_dataset` VARCHAR (`federal_gc` / `canada_council` / `t3010_qualified_donee` / `t3010_non_qualified_donee`), `raw_name` VARCHAR, `raw_bn` VARCHAR, `match_method` VARCHAR (`exact_bn` / `fuzzy_accept` / `unmatched_new`), `match_score` DOUBLE (nullable)

**`grants_unified`** — one row per grant/gift from any source
- `grant_id` INTEGER, `source_dataset` VARCHAR, `funder_entity_id` INTEGER, `recipient_entity_id` INTEGER, `amount_cad` DOUBLE, `fiscal_year` INTEGER, `program_name` VARCHAR, `description` VARCHAR (nullable)

**`entity_role_summary`** — per-entity give/receive totals and role
- `entity_id` INTEGER, `canonical_name` VARCHAR, `entity_kind` VARCHAR, `total_given` DOUBLE, `total_received` DOUBLE, `n_grants_given` INTEGER, `n_grants_received` INTEGER, `given_share` DOUBLE (nullable), `role` VARCHAR (`primarily_funder` given_share≥0.9 / `primarily_recipient` given_share≤0.1 / `dual_role` / `no_flows`)

**`entity_financials`** — T3010 line-code data joined to entities by BN root
- `entity_id` INTEGER, `bn_full` VARCHAR, `fiscal_period_end` DATE, `total_revenue` DOUBLE (line 4700), `total_expenditures` DOUBLE (4950), `total_expenditures_incl_disbursements` DOUBLE (5100), `total_gifts_to_qualified_donees` DOUBLE (5050), `revenue_from_federal_gov` DOUBLE (4540), `revenue_from_any_cdn_gov` DOUBLE (4570)
- One row per entity: `build_entity_financials` dedups `raw_t3010_fin` to the latest `source_year` per `bn_root` before joining.

## open.canada.ca WAF workaround

`open.canada.ca` rejects `urllib`'s default request signature but accepts `curl`'s. Both `analysis/download_sources.py`'s CSV downloads and its CKAN API calls (`package_search`/`package_show`) shell out to `curl -sL` rather than using `urllib.request` or `requests`. If adding new downloads from this domain, follow the same pattern.

## Open issues

1. **Silent row drops from `ignore_errors=true`, partially addressed — investigated further, root cause is two unrelated problems, no safe fix found yet.** The multi-year T3010 loads in `load_raw` (`identification_*.csv`, `qualified_donees_*.csv`, `non_qualified_donees_*.csv`, `financials_*.csv`) all use `ignore_errors=true` across 48 files spanning a form whose columns changed over the years (e.g. line codes 5045/5840-5843 only exist from 2023 onward). `_load_t3010_table` scans each file individually with `store_rejects=true` first and prints a per-file reject count (DuckDB doesn't allow `store_rejects` together with `union_by_name`, which the real load needs, hence the separate pass). This surfaced ~20k dropped rows in `financials_2013`-`2017` and ~1,444 in `qualified_donees_2014/2017/2018` — but a follow-up investigation found the original "non-UTF-8 bytes" explanation only covers **half** of these, and the "obvious" fixes don't actually work:
   - **`financials_2013`-`2016` (confirmed genuine encoding issue).** These files are not UTF-8 at all — every accented French character is single-byte Latin-1/CP1252 (`\xe9`="é", `\xe0`="à", confirmed byte-for-byte; zero legitimate multi-byte UTF-8 sequences exist anywhere in these files). But DuckDB's own `read_csv(..., encoding='latin-1')` parameter, reproduced cleanly across fresh connections, returns the **exact same row count** as the current broken UTF-8 load — zero rows recovered — even though a plain Python `bytes.decode('latin-1')` correctly recovers every affected line. Why DuckDB's `encoding=` flag doesn't do the transcode a plain per-line Python decode does is unresolved; a Python pre-processing pass (read raw bytes, decode with a fallback chain, write a cleaned UTF-8 file, then let DuckDB read that) is the more promising path, not yet implemented or verified.
   - **`financials_2017` and all three `qualified_donees_2014/2017/2018` files (different root cause — NOT encoding).** Every line in these files decodes as valid UTF-8. DuckDB's actual reject reason is `TOO MANY COLUMNS` (confirmed via `store_rejects`) — a CSV quote/dialect-detection problem (e.g. `"Morningstar Mission, Napanee, ON"` not being recognized as one quoted field in some rows). `financials_2017` fails DuckDB's `sniff_csv` dialect auto-detection outright. Two candidate fixes were tested and **neither is safe to apply uniformly**: forcing `quote='"', escape='"'` recovered +381/+317 rows on `qualified_donees_2017`/`2018` but **lost 51,148 rows on `financials_2017`** (worse than doing nothing); DuckDB's own suggested `strict_mode=false` recovered +7,504 rows on `financials_2017` but **reduced row counts on both `qualified_donees_2017` and `2018`** (301,889 vs baseline 302,901, and 296,339 vs 298,472) — meaning it's silently changing which rows are treated as valid, not simply recovering the ones currently rejected, plausibly via null-padding misaligning columns rather than genuinely fixing them. No single setting fixes all four files without a regression somewhere.
   
   Not yet addressed: a per-file fix (Python pre-decode for the encoding files; targeted, row-level-verified dialect handling for the quote-mismatch files) rather than one blanket setting. Given this data feeds dollar-value calculations, a fix should not be applied without row-level verification that it doesn't silently misalign columns — the `strict_mode=false` result above is exactly that failure mode observed directly, not a hypothetical risk.

2. **Numeric-suffix fuzzy-match false positive — fixed, with one confirmed small residual gap.** `ALBERTA CIRCUIT 5A OF JEHOVAH'S WITNESSES` was matching `Alberta Circuit 7A of Jehovah's Witnesses` at a 97.4 `token_sort_ratio` score — branch/circuit/chapter/district numbers barely affected the score in an otherwise-long, otherwise-identical name. Fixed by a digit-token gate: `Resolver.resolve()` now requires a candidate's digit-bearing tokens (`digit_tokens()`, whitespace-split so `5A` stays distinct from `5B`/`7A` — a `\d+` regex would collapse it to `5` and reintroduce the bug) to exactly match the incoming record's before scoring; the existing `FUZZY_ACCEPT`/`FUZZY_REVIEW` thresholds are unchanged and apply on top. A follow-up on the first full run found two false-reject patterns (8.6%/13.9% of that run's 1,492 gate rejects), both fixed with `_fuse_digit_letter_tokens()` (joins a split digit+single-letter suffix, `1-B`→`1B`, back together before comparison) and `digit_tokens_match()` (ignores a year-like token, `YEAR_RE` 1800-2099, when only one side carries one, but keeps it as a differentiator if both sides carry a *differing* year).

   On the latest full run (12-year T3010, both refinements in place): **1,164 gate rejects**, and a full regression scan (recomputing pre-refinement digit-token equality against every one of them) found **3 of 1,164 (0.6%)** where the fusion refinement itself newly rejects a pair the simpler pre-refinement logic would have matched — manually reviewed, not just counted. 1 is a confirmed genuine regression and a new failure mode: a T3010 donee-name field truncated mid-word right after a digit (`"...Saskatoon School Division No. 13 T"`, cut off before `TRUST FUND`) gets its truncated single letter fused onto the digit (`"13"`+`"T"`→`"13T"`), no longer matching the untruncated registry name's bare `"13"` — confirmed recurring (multiple donee records for the same org truncated at exactly 60 chars), not a one-off, so it could plausibly affect other long organization names too. Not yet fixed. The other 2 are genuinely ambiguous (JW circuit `1`/`1-B`, `2`/`2-A` pairs) rather than confirmed regressions, since this same dataset has confirmed-different lettered JW sub-circuits elsewhere (`5A`/`7A`, `11B`/`1B`).

   Still not addressed: a third, unquantified pattern where a branding/campaign number appears in a common name but not the registered legal name (e.g. `Times Colonist 1000 Christmas Fund` vs `Times Colonist Christmas Fund Society`) — no safe general rule was found for this one. Full writeup with real numbers: `docs/entity-resolution-methodology.md`.

3. **Amendment double-counting and BN-residual entity splitting — fixed, verified against the real rebuild. Two related gaps found in verification, both flagged rather than chased further.** `grants_unified` was built from every `raw_grants` row with no amendment dedup, inflating `federal_gc`'s total by roughly 1.7x (reported $952.2B, corrected/verified $831.2B) and `Resolver`'s residual branch never registered a found `bn_root` in `bn_to_entity` (splitting one real organization into many entities — confirmed: 18,139 BN roots mapped to multiple entities, e.g. Prince Rupert Port Authority existed as 6 entities sharing one BN). Both fixed: `_latest_amendment_sql()` dedupes to the latest amendment per `(owner_org, ref_number)` before anything reads grants values (`ref_number` alone collides across departments — 24,851 refs, e.g. `GC-2016-Q4-00001` is six different grants from six different departments — so the dedup key must include `owner_org` or genuinely distinct agreements get silently discarded); the residual branch now registers/backfills `bn_root` into `bn_to_entity`, explicitly refusing to merge two different BNs that share a normalized name+province. `normalize_name()` also now splits bilingual pipe-formatted names (`"English Name|Nom français"`) on `|` and keeps only the English half, so both language variants collapse to one entity. Post-rebuild verification: duplicate-BN entity count 18,139 → 0; `federal_gc` row count in `grants_unified` matches distinct `(owner_org, ref_number)` pairs with a dept prefix exactly (1,086,085 = 1,086,085); `federal_gc` total matches an independently-computed target exactly. Regression tests: `tests/test_bn_residual_and_amendment_dedup.py` (11 cases, CI-enforced), including the exact ref-collision scenario that caught a wrong ref-only dedup key before it was ever run against real data.

   **Gap found during verification, not fixed:** re-checking Prince Rupert Port Authority specifically still shows 2 entities, not 1. Traced to a narrower, pre-existing limitation, not a bug in the fix above: 3 of its ~20 raw records have neither a valid BN nor a filled-in province, and residual dedup keys on `(normalized name, province)` — those specific rows can't be reconciled with the BN-carrying, BC-province cluster by either mechanism. Not investigated further.

   **Also found during verification, same family as the gap above:** both Prince Rupert entities' `canonical_name` still stores the raw pipe-formatted bilingual string (e.g. `"Prince Rupert Port Authority|Administration portuaire de Prince Rupert"`) even though `normalize_name()` strips the pipe for matching — normalization is applied to the match key, not to the value stored as `canonical_name`. And the underlying pattern is far bigger than one port authority: The Salvation Army's BN-anchored national entity (entity 6930, `bn_root` 107951618, $3.7B flow — the org-page sample) is only the core of a much larger fragmented picture. Verified: at least 32 additional `other_org` entities with no BN and clearly Salvation-Army divisional/territorial/headquarters naming (e.g. `"THE SALVATION ARMY ONTARIO DIVISION"`, `"THE SALVATION ARMY - MARITIME DIVISIONAL HEADQUARTERS"`) span at least 6 provinces (AB, MB, NL, NS, ON, QC) plus several with no province recorded — the same no-BN / no-fuzzy-match / province-blocked gap as Prince Rupert, just at a much larger scale for an organization this size. A broader substring search for any entity whose name contains "salvation army" turns up 839 entities total, though most of those (individual corps, thrift stores, food banks, Christmas hamper programs) are plausibly distinct program-level records rather than the same legal entity, so the true fragmentation count depends on where that line is drawn — the 32 figure above is the narrower, more defensible one (divisional/territorial/headquarters naming only). Not investigated further or fixed; next-rebuild territory, same as the gap above.

   **Separate, unfixed, flagged rather than expanded into this change:** the pipeline attributes a grant's funder department via `split_part(ref_number, '-', 1)` (the ref's prefix) — but the same 24,851 colliding-ref groups above share that prefix while `owner_org` genuinely differs between them, so those records likely get attributed to whichever department's row happened to win an arbitrary tiebreak, not necessarily the correct one. `owner_org` is probably the more reliable funder key than the ref-number prefix. Not investigated or fixed.

   **Severity found while building the org-page feature (issue #4 below):** this is worse than "some collision groups" suggests. Of 120 `federal_dept` entities in `entities`, 97 (81%) have a `canonical_name` that's just a ref-number fragment (`"014"`, `"200607Q2"`, `"Q4"`) rather than a real department name — only entities like `Transport Canada` came through intact. Any feature that displays a grant's funder by name (org profile pages, dashboards) will show these codes verbatim for the large majority of federal_gc grants. Confirmed via `SELECT canonical_name FROM entities WHERE entity_kind='federal_dept'`.

   **Also flagged, not this pipeline's scope:** the $972B headline figure in `docs/why-this-matters.md` sums amendment rows the same way `grants_unified` used to before this fix, so it's very likely also inflated by a similar factor and needs independent revisiting — not assumed correct just because this pipeline's own number is now fixed.

4. **`grants_unified` doesn't store `ref_number`, so per-grant receipts require a runtime best-effort join.** `analysis/org_page.py`'s federal-grant receipt drawer locates the underlying `raw_grants` row by joining through `entity_links.raw_name` variants against `recipient_legal_name`, then matching on amount + fiscal year — inherently ambiguous when multiple raw rows share those (drawer says "not located" rather than guessing). A `source_ref` column on `grants_unified` (populated at build time, when the source row is still at hand) would make this exact instead of best-effort. Not implemented — would require a rebuild; suggested for the next one, not done as part of the org-page feature (which was explicitly scoped to not touch `build_entity_graph.py` or trigger a rebuild).

## Organization Profile Pages (self-contained HTML, no dependencies)

`analysis/org_page.py` generates one profile page per organization from `nonprofit_network.duckdb` — a "claim and receipt" design: a clean summary by default (name, stats, funding timeline, grants received/given), with a "Show your work" toggle that reveals every claim's underlying evidence (raw name variants from `entity_links`, amendment chains from `raw_grants`, match scores) in click-to-open drawers. See `docs/org-page-spec.md` for the full spec and its "Decisions" note for choices made where the spec didn't specify. Large regranters (received/given lists, and identity-receipt name-variant lists) are capped at 300 entries by volume, with a rollup note for the rest.

```bash
.venv/bin/python analysis/org_page.py "Salvation Army"      # fuzzy name lookup -> docs/orgs/<slug>.html
.venv/bin/python analysis/org_page.py --entity-id 12345
.venv/bin/python analysis/org_page.py --bn 107951618
```

Three committed samples under `docs/orgs/`: The Salvation Army (large regranter, both directions), TYS Theatre Yes Society (small single-source charity), Prince Rupert Port Authority (the BN-residual-splitting case from issue #3 above — its identity receipt shows the raw name variants now merged into one entity).

## Dashboards (self-contained HTML, no dependencies)

Two single-file HTML reports live in `docs/`, each rebuilt from `grants.csv` by a script in `analysis/` (~40s each; both apply latest-amendment-per-(owner_org, ref_number) dedup, consistent with `build_entity_graph.py`):

- `docs/grants-dashboard.html` ← `analysis/build_dashboard.py` — headline totals, per-department data-quality grades, fiscal-year funding chart, largest agreements, curiosities ($1 grants, negative values, Excel-null dates).
- `docs/data-quality-rankings.html` ← `analysis/build_quality_report.py` — departments ranked best-to-worst on publishing quality with expandable per-department evidence (real refs) and a "specimen jar" of egregious records. Scoring weights are documented in the page footer and are editorial judgment, not TBS policy.

Rebuild: `python3 analysis/build_dashboard.py grants.csv docs/grants-dashboard.html` (same pattern for the other).
