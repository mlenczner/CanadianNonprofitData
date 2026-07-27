# Spec: Crowding-out study & flow-through mapping

Two analyses that only became possible once the entity graph linked federal G&C, T3010,
Canada Council, and OTF into one database. Both were prototyped against the real
`nonprofit_network.duckdb` on 2026-07-19; the verified facts and raw results below come
from those prototype runs and should be used for reconciliation when implementing.

Extends `docs/questions-and-insights.md` ("Of matched charities, how does federal G&C
funding compare to their reported revenues?") from a descriptive question into two
causal/structural ones:

- **Part A — Crowding-out:** does winning a first large federal grant crowd out (or in)
  a charity's private donations? Classic public-economics question (Andreoni; Payne —
  Payne's work used Canadian data but predates open G&C disclosure; nobody has run it
  on this linked panel).
- **Part B — Flow-through:** how much federal money passes *through* charities that
  re-grant it, and can the second hop be traced to named final recipients?

---

## Verified facts (prototype runs, 2026-07-19 — use for reconciliation)

**Panel availability**
- `raw_t3010_fin` line `4500` (total tax-receipted gifts) spans `source_year` 2013–2024,
  988,911 filing rows. Dedup to one row per `(bn_root, EXTRACT(year FROM FPE))` via
  `MAX` before use (amended/duplicate filings exist).
- `entity_financials_by_year` already exists (`entity_id`, `fiscal_period_end`,
  `fiscal_year`, `total_revenue`, `gov_revenue`, `foundation_revenue`) but does **not**
  carry donations — either extend it with line 4500 or build the donations panel
  directly from `raw_t3010_fin` (prototype did the latter).
- `grants_unified` federal_gc: 1,086,085 rows, `fiscal_year` 1899–2026. Solid volume
  from 2005 onward (1,841 rows in 2005, 8k+ by 2006), so first-grant years 2016–2021
  are not badly left-censored.
- **Junk fiscal years exist**: 12 rows at 1899, 1 at 1988, 1 at 2002, 1 at 2004.
  Filter them; also worth adding to `docs/data-publishing-problems.md` if not already
  documented.

**Line 4570 trap**
- `entity_financials.revenue_from_any_cdn_gov` (line 4570) is populated for only
  5,114 of 96,825 entities. It's a detailed-schedule line — short-form filers (most
  small charities) never report it. **Do not use 4570 to identify government-funded
  charities.** Use actual receipts in `grants_unified` instead. (Short-form variants
  `4571`/`4576`/`4577` exist in `raw_t3010_fin` but were not validated in the
  prototype.)

**Prototype results — Part A** (treated = first federal grant ≥ $100k in year Y, no
prior federal grants under the same `bn_root`; windows = mean of line 4500 over
[Y−3, Y−1] vs [Y+1, Y+3], ≥2 observed years in each window, pre > 0):

| Y | treated n | treated median Δ donations | never-treated control Δ (n≈37k) |
|---|---|---|---|
| 2016 | 120 | +12.8% | — |
| 2017 | 351 | +3.0% | −2.6% |
| 2018 | 205 | +2.3% | −5.1% |
| 2019 | 178 | +8.5% | −4.9% |
| 2020 | 144 | +20.1% | −1.7% |
| 2021 | 191 | +9.7% | +4.6% |

Sign is consistently crowding-**in**, every cohort. 13,642 charities had a qualifying
first grant in 2016–2021; the filter down to 120–351 per cohort comes from requiring
line 4500 non-null in ≥2 years on both sides — worth quantifying attrition explicitly
in the real run.

**Prototype results — Part B**
- 1,414 charities both received federal G&C money (sum $11.19B all-time) and reported
  > $100k in `total_gifts_to_qualified_donees` (line 5050) in their latest filing —
  **$5.23B re-granted in that single latest year**.
- Second hop resolves to named donees: `raw_t3010_qd` has `Donee BN`, `Donee Name`,
  `Total Gifts` per gift. Spot check: Aga Khan Foundation Canada (major GAC recipient)
  → Aga Khan Foundation $51.4M, FOCUS Humanitarian Assistance $1.4M, Aga Khan Museum
  $0.6M (source_year 2024).

---

## Part A — Crowding-out study

### Design

Event study around a charity's **first large federal grant**.

- **Unit:** `bn_root` (charities only — need T3010 filings).
- **Treatment:** first fiscal year Y with federal_gc receipts ≥ $100k summed within
  the year, no federal_gc receipts in any earlier year. Restrict Y ∈ [2016, 2021] so
  ±3-year windows fit inside the 2013–2024 T3010 panel.
