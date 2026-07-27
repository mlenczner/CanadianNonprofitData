> **DRAFT — research prototype.** This is an unreleased working draft produced for research purposes only. Figures are derived from public data using experimental methods, contain known data-quality limitations, and have not been reviewed for publication. Do not cite, circulate, or rely on any figure or claim in this document.

# Flow-Through Mapping: Government Money Through Regranting Charities

Generated: 2026-07-21 14:50

## Honest denominator

This report counts **co-occurrence**, not dollar-tracing: "org X received $A in government money and separately re-granted $B" in an overlapping fiscal year. Money is fungible and the two totals are not the same dollars -- this is the same "claim and receipt" discipline already used on org pages, applied here at the network level.

## Reconciliation note

The spec's documented prototype (federal-only, "latest filing only" intermediary rule) found **1,414 charities** both receiving federal G&C money (**$11,190,000,000** all-time) and re-granting **$5,230,000,000** in their latest filing year. This report uses a broader government-funding scope (federal G&C + OTF + Canada Council) and the spec's stricter year-overlap intermediary rule instead of the prototype's latest-filing shortcut, so the numbers below are not directly comparable to the prototype's -- both rule changes are intentional (see this file's module docstring), not a discrepancy to reconcile away.

## Headline aggregates


- Total hop-1 receipts (federal G&C + OTF + Canada Council, all years): $811,941,312,968

- Charities flagged as intermediaries: 1,928

- Hop-1 receipts among flagged-intermediary org-years: $10,565,808,429 (1.3% of all hop-1 receipts)

- Total re-granted by intermediaries (direct, hop_depth=1 edges): $15,648,982,580

- Chains flagged as cycles (traversal stopped): 17,918


## Top intermediaries by amount re-granted

| Intermediary BN root | Entity ID | Total re-granted |
|---|---|---|
| 896568417 | 78592 | $1,897,104,048 |
| 107951618 | 6930 | $523,635,481 |
| 119276723 | 24767 | $478,423,489 |
| 106702251 | 1165 | $328,606,355 |
| 119278216 | 24838 | $316,856,012 |
| 100072586 | 9 | $312,824,256 |
| 118852433 | 11616 | $303,320,872 |
| 130229750 | 28997 | $245,207,134 |
| 108160185 | 8612 | $230,697,387 |
| 108074436 | 8041 | $222,945,605 |
| 136535226 | 34221 | $222,796,698 |
| 880846829 | 62545 | $217,626,409 |
| 118974179 | 15566 | $189,396,083 |
| 118881549 | 12556 | $176,389,355 |
| 130643737 | 29598 | $174,589,374 |
| 106966591 | 80380 | $172,469,133 |
| 119257939 | 24086 | $169,570,560 |
| 122680572 | 26697 | $168,728,164 |
| 119278141 | 24834 | $163,745,335 |
| 118950120 | 14779 | $156,975,771 |


**Note on CanadaHelps (bn_root 896568417):** it ranks highly here because it is a donation-processing platform that routes giving for a large share of Canadian charities, not because it makes discretionary re-grants the way a foundation or a charity like The Salvation Army does. Its "donees" in T3010 Schedule 6 are overwhelmingly pass-through disbursements to donor-designated charities, not grant-making decisions -- lumping it into "top regranting intermediaries" without this context would overstate how much of this list reflects discretionary re-granting.


## Chain-depth distribution

| Hop depth | Edges |
|---|---|
| 1 | 372,468 |
| 2 | 1,693,388 |


## Worked example: Aga Khan Foundation Canada


| Donee | Amount |
|---|---|
| Aga Khan Foundation | $99,839,139 |
| Aga Khan Foundation | $55,012,155 |
| Aga Khan Foundation | $51,437,331 |
| AGA KHAN FOUNDATION | $43,441,722 |
| Aga Khan Foundation | $37,162,371 |
| The Aga Khan Museum | $7,738,880 |
| The Aga Khan Museum | $6,387,688 |
| THE AGA KHAN MUSEUM | $4,300,000 |
| FOCUS Humanitarian Assistance in Canada | $3,630,701 |
| The Aga Khan Museum | $1,793,900 |
| FOCUS Humanitarian Assistance in Canada | $1,376,377 |
| Aga Khan Museum | $573,785 |
| FOCUS Humanitarian Assistance Canada | $67,074 |
| FOCUS Humanitarian Assistance in Canada | $50,003 |
| FOCUS HUMANITARIAN ASSISTANCE IN CANADA | $9,926 |
| GLOBAL CENTRE FOR PLURALISM | $3,204 |


## Limitations

- **Year overlap is calendar-year equality**, not month-level fiscal alignment -- a funder's fiscal year and a charity's T3010 fiscal period end don't necessarily line up (same caveat as Part A).
- **Donee BN resolution is best-effort.** A donee with no BN on file, or a BN that doesn't resolve to an entity in this graph, still appears in the chain by raw name, just without a linked entity_id.
- **Not every real regrant chain is captured.** Only qualified-donee gifts (Schedule 6) are traced; non-qualified-donee gifts and gifts made outside the flagged overlapping fiscal year are out of scope.
