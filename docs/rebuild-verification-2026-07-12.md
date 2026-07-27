# Rebuild Verification — 2026-07-12

Automated verification of the entity-graph rebuild started ~00:30 EDT 2026-07-12 (post-fix: amendment dedup + BN-residual registration + bilingual pipe-name normalization). DB file last modified 01:51 (~81 min build, within the 60–90 min estimate); no `.wal` present; read-only connection succeeded. All numbers below queried directly from `nonprofit_network.duckdb`, not from logs.

| # | Check | Expected | Actual | Result |
|---|-------|----------|--------|--------|
| a | `COUNT(*) raw_grants_latest` | 1,174,938 | 1,174,938 | PASS |
| b | `grants_unified` federal_gc SUM | ~$831.2B (827–835B) | $831.17B (1,086,085 rows) | PASS |
| c | BN roots mapped to >1 entity | 0 (was 18,139) | 0 | PASS |
| d | Entities matching "prince rupert port authority" | 1 (was 6) | 2 | PARTIAL |
| e | `pytest tests/` | all passing (28+) | 30 passed | PASS |

Other `grants_unified` totals for reference: t3010_qualified_donee $129.2B (3,744,650), canada_council $2.5B (65,002), t3010_non_qualified_donee $2.3B (29,309).

## Check (d) detail

Down from 6 entities to 2, but not fully consolidated:

- entity 102089 — bn_root 119332617, province BC; absorbs all BN-bearing variants (exact_bn) plus several no-BN rows.
- entity 160959 — bn_root NULL, province NULL; three `unmatched_new` links, all raw_name `Prince Rupert Port Authority|Administration portuaire de Prince Rupert` with raw_bn NULL.

The residual entity comes from no-BN records that also lack a province, so they miss the name+province merge key that folded other no-BN rows into 102089. Also note both canonical_names still contain the raw pipe-formatted bilingual string — normalization appears applied to the match key but not to the stored `canonical_name`.

## Stale docs

`docs/entity-resolution-methodology.md` still cites the pre-fix federal_gc total: "952.2" / "$952" appears at lines 69, 71, and 92. NOT yet updated to the new ~$831.2B figure (line 92's "~$20B gap to $972B" narrative is also now wrong).

## Verdict

**PASS with two minor follow-ups.** The two bug fixes took effect: amendment dedup brought federal_gc from $952B to $831.2B, and BN-residual registration eliminated all 18,139 duplicate-BN entities. Remaining: (1) a small no-BN/no-province residual duplicate (check d: 2 entities, not 1) plus pipe-formatted canonical_name display strings; (2) stale $952.2B figures in the methodology doc.
