# Questions & Possible Insights
**Project:** Canadian Nonprofit Data  
**Status:** Working list — to be refined as analysis progresses  

---

## Compliance & Accountability

- Which departments have the highest rates of missing descriptions? Is there a pattern (size, mandate, type)?
- Which departments have the lowest completion rates on the post-Dec 2025 mandatory fields (riding number, business number)? Does this correlate with total spending volume?
- Has description quality improved or worsened over time? Is the Dec 2025 policy update making a measurable difference?
- Which departments are using non-standard agreement type values (full words instead of codes)? Are these mostly older records or recent?
- Are there departments that have never filed a "Nothing to Report" but also have an unusually low record count? Could indicate underreporting.
- What share of records per department are amendments vs. originals? High amendment rates could signal poor planning, contract churn, or scope creep.
- Are there departments with a disproportionate share of very short descriptions? Which departments rely most heavily on boilerplate text?
- Do negative-value records correspond to proper amendment chains, or are they standalone (suggesting misuse of the value field)?

---

## Spending Patterns

- Which departments account for the most total disclosed value? What share of the $972B does the top 5 control?
- Department `141` has 293,922 records — nearly 23% of the dataset. Almost certainly ESDC. What programs drive this volume?
- How did COVID (2020-21, 2021-22) change the volume and composition of grants vs. contributions? Which departments saw the biggest spikes?
- What is the distribution of award sizes by recipient type? Do Indigenous recipients get smaller awards on average? Do for-profits get larger ones?
- What share of total value goes to multi-year awards? Which departments commit the most money in multi-year agreements?
- Are there programs that consistently appear across many fiscal years with similar values — essentially permanent funding streams dressed as grants?
- What does the amendment tail look like — some records have 14+ amendments. What are those agreements, and how much has the value changed?

---

## Recipient Analysis

- Who are the top 20 recipients by total disclosed value across all years? Is this dominated by a few large organizations?
- How many unique recipient names are there? How many are likely duplicates of the same organization under different name variants?
- What share of NFP/charity recipients have a business number that can be matched to the CRA charity registry?
- Are there organizations receiving funding from multiple departments simultaneously? How common is joint funding, and is it disclosed properly?
- How much goes to Indigenous recipients (type A) by department and by year? Has this share increased?
- Which organizations have received the most amendments to their agreements? What does that suggest about program management?
- Are there recipients with very high total values but very short/boilerplate descriptions — i.e. large amounts with minimal public accountability?

---

## Geographic Analysis

- Provincial distribution of funding — does it roughly match population, or are some provinces over/under-represented?
- With riding numbers available for ~20% of records, can we identify the top-funded ridings? Is there a relationship to political representation?
- How much funding goes to international recipients (non-CA country codes)? Which departments are most internationally focused? Which countries receive the most?
- For the post-Dec 2025 cohort where postal codes are more complete, can we do finer-grained geographic analysis?

---

## Nonprofit / Charity Sector Specific

- How many of the NFP/charity recipients (234,766 records) have business numbers that match the CRA charity registry?
- Of matched charities, how does federal G&C funding compare to their reported revenues in T3010 filings? For some orgs, is federal funding the majority of their revenue?
- Are there charities receiving large federal contributions that are not registered with CRA (i.e. non-charitable nonprofits)?
- Which charitable sub-sectors (health, social services, arts, environment, religion, international) receive the most federal funding?
- Are there charities that have been de-registered by CRA but continue to appear as recipients in recent records?
- For academic recipients (type S, 23,873 records) — which universities dominate? How does SSHRC/NSERC/CIHR funding show up here vs. through the granting councils directly?

---

## Data Infrastructure / Meta Questions

- What would a "clean" version of this dataset look like? What transformations are needed?
- Which fields have enough completeness to be reliable for analysis vs. which should be treated as unreliable?
- Can recipient name clustering (fuzzy matching) meaningfully reduce the duplicate entity problem?
- What other federal datasets could be joined to this one — contracts, travel & hospitality, T3010 charity returns, Statistics Canada nonprofit satellite account?
- Is the Open Canada search portal actually surfacing all records, or are there discrepancies between the CSV and the search index?
- How does this dataset compare to equivalent disclosure datasets in other countries (UK, US, Australia)?

---

## Potential Analyses

- Quantify how much federal grant money carries inadequate public description
- Department-by-department compliance comparison
- Riding-level federal funding map (for the ~20% of records with riding data)
- Tracking a single large nonprofit's federal funding across all departments and years
- COVID funding spike: which organizations received emergency contributions and are they still receiving funding?
- Amendment volume: agreements that have been modified 10+ times

---

*Working document — Canadian Nonprofit Data project, June 2026*
