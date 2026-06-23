# Data Publishing Problems: GC Grants & Contributions Proactive Disclosure

**Project:** Canadian Nonprofit Data  
**Dataset:** Proactive Disclosure — Grants and Contributions  
**Source:** https://open.canada.ca/data/en/dataset/432527ab-7aac-45b5-81d6-7597107a7013  
**Dataset size:** 1,303,898 records (as of June 2026)  
**Total disclosed value:** ~$972 billion CAD  

---

## Background

Under the *Policy on Transfer Payments*, all federal departments and agencies are required to publicly disclose every grant and contribution they award, on a quarterly basis. This data is published through the Government of Canada's Open Government Portal and is searchable at https://search.open.canada.ca/grants/

Treasury Board Secretariat (TBS) manages the schema and the centralized publishing system. The schema has been updated significantly over the years — most recently with a wave of new mandatory fields that came into effect **December 1, 2025**.

The problems documented here fall into three categories: **missing mandatory data**, **dirty/inconsistent data**, and **structural limitations** in how the data is published.

---

## Problem 1: Missing Descriptions (Mandatory Field)

**What the policy says:** `description_en` and `description_fr` are mandatory fields. Departments are required to explain why the recipient received funding.

**What the data shows:**
- `description_en` is empty in **109,383 records** (8.4% of the dataset)
- `description_fr` is empty in **113,422 records** (8.7% of the dataset)

**Why it matters:** This is the primary field that explains what public money was used for. Empty descriptions make those records effectively opaque — you can see money went somewhere, but not why.

**When it appears to have started:** Records missing descriptions appear across multiple fiscal years, including recent ones. This is not just a legacy data problem.

**Example:** A record showing $500,000 to a recipient with no description field populated tells the public nothing about the purpose of the award.

**Worst offenders by department** (see [dept-compliance-report.md](dept-compliance-report.md)):

| Department | Records | Missing desc_en | % Missing |
|---|---|---|---|
| Canadian Heritage (pch) | 134,212 | 45,309 | 33.8% |
| Environment and Climate Change Canada (ec) | 8,582 | 2,577 | 30.0% |
| Status of Women Canada (swc-cfc) | 143 | 44 | 30.8% |
| Western Economic Diversification Canada (wd-deo) | 14,556 | 4,240 | 29.1% |
| Global Affairs Canada (dfatd-maecd) | 16,001 | 4,589 | 28.7% |
| Fisheries and Oceans Canada (dfo-mpo) | 18,504 | 4,871 | 26.3% |
| National Research Council Canada (nrc-cnrc) | 77,946 | 20,440 | 26.2% |
| Immigration, Refugees and Citizenship Canada (cic) | 18,393 | 3,153 | 17.1% |
| Natural Resources Canada (nrcan-rncan) | 11,629 | 1,754 | 15.1% |
| Impact Assessment Agency of Canada (iaac-aeic) | 4,319 | 599 | 13.9% |

**Exemplary departments** (50+ records, zero missing descriptions): Parks Canada, Women and Gender Equality Canada, Canadian Space Agency, Canada Energy Regulator, Polar Knowledge Canada, Library and Archives Canada, Privy Council Office, Accessibility Standards Canada, and Canadian Food Inspection Agency.

---

## Problem 2: Controlled Field Used Inconsistently (Agreement Type)

**What the policy says:** `agreement_type` is a mandatory controlled field with three valid values: `C` (Contribution), `G` (Grant), `O` (Other transfer payment).

**What the data shows:**
- `C`: 730,242 records ✓
- `G`: 519,077 records ✓
- `O`: 35,331 records ✓
- `Contribution` (full word): 16,052 records ✗
- `Grant` (full word): 1,870 records ✗
- `CONTRIBUTION` (uppercase): 727 records ✗
- `GRANT` (uppercase): 549 records ✗
- Empty: 50 records ✗

**Total non-compliant records:** ~19,248 (1.5% of dataset)

**Why it matters:** Non-standard values break automated analysis and filtering. A query for all contributions will miss 16,779 records that used text variants instead of the code `C`. This suggests some departments are not using the centralized publishing system correctly, or that data was migrated without normalization.

**When it started:** Likely reflects older records submitted before the controlled vocabulary was enforced, or departments bypassing the standard template.

**Concerned departments** — only three departments account for virtually all non-standard values:

| Department | Records | Dirty type count | % of dept records |
|---|---|---|---|
| Employment and Social Development Canada (esdc-edsc) | 352,726 | 17,922 | 5.1% |
| Transport Canada (tc) | 46,598 | 777 | 1.7% |
| Social Sciences and Humanities Research Council of Canada (sshrc-crsh) | 65,839 | 499 | 0.8% |

