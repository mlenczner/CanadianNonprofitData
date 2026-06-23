# Why Better Grants & Contributions Data Matters
## A Call for Improvement to Canada's Proactive Disclosure of Federal Funding

**Project:** Canadian Nonprofit Data  
**Prepared for:** Imagine Canada Nonprofit Data Working Group  
**Date:** June 2026  
**Contact:** github.com/mlenczner/CanadianNonprofitData

---

## The Short Version

The Government of Canada discloses every grant and contribution it awards — 1.3 million records covering roughly $972 billion in public money. This dataset is one of the most significant public accountability resources in Canada. It is also riddled with data quality problems that make it far less useful than it should be.

This document explains why improving this data matters, situates the problem within Canada's longstanding open government commitments, and describes what we are asking for.

---

## What the Data Is

Under the *Policy on Transfer Payments*, every federal department and agency is required to publish quarterly reports on every grant and contribution they award. This data is consolidated and published at [search.open.canada.ca/grants](https://search.open.canada.ca/grants/) and available as a bulk download from the Open Government Portal.

Grants and contributions are the primary mechanism by which the federal government funds the nonprofit and charity sector — for community development, health services, social programs, arts and culture, Indigenous self-determination, international development, scientific research, and much more. For most nonprofits, federal G&C funding is a material part of their revenue. For many, it is the majority.

This dataset is, in theory, the most complete public record of what the federal government funds in civil society.

---

## This Problem Has a Long History

This is not a new concern. In 2006, the Independent Blue Ribbon Panel on Federal Grant and Contribution Programs — commissioned by Treasury Board — recommended that TBS "ensure that relevant information about federal investments in grants and contributions is easily available across government" and specifically recommended sharing recipients' grant and contribution funding history through business numbers. Those recommendations were largely unimplemented for years.

In 2016, a proposal was submitted to the Government of Canada's open government ideas platform calling for the development of a formal open data standard for grants and contributions data. That proposal was submitted by a civil society representative and received 140 votes — the most of any idea submitted in that consultation — and was formally reviewed by Treasury Board. It argued that:

> *"The only way that these benefits are achieved is if this data is published in a robust data standard designed specifically for these use cases. Publishing basic machine-readable data along the lines of what is currently available will not provide these results."*

The full proposal is available at: https://open.canada.ca/en/idea/develop-open-data-standard-grants-contributions-data-developper-une-norme-pour-les-donnees

That proposal contributed to improvements. TBS subsequently developed a centralized publishing system, standardized the schema, and expanded mandatory fields. These were meaningful steps. But a decade later, we have conducted a fresh analysis of the published data and found that significant problems remain — and that new mandatory fields introduced as recently as December 2025 are already being ignored by most departments.

---

## Canada's Open Government Commitments

Canada has made repeated, formal, internationally-visible commitments to open government that are directly relevant here.

**The Open Government Partnership.** Canada has been a member of the international Open Government Partnership (OGP) since 2012 — one of the founding members. The OGP requires that members submit an action plan co-created with the public every two years describing commitments for achieving greater transparency, accountability, and public participation. Canada has completed five National Action Plans and is currently developing its sixth (2025–2029).

**Grants and contributions transparency as a specific OGP commitment.** Improving grants and contributions transparency has appeared explicitly in multiple Canadian OGP action plans. In the 2016–2018 plan, Commitment 11 aimed to centralize access via a common portal, expand the amount of information available on grants and contributions funding, and have the Department of Canadian Heritage pilot an advanced open data programme. The OGP's Independent Reporting Mechanism assessed this as a "starred commitment" — one of the strongest possible ratings — and recommended carrying it forward. As part of the implementation of the 2016–18 National Action Plan, Canada took steps including increasing the transparency of information on grants and contributions using a collaborative approach.

**Successive action plans.** Canada's 2022–2024 NAP was the fifth consecutive NAP involving a commitment focusing on making government data easier to access and use, with much weight placed upon delivering information, reports, and analyses, facilitating user engagement with data and information, and managing data standards.

**The Directive on Open Government.** The *Directive on Open Government* (Treasury Board, 2014, updated) requires departments to maximize the release of open data and open information. The proactive disclosure of grants and contributions is a specific, mandatory application of this directive.

**The International Open Data Charter.** Canada is a signatory to the International Open Data Charter, which commits governments to publishing data that is open by default, timely, accessible, comparable, and usable. The G&C dataset falls short of these commitments in measurable ways.

In short: improving grants and contributions data quality is not a new ask. It is the continuation of a commitment Canada has made repeatedly, formally, and publicly — to its own citizens and to the international community.

---

## What the Data Shows

A full technical analysis is available in [data-publishing-problems.md](data-publishing-problems.md) and the associated compliance reports. The headline findings are:

**Missing mandatory descriptions.** 109,383 records — 8.4% of the dataset — have no description of what the funding was for, despite this being a mandatory field. Canadian Heritage is missing descriptions on 33.8% of its records. Environment and Climate Change Canada is missing 30%.

**Boilerplate descriptions.** Of the records that do have a description, 28.8% — roughly 344,000 records — have descriptions under 50 characters. Indigenous Services Canada and Crown-Indigenous Relations and Northern Affairs Canada have descriptions under 50 characters on 99%+ of their records. The most common "description" for tens of thousands of records is literally "Not a Project (Mandated or Core Funding)" — which tells the public nothing.

**New mandatory fields being ignored.** In December 2025, TBS made several fields mandatory for new agreements, including the federal riding number and the recipient business number. Seven months later, 20 departments are at 0% compliance on the riding number field. Canadian Heritage — the department with the most post-December 2025 records — is at 0% on both fields.

**The business number problem.** Only 44.7% of all records in the dataset have a recipient business number. This is the field that would enable linking to the CRA charity registry, identifying total funding to a single organization across departments, and conducting meaningful sector-level analysis. Without it, the dataset cannot answer basic questions like "how much federal money did [organization] receive last year?"

**Batch reporting obscures billions.** Veterans Affairs Canada reports billions of dollars in veteran benefits — disability pensions, pain and suffering compensation — as single records with the recipient name "batch report│rapport en lots." This is technically permitted but renders those records completely opaque. Billions in public spending are disclosed in name only.

---

## Why This Matters for the Nonprofit Sector

The nonprofit and charity sector is the primary non-governmental recipient of federal grants and contributions. Of the 1.3 million records in the dataset:

- 234,766 records (18%) are classified as going to not-for-profit organizations and charities
- 166,371 records (12.8%) go to Indigenous recipients
- 23,873 records (1.8%) go to academic institutions

Together, these categories represent roughly a third of all disclosed federal spending via G&Cs.

**The sector cannot see itself clearly.** Without clean, consistent recipient identifiers linked to CRA business numbers, it is impossible to build a comprehensive picture of federal funding flows to the nonprofit sector. The same organization may appear under dozens of name variants across departments and fiscal years. Aggregate analysis is unreliable.

**Funders and organizations cannot plan together.** Nonprofits and their infrastructure organizations — including Imagine Canada — need reliable data to understand the distribution of federal funding, identify gaps, and make the case for investment in underserved communities and causes. The current data quality makes this analysis unreliable.

**Accountability cuts both ways.** Better data serves not just public accountability but also sector self-knowledge. Understanding which organizations receive federal funding, for what purposes, and with what results is essential for the sector to advocate for its own funding adequacy and to demonstrate its public value.

**The CRA charity registry linkage opportunity.** Canada has one of the most open charity registries in the world — T3010 data is publicly available and has been recognized internationally as a model. Linking federal G&C data to T3010 filings via business numbers would create an extraordinarily powerful dataset for understanding the nonprofit sector's relationship with government. The technical barrier is low. The policy barrier — requiring departments to record business numbers — is the bottleneck, and it is now mandatory in policy but not in practice.

---

## What We Are Asking For

This is not a request to build something new. The infrastructure exists. The policy exists. The mandatory fields exist. We are asking for enforcement of commitments already made.

Specifically, we ask Treasury Board Secretariat and individual departments to:

1. **Enforce the December 2025 mandatory fields** — particularly business number, riding number, and postal code — for all new agreements. The current compliance rates of 40–50% are unacceptable for fields that have been mandatory for seven months.

2. **Remediate description quality** — establish a minimum meaningful length and content standard for the `description_en/fr` fields, and require departments with high rates of boilerplate text (ISC, CIRNAC, ISED, Canadian Heritage) to improve their historical records.

3. **Eliminate batch reporting for statutory payments** — Veterans Affairs and similar departments should disclose individual payment records, or at minimum provide sufficient aggregate data to enable meaningful analysis. Billions of dollars reported to "batch report│rapport en lots" is not proactive disclosure.

4. **Normalize legacy data** — the agreement type field has at least four variants of "Contribution" and three variants of "Grant." This should be a one-time cleaning exercise. Similarly, province and country field contamination is straightforward to fix.

5. **Include G&C data quality in the 6th National Action Plan (2025–2029)** — Canada is currently developing its next OGP action plan. Improving grants and contributions data quality is a concrete, measurable commitment that would fulfill longstanding civil society recommendations and directly serve the nonprofit sector.

6. **Engage the nonprofit sector in schema development** — the original 2016 proposal called for an advisory group including nonprofit representatives. That recommendation was never fully implemented. The upcoming schema review process should include structured engagement with Imagine Canada's Nonprofit Data Working Group and other sector representatives.

---

## About This Project

The Canadian Nonprofit Data project is an open-source civic data initiative analyzing federal grants and contributions data with a focus on the nonprofit and charity sector. All analysis scripts, data documentation, and findings are published openly at:

**https://github.com/mlenczner/CanadianNonprofitData**

We welcome contributions from researchers, data professionals, policy analysts, and nonprofit sector representatives.

---

*Prepared by the Canadian Nonprofit Data project, June 2026.*  
*Data profiled June 23, 2026 from the full grants.csv download (1,303,898 records).*  
*Full technical documentation available at the GitHub repository above.*
