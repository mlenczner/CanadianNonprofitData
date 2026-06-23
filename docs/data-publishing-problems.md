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

**Concerned departments:** Unknown without deeper per-department breakdown — this is a priority analysis task.

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

**Concerned departments:** Unknown without per-department breakdown — analysis task needed.

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

**Concerned departments:** Unknown without per-department breakdown — high priority analysis task given the recency of the policy change.

---

## Problem 4: Negative and Zero Agreement Values

**What the policy says:** `agreement_value` must be greater than zero.

**What the data shows:**
- **16,675 records** have a value of zero or less
- Minimum value in the dataset: **-$214,127,920** (negative $214 million)

**Why it matters:** Some negative values are legitimate — they represent amendments that reduce a previous award. But the schema requires amendments to be tracked via `amendment_number`, not via negative values. Zero-value records have no clear valid use. Without consistent amendment tracking, it's impossible to calculate the true net value of awards to any recipient.

**When it started:** Appears across multiple years — predates the amendment tracking fields added to the current schema.

**Concerned departments:** Unknown without per-department breakdown.

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

**Concerned departments:** Unknown without per-department breakdown.

---

## Problem 8: Recipient Business Number Sparseness

**What the policy says:** Business number is optional for older records, mandatory (9-digit) for agreements starting December 1, 2025 or later.

**What the data shows:**
- Overall completeness: **44.7%** (721,577 missing out of 1.3M records)
- For post-Dec 2025 records: **50.6%** complete — meaning nearly half of new mandatory records are still missing it

**Why it matters:** The business number (CRA-issued 9-digit identifier) is the key to linking this dataset to the CRA charity registry, the corporations registry, and other government datasets. Without it, you cannot reliably track total funding to a single organization across departments or years — the same org may appear under dozens of name variants. This is the single biggest obstacle to meaningful recipient-level analysis.

**When it started:** The field has been optional for most of the dataset's history. The Dec 2025 mandate was meant to fix this going forward, but early compliance is poor.

---

## Problem 9: Federal Riding Number Sparseness

**What the data shows:**
- Overall completeness: **19.7%** (1,047,369 missing)
- For post-Dec 2025 records (where it's now mandatory): **40.3%** complete

**Why it matters:** The federal riding number is what enables constituency-level accountability analysis — understanding which ridings are receiving how much federal funding. With 80% of records missing this field, that analysis is impossible for the bulk of the dataset. Even for the new mandatory cohort, 60% of records are missing it.

---

## Summary Table

| Problem | Records Affected | Severity |
|---|---|---|
| Missing descriptions (mandatory) | 109,383 | High |
| Agreement type non-standard values | 19,248 | Medium |
| Post-Dec 2025 riding number missing | 8,630 of 14,448 | High |
| Post-Dec 2025 business number missing | 7,137 of 14,448 | High |
| Zero or negative values | 16,675 | Medium |
| Garbage dates (1899, future) | Unknown count | Medium |
| Province/country field contamination | ~1,200+ | Low-Medium |
| Short/boilerplate descriptions | 343,693 | High |
| Business number missing (overall) | 721,577 | High |
| Riding number missing (overall) | 1,047,369 | High |

---

## What We Don't Know Yet

The analysis above is based on the full dataset profile. We have not yet broken down most of these problems by department — which departments are the worst offenders, which are exemplary, and whether problems are improving or worsening over time. That analysis is the next priority.

---

*Document prepared by the Canadian Nonprofit Data project. Data profiled June 23, 2026.*