All other departments use only the standard codes (`C`, `G`, `O`).

---

## Problem 3: Post-December 2025 Mandatory Fields Not Being Filled

**What the policy says:** As of December 1, 2025, several fields became mandatory for any agreement with a start date on or after that date. These include recipient business number, postal code, federal riding number, program name, program purpose, agreement title, agreement end date, and expected results.

**What the data shows** (14,448 records with start date ≥ Dec 1, 2025):

| Field | % Complete | Missing |
|---|---|---|
| `federal_riding_number` | 40.3% | 8,630 records |
| `recipient_business_number` | 50.6% | 7,137 records |
| `recipient_postal_code` | 92.6% | 1,075 records |

All other new mandatory fields are at 99.8–100%.

**Why it matters:** The riding number and business number are the two fields with the highest analytical value — riding number enables geographic accountability analysis; business number enables linking to charity and corporate registries. Both are failing at high rates only months after becoming mandatory.

**When it started:** The policy took effect December 1, 2025 — this non-compliance is less than 7 months old and already significant.

**Worst offenders — missing `federal_riding_number`** (post-Dec 2025 records only):

| Department | Post-Dec Records | Missing riding | % Missing |
|---|---|---|---|
| Canadian Heritage (pch) | 3,040 | 3,040 | 100.0% |
| SSHRC (sshrc-crsh) | 1,149 | 1,149 | 100.0% |
| NSERC (nserc-crsng) | 672 | 672 | 100.0% |
| Natural Resources Canada (nrcan-rncan) | 542 | 542 | 100.0% |
| Agriculture and Agri-Food Canada (aafc-aac) | 468 | 468 | 100.0% |
| Canada Economic Development for Quebec Regions (ced-dec) | 397 | 397 | 100.0% |
| Atlantic Canada Opportunities Agency (acoa-apeca) | 346 | 346 | 100.0% |
| CIHR (cihr-irsc) | 283 | 283 | 100.0% |
| Global Affairs Canada (dfatd-maecd) | 280 | 280 | 100.0% |
| Fisheries and Oceans Canada (dfo-mpo) | 255 | 255 | 100.0% |

20 departments have 100% missing riding numbers on their post-Dec 2025 records. Only Employment and Social Development Canada (esdc-edsc, 2,955 post-Dec records) achieves 100% compliance on this field.

**Worst offenders — missing `recipient_business_number`** (post-Dec 2025 records only):

| Department | Post-Dec Records | Missing biz# | % Missing |
|---|---|---|---|
| Canadian Heritage (pch) | 3,040 | 3,040 | 100.0% |
| Global Affairs Canada (dfatd-maecd) | 280 | 280 | 100.0% |
| Fisheries and Oceans Canada (dfo-mpo) | 255 | 255 | 100.0% |
| Impact Assessment Agency of Canada (iaac-aeic) | 237 | 237 | 100.0% |
| Environment and Climate Change Canada (ec) | 199 | 199 | 100.0% |
| Housing, Infrastructure and Communities (infc) | 97 | 97 | 100.0% |
| Canadian Space Agency (csa-asc) | 74 | 74 | 100.0% |
| Canada Water Agency (cwa-aec) | 69 | 69 | 100.0% |
| Indigenous Services Canada (isc-sac) | 64 | 64 | 100.0% |
| NSERC (nserc-crsng) | 672 | 612 | 91.1% |

**Notable compliance:** ESDC (esdc-edsc) achieves 100% on all post-Dec 2025 mandatory fields across 2,955 records. Prairies Economic Development Canada and Pacific Economic Development Canada also show full compliance.

---

## Problem 4: Negative and Zero Agreement Values

**What the policy says:** `agreement_value` must be greater than zero.

**What the data shows:**
- **16,675 records** have a value of zero or less
- Minimum value in the dataset: **-$214,127,920** (negative $214 million)

**Why it matters:** Some negative values are legitimate — they represent amendments that reduce a previous award. But the schema requires amendments to be tracked via `amendment_number`, not via negative values. Zero-value records have no clear valid use. Without consistent amendment tracking, it's impossible to calculate the true net value of awards to any recipient.

**When it started:** Appears across multiple years — predates the amendment tracking fields added to the current schema.

**Worst offenders by department:**

