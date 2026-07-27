> **DRAFT — research prototype.** This is an unreleased working draft produced for research purposes only. Figures are derived from public data using experimental methods, contain known data-quality limitations, and have not been reviewed for publication. Do not cite, circulate, or rely on any figure or claim in this document.

# Crowding-Out Study: First Large Government Grant vs. Private Donations

Generated: 2026-07-21 09:43

## Methodology

Event study around a charity's first fiscal year with government receipts (federal G&C + OTF + Canada Council) summing to >= $100,000 in that year, with zero receipts from any of those sources in any earlier year. Outcome: T3010 line 4500 (total tax-receipted gifts), pre/post means over the 3 years before and after the treatment year (the treatment year itself excluded).

## Reconciliation: federal-only, vs. the spec's documented prototype

The scope used for the headline result below is broader than the spec's federal-only prototype (see docs/crowding-out-and-flow-through-spec.md). This table reruns the identical federal-only definition as a sanity check against the spec's documented numbers before trusting the broader result.

| Y | treated_n | treated_median_pct | control_n | control_median_pct |
|---|---|---|---|---|
| 2016.0 | 120.0 | 12.8 | 37650.0 | -0.1 |
| 2017.0 | 349.0 | 3.0 | 37246.0 | -2.7 |
| 2018.0 | 204.0 | 1.9 | 36877.0 | -5.1 |
| 2019.0 | 176.0 | 9.2 | 36914.0 | -5.0 |
| 2020.0 | 144.0 | 20.1 | 36904.0 | -1.7 |
| 2021.0 | 193.0 | 11.9 | 36928.0 | 4.6 |


## Headline result: federal G&C + OTF + Canada Council

Naive median % change in donations, treated vs. never-treated, per cohort year.

| Y | treated_n | treated_median_pct | control_n | control_median_pct |
|---|---|---|---|---|
| 2016.0 | 118.0 | 9.2 | 36019.0 | -0.1 |
| 2017.0 | 349.0 | 3.1 | 35638.0 | -2.7 |
| 2018.0 | 166.0 | 0.3 | 35341.0 | -5.1 |
| 2019.0 | 141.0 | 15.2 | 35421.0 | -4.9 |
| 2020.0 | 107.0 | 16.5 | 35425.0 | -1.5 |
| 2021.0 | 169.0 | 11.9 | 35461.0 | 4.8 |


## Matched-control estimate

Treated-minus-matched-control difference in median % change (matched on province, revenue decile, and pre-period donation trend tercile).

| Y | treated_n | matched_control_n | treated_median_pct | matched_control_median_pct | diff_pct |
|---|---|---|---|---|---|
| 2016.0 | 118.0 | 373.0 | 9.2 | 0.6 | 8.6 |
| 2017.0 | 349.0 | 782.0 | 3.1 | -3.9 | 7.0 |
| 2018.0 | 166.0 | 442.0 | 0.3 | 0.5 | -0.2 |
| 2019.0 | 141.0 | 419.0 | 15.2 | -4.8 | 20.0 |
| 2020.0 | 107.0 | 358.0 | 16.5 | 6.4 | 10.1 |
| 2021.0 | 169.0 | 501.0 | 11.9 | 4.9 | 7.0 |


## Fixed-effects regression (matched sample)


```
None
```


## Robustness checks


### Threshold sensitivity

| threshold | total_treated_n | pooled_median_pct |
|---|---|---|
| 50000.0 | 1719.0 | 13.2 |
| 250000.0 | 586.0 | 9.0 |


### Dropping 2020-2021 cohorts (COVID confound)

| Y | treated_n | treated_median_pct | control_n | control_median_pct |
|---|---|---|---|---|
| 2016.0 | 118.0 | 9.2 | 36019.0 | -0.1 |
| 2017.0 | 349.0 | 3.1 | 35637.0 | -2.7 |
| 2018.0 | 166.0 | 0.3 | 35347.0 | -5.1 |
| 2019.0 | 141.0 | 15.2 | 35422.0 | -4.9 |


### Placebo pre-trend (fake event at Y-2)

| Y | treated_n | treated_median_pct |
|---|---|---|
| 2014.0 | 0.0 | — |
| 2015.0 | 327.0 | 7.1 |
| 2016.0 | 165.0 | 1.9 |
| 2017.0 | 130.0 | 24.6 |
| 2018.0 | 94.0 | 19.4 |
| 2019.0 | 160.0 | 1.5 |


### Exact-BN-only (entity-resolution robustness)

| Y | treated_n | treated_median_pct |
|---|---|---|
| 2016.0 | 114.0 | 9.2 |
| 2017.0 | 252.0 | 4.2 |
| 2018.0 | 160.0 | 0.3 |
| 2019.0 | 109.0 | 15.2 |
| 2020.0 | 73.0 | 33.6 |
| 2021.0 | 148.0 | 13.6 |


## Known limitations

- **Selection bias is the headline caveat.** Orgs that win large government grants are plausibly already on a growth trajectory; matching mitigates this but does not eliminate it.
- **Fiscal-year alignment is approximate.** `grants_unified.fiscal_year` is the funder's fiscal year; T3010's `FPE` is the charity's own fiscal period end. A grant late in a funder's fiscal year may land in the charity's following filing year. The treatment year itself is excluded from both windows to reduce this risk, but it is not eliminated.
- **Entity resolution errors propagate.** A missed BN match makes a treated org look never-treated, contaminating the control pool -- quantified above via the exact-BN-only restriction.


## Open questions carried forward

- Whether `entity_financials_by_year` should grow a donations (line 4500) column so this and any future consumer read from one blessed panel instead of each re-deriving it from `raw_t3010_fin`.
- Whether the regression estimate above should be published given only one pass of robustness checking -- the spec's original suggestion was to ship matching first and add regression only if the matched result held up.