- **Outcome:** line 4500, total tax-receipted gifts. (Consider line 4510 —
  non-receipted gifts — as a secondary outcome; not prototyped.)
- **Naive contrast (prototyped):** median within-org % change, pre vs post window,
  against never-treated charities over the same calendar windows.
- **Real estimator (to build):** the naive contrast is confounded by selection —
  orgs that win grants are plausibly on a growth path already. Two upgrades, in order:
  1. **Matched controls:** for each treated org, match 1–5 never-treated charities on
     province, size decile (line 4700 total revenue in Y−1), and pre-period donation
     trend. Report the treated-minus-matched difference in Δ.
  2. **Org and year fixed effects** on the full panel (log donations ~ post×treated +
     org FE + year FE), if we want a regression number. Requires pulling the panel
     into Python (statsmodels/pyfixest) — DuckDB alone won't do it.
- **Robustness:** vary threshold ($50k / $250k), drop 2020–21 cohorts (COVID emergency
  funding is a different treatment), winsorize donation changes, check pre-trends
  (placebo event at Y−2).

### Known limitations (state in output, don't hide)

- Selection bias is the headline caveat; matching mitigates, doesn't eliminate.
- Fiscal-year alignment is approximate: `grants_unified.fiscal_year` is the federal FY;
  T3010 `FPE` is the charity's own fiscal period end. A grant late in federal FY Y may
  land in the charity's Y+1 filing. Prototype ignored this; the real run should either
  align on FPE month or drop Y itself from both windows (prototype already excludes Y).
- Entity resolution errors propagate: a missed BN match makes a treated org look
  never-treated (contaminates controls). Quantify by re-running controls restricted to
  charities with an `exact_bn` link in `entity_links`.

### Deliverables

- `analysis/crowding_out.py` — builds a `crowding_out_panel` table in the DuckDB
  (treated flag, Y, pre/post window stats, matched-control assignment), prints the
  cohort table, and writes `docs/crowding-out-report.md` with the results + caveats.
- Tests in `tests/test_crowding_out.py`: window logic (org with grants in 2015 is not
  "first in 2017"), dedup of multiple filings per year, junk-year filtering, and the
  ≥2-observations-per-window rule.

## Part B — Flow-through mapping

### Design

Build explicit **government → intermediary → final recipient** chains.

- **Hop 1:** `grants_unified` where `source_dataset='federal_gc'` (optionally + otf +
  canada_council) and recipient has `bn_root` — government money into a charity.
- **Intermediary flag:** charity's line 5050 > threshold (start: $100k) in a fiscal
  year overlapping its hop-1 receipts.
- **Hop 2:** `raw_t3010_qd` rows for that `bn_root` — named donees with amounts. Donee
  BNs should resolve back to `entities` via `bn_root` where possible, making the chain
  queryable end-to-end inside the graph.
- **Headline aggregates:** total $ at each hop; share of federal G&C going to
  re-granters; top intermediaries table; distribution of chain depth (some donees are
  themselves re-granters — cap traversal at 3 hops, flag cycles).
- **Honest denominator:** hop-1 receipts and hop-2 gifts are different years and the
  charity's money is fungible — we can say "org X received $A federal and re-granted
  $B", **not** "$B of federal money was re-granted". The report must phrase it as
  co-occurrence, not tracing of specific dollars. (Same "claim and receipt" discipline
  as the org pages.)

### Deliverables

- `analysis/flow_through.py` — builds `flow_through_chains` (one row per
  intermediary→donee edge with hop-1 context) + summary aggregates; writes
  `docs/flow-through-report.md`.
- Org-page integration (later, separate spec change): a "re-grants to" badge on
  intermediary org pages, reusing the existing qualified-donee receipt drawers.
- Tests in `tests/test_flow_through.py`: donee-BN resolution, year-overlap rule,
  cycle detection, and the Aga Khan spot-check numbers above as a regression fixture.

## Decisions & open questions

- **Decided (prototype):** identify government funding via `grants_unified` receipts,
  never line 4570 (see trap above).
- **Decided (prototype):** treatment threshold $100k first-year sum; sensitivity
  analysis required before any published number.
- **Open:** include provincial/OTF/CC money in "government" treatment for Part A, or
  keep it federal-only? Prototype was federal-only; adding OTF changes the
  never-treated control pool.
- **Open:** Part A estimator upgrade order — matching is cheap and DuckDB-native;
  fixed-effects regression pulls in a new dependency (pyfixest or statsmodels).
  Suggest shipping matching first, regression only if the matched result holds.
- **Open:** whether `entity_financials_by_year` should grow a `donations` column
  (line 4500) so both parts read from one blessed panel instead of each re-deriving
  from `raw_t3010_fin`.