| Department | Records | Zero/Neg value count | % of dept records |
|---|---|---|---|
| Employment and Social Development Canada (esdc-edsc) | 352,726 | 10,161 | 2.9% |
| Indigenous Services Canada (isc-sac) | 156,896 | 3,885 | 2.5% |
| Crown-Indigenous Relations and Northern Affairs Canada (aandc-aadnc) | 21,444 | 1,129 | 5.3% |
| National Research Council Canada (nrc-cnrc) | 77,946 | 184 | 0.2% |
| Natural Resources Canada (nrcan-rncan) | 11,629 | 108 | 0.9% |
| Fisheries and Oceans Canada (dfo-mpo) | 18,504 | 97 | 0.5% |
| Public Health Agency of Canada (phac-aspc) | 5,288 | 77 | 1.5% |
| Canadian Northern Economic Development Agency (cannor) | 2,018 | 79 | 3.9% |
| Public Safety Canada (ps-sp) | 3,476 | 57 | 1.6% |
| CIHR (cihr-irsc) | 44,051 | 46 | 0.1% |

ESDC alone accounts for 61% of all zero/negative value records in the dataset.

---

## Problem 5: Garbage Dates

**What the data shows:**
- Earliest agreement start date in the dataset: **1899-12-30**
- Latest agreement start date: **2027-01-01**

**Why it matters:** 1899-12-30 is a well-known artifact of Microsoft Excel's date handling — it's what Excel produces when a date cell is empty or contains a zero. Its presence indicates some departments are submitting data via spreadsheet without proper validation, and those records are making it into the published dataset uncleaned. Future-dated records (2027) may be legitimate multi-year agreement projections, but without context they're indistinguishable from errors.

**When it started:** Likely reflects data from earlier years when spreadsheet submission was more common.

---

## Problem 6: Country and Province Field Contamination

**What the policy says:** `recipient_country` uses ISO 3166 country codes (controlled list). `recipient_province` uses Canadian province/territory codes and is only required when the country is Canada.

**What the data shows:**

*Country field:*
- `ca` (lowercase): 156 records — should be `CA`

*Province field (non-Canadian values appearing):*
- `OC`: 412 records (not a valid Canadian province code)
- `CA`: 239 records (country code in province field)
- `NY`: 208 records (New York state)
- `MA`: 164 records (Massachusetts)
- `NA`: 97 records
- `US`: 65 records

**Why it matters:** Foreign addresses are being entered into Canada-only fields, indicating departments are not following the schema correctly for international recipients. This corrupts geographic analysis.

**When it started:** Unknown — requires per-record date analysis.

---

## Problem 7: Description Quality — Boilerplate Text

**What the data shows:**
- Of 1,194,515 records that have a description, **343,693 (28.8%)** have descriptions under 50 characters
- Median description length: 199 characters
- Mean description length: 215 characters

**Why it matters:** A 50-character description for a grant of any significant size is almost certainly not meaningful. Examples of the kind of text this captures: "Support for community project", "Funding for operations", "Research grant." These technically satisfy the non-empty requirement but provide no real accountability value.

**When it started:** Likely a persistent pattern across all years — this is a cultural/compliance problem as much as a technical one.

**Worst offenders by department** (descriptions under 50 characters):

| Department | Records | Short desc | % Short |
|---|---|---|---|
| Crown-Indigenous Relations and Northern Affairs Canada (aandc-aadnc) | 21,444 | 21,319 | 99.4% |
| Indigenous Services Canada (isc-sac) | 156,896 | 155,837 | 99.3% |
| Public Health Agency of Canada (phac-aspc) | 5,288 | 4,738 | 89.6% |
| Innovation, Science and Economic Development Canada (ic) | 41,673 | 34,093 | 81.8% |
| Veterans Affairs Canada (vac-acc) | 2,163 | 1,334 | 61.7% |
| Canadian Heritage (pch) | 134,212 | 59,362 | 44.2% |
| Health Canada (hc-sc) | 2,252 | 978 | 43.4% |
| Atlantic Canada Opportunities Agency (acoa-apeca) | 30,350 | 12,733 | 42.0% |
| Employment and Social Development Canada (esdc-edsc) | 352,726 | 40,928 | 11.6% |
| Immigration, Refugees and Citizenship Canada (cic) | 18,393 | 4,547 | 24.7% |

Indigenous Services Canada and Crown-Indigenous Relations and Northern Affairs Canada together account for 177,156 short-description records — over half of all short descriptions in the dataset.

---

## Problem 8: Recipient Business Number Sparseness

**What the policy says:** Business number is optional for older records, mandatory (9-digit) for agreements starting December 1, 2025 or later.

**What the data shows:**
- Overall completeness: **44.7%** (721,577 missing out of 1.3M records)
- For post-Dec 2025 records: **50.6%** complete — meaning nearly half of new mandatory records are still missing it

**Why it matters:** The business number (CRA-issued 9-digit identifier) is the key to linking this dataset to the CRA charity registry, the corporations registry, and other government datasets. Without it, you cannot reliably track total funding to a single organization across departments or years — the same org may appear under dozens of name variants. This is the single biggest obstacle to meaningful recipient-level analysis.

