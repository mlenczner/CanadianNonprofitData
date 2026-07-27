> **DRAFT — research prototype.** This is an unreleased working draft produced for research purposes only. Figures are derived from public data using experimental methods, contain known data-quality limitations, and have not been reviewed for publication. Do not cite, circulate, or rely on any figure or claim in this document.

# Policy Context: Grants & Contributions Data and Canada's Open Government Commitments

**Project:** Canadian Nonprofit Data
**Date:** June 2026
**Contact:** github.com/mlenczner/CanadianNonprofitData

---

## Summary

Federal grants and contributions (G&C) data is published under the *Policy on Transfer Payments* and is one of the largest disclosure datasets the Government of Canada produces. It is also the main mechanism by which federal funding to the nonprofit and charity sector can be observed and analyzed. Current data-quality findings on this dataset are documented in [data-publishing-problems.md](data-publishing-problems.md) and [data-quality-rankings.html](data-quality-rankings.html); this document instead summarizes the policy and open-government history behind why the schema and mandatory fields exist in their current form.

---

## Background

Under the *Policy on Transfer Payments*, every federal department and agency publishes quarterly reports on the grants and contributions it awards. This data is consolidated and published at [search.open.canada.ca/grants](https://search.open.canada.ca/grants/) and available as a bulk download from the Open Government Portal.

Grants and contributions are a primary mechanism by which the federal government funds the nonprofit and charity sector — community development, health services, social programs, arts and culture, Indigenous self-determination, international development, scientific research, and other areas. For a meaningful share of nonprofits, federal G&C funding is a material part of revenue.

---

## History of Related Recommendations

**2006 — Independent Blue Ribbon Panel on Federal Grant and Contribution Programs.** Commissioned by Treasury Board, the panel recommended that TBS "ensure that relevant information about federal investments in grants and contributions is easily available across government," and specifically recommended sharing recipients' grant and contribution funding history through business numbers.

**2016 — Open government ideas platform proposal.** A proposal calling for a formal open data standard for grants and contributions data was submitted to the Government of Canada's open government ideas platform by a civil society representative. It received 140 votes — the most of any idea in that consultation — and was formally reviewed by Treasury Board. The proposal argued:

> *"The only way that these benefits are achieved is if this data is published in a robust data standard designed specifically for these use cases. Publishing basic machine-readable data along the lines of what is currently available will not provide these results."*

Full proposal: https://open.canada.ca/en/idea/develop-open-data-standard-grants-contributions-data-developper-une-norme-pour-les-donnees

That proposal is associated with subsequent changes: TBS developed a centralized publishing system, standardized the schema, and expanded mandatory fields, most recently in December 2025.

---

## Canada's Open Government Commitments

**The Open Government Partnership (OGP).** Canada has been a member of the international Open Government Partnership since 2012, one of the founding members. OGP membership requires a public-co-created action plan every two years describing commitments toward greater transparency, accountability, and public participation. Canada has completed five National Action Plans and is developing its sixth (2025–2029).

**Grants and contributions transparency as an OGP commitment.** Improving grants and contributions transparency has appeared explicitly in multiple Canadian OGP action plans. In the 2016–2018 plan, Commitment 11 aimed to centralize access via a common portal, expand the information available on grants and contributions funding, and have the Department of Canadian Heritage pilot an advanced open-data program. The OGP's Independent Reporting Mechanism assessed this as a "starred commitment" — one of its strongest ratings — and recommended carrying it forward.

**The Directive on Open Government.** The *Directive on Open Government* (Treasury Board, 2014, updated) requires departments to maximize the release of open data and open information. Proactive disclosure of grants and contributions is a specific, mandatory application of this directive.

**The International Open Data Charter.** Canada is a signatory to the International Open Data Charter, which commits governments to publishing data that is open by default, timely, accessible, comparable, and usable.

---

## Relevance to the Nonprofit Sector

The nonprofit and charity sector is a primary non-governmental recipient category in this dataset (alongside individuals, for-profit recipients, and other governments/institutions). Consistent, well-populated recipient identifiers — in particular the CRA business number — are what would allow federal G&C data to be linked to the CRA charity registry (T3010) and to other sector datasets, enabling analysis of total federal funding to a given organization across departments and years. Current business-number completeness is documented in [data-publishing-problems.md](data-publishing-problems.md).

---

## About This Project

The Canadian Nonprofit Data project is an open-source project analyzing federal grants and contributions data with a focus on the nonprofit and charity sector. Analysis scripts, data documentation, and findings are published at:

**https://github.com/mlenczner/CanadianNonprofitData**

---

*Prepared by the Canadian Nonprofit Data project, June 2026. Full technical documentation available at the GitHub repository above.*