**When it started:** The field has been optional for most of the dataset's history. The Dec 2025 mandate was meant to fix this going forward, but early compliance is poor.

**Worst offenders — post-Dec 2025 records** (where business number is now mandatory):

| Department | Post-Dec Records | Missing biz# | % Missing |
|---|---|---|---|
| Canadian Heritage (pch) | 3,040 | 3,040 | 100.0% |
| Global Affairs Canada (dfatd-maecd) | 280 | 280 | 100.0% |
| Fisheries and Oceans Canada (dfo-mpo) | 255 | 255 | 100.0% |
| Impact Assessment Agency of Canada (iaac-aeic) | 237 | 237 | 100.0% |
| Environment and Climate Change Canada (ec) | 199 | 199 | 100.0% |
| Public Safety Canada (ps-sp) | 193 | 180 | 93.3% |
| NSERC (nserc-crsng) | 672 | 612 | 91.1% |
| CIHR (cihr-irsc) | 283 | 278 | 98.2% |
| SSHRC (sshrc-crsh) | 1,149 | 914 | 79.5% |
| Parks Canada (pc) | 218 | 150 | 68.8% |

---

## Problem 9: Federal Riding Number Sparseness

**What the data shows:**
- Overall completeness: **19.7%** (1,047,369 missing)
- For post-Dec 2025 records (where it's now mandatory): **40.3%** complete

**Why it matters:** The federal riding number is what enables constituency-level accountability analysis — understanding which ridings are receiving how much federal funding. With 80% of records missing this field, that analysis is impossible for the bulk of the dataset. Even for the new mandatory cohort, 60% of records are missing it.

**Worst offenders — post-Dec 2025 records** (where riding number is now mandatory):

| Department | Post-Dec Records | Missing riding | % Missing |
|---|---|---|---|
| Canadian Heritage (pch) | 3,040 | 3,040 | 100.0% |
| SSHRC (sshrc-crsh) | 1,149 | 1,149 | 100.0% |
| NSERC (nserc-crsng) | 672 | 672 | 100.0% |
| Natural Resources Canada (nrcan-rncan) | 542 | 542 | 100.0% |
| Agriculture and Agri-Food Canada (aafc-aac) | 468 | 468 | 100.0% |
| Canada Economic Development for Quebec Regions (ced-dec) | 397 | 397 | 100.0% |
| Atlantic Canada Opportunities Agency (acoa-apeca) | 346 | 346 | 100.0% |
| CIHR (cihr-irsc) | 283 | 283 | 100.0% |
| Global Affairs Canada (dfatd-maecd) | 280 | 280 | 100.0% |
| Fisheries and Oceans Canada (dfo-mpo) | 255 | 255 | 100.0% |

20 of 43 departments with post-Dec 2025 records have 100% missing riding numbers. ESDC (esdc-edsc) is the only large department with full compliance (2,955 records, 100% complete).

---

## Summary Table

| Problem | Records Affected | Worst Offender(s) | Severity |
|---|---|---|---|
| Missing descriptions (mandatory) | 109,383 | Canadian Heritage (33.8%) | High |
| Agreement type non-standard values | 19,248 | ESDC (17,922 records) | Medium |
| Post-Dec 2025 riding number missing | 8,630 of 14,448 | 20 depts at 100% missing | High |
| Post-Dec 2025 business number missing | 7,137 of 14,448 | Canadian Heritage (100%) | High |
| Zero or negative values | 16,675 | ESDC (10,161 records) | Medium |
| Garbage dates (1899, future) | Unknown count | — | Medium |
| Province/country field contamination | ~1,200+ | — | Low-Medium |
| Short/boilerplate descriptions | 343,693 | ISC (99.3%), CIRNAC (99.4%) | High |
| Business number missing (overall) | 721,577 | Canadian Heritage (100% post-Dec) | High |
| Riding number missing (overall) | 1,047,369 | 20 depts at 100% post-Dec | High |

Full per-department breakdown: [dept-compliance-report.md](dept-compliance-report.md)

---

## What We Don't Know Yet

Per-department breakdowns are now available for Problems 1–4, 7–9 (see [dept-compliance-report.md](dept-compliance-report.md)). Remaining gaps:

- **Temporal trends** — are problems improving or worsening over time?
- **Garbage dates** — which departments are submitting Excel artifacts (1899-12-30) and future-dated records?
- **Province/country contamination** — per-department breakdown not yet run
- **Amendment tracking** — are negative values properly linked to amendment chains?

---

*Document prepared by the Canadian Nonprofit Data project. Data profiled June 23, 2026. Department breakdown added from compliance report generated same date.*
