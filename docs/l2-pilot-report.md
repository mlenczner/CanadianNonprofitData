> **DRAFT — research prototype.** This is an unreleased working draft produced for research purposes only. Figures are derived from public data using experimental methods, contain known data-quality limitations, and have not been reviewed for publication. Do not cite, circulate, or rely on any figure or claim in this document.

# L2 Classification Pilot Report

Generated: 2026-07-13 12:54 UTC

## Dry-run vs. pilot

- Backend: ollama (qwen2.5:7b)
- Distinct texts in scope: 1,000
- Pilot classification rows written: 1,000 (ok: 905, error: 95)
- Actual cost: $0.00 (local Ollama model, qwen2.5:7b). Token usage: 15,909,651 input / 93,857 output (no prompt caching -- Anthropic-specific optimization, not applicable here)

## Confidence distribution

- high: 414 (45.7%)
- medium: 27 (3.0%)
- abstain: 464 (51.3%)
- quote_failed downgrades: 55
- bad_code downgrades: 194

## Housing benchmark

Category -> PCS code mapping (looked up in the Subject sheet):

- `housing_first` -> `SS070102`
- `supportive_housing` -> `SS070100`
- `emergency_shelter` -> `SS070400`
- `transitional_housing` -> `SS070106`

(1 benchmark rows skipped: their ref_number is reused by more than one distinct owner_org in grants.csv, and the benchmark CSV doesn't carry the raw owner_org needed to disambiguate which grant it actually meant -- excluded rather than guessed.)

Of 334 benchmark **rows** the pilot classified with a code, 167 agree with the mapped category (match or ancestor/descendant): **50.0%**. Note: the benchmark CSV can tag the same underlying grant under more than one category (168 ref_numbers are tagged both `emergency_shelter` and `supportive_housing`), so this counts benchmark rows, not distinct grants -- see the per-category and disagreement-cluster breakdowns below for what's actually driving the number.

By category:

| Category | Agree | Total | Rate |
|---|---|---|---|
| emergency_shelter | 0 | 167 | 0.0% |
| supportive_housing | 167 | 167 | 100.0% |

Disagreements (167 benchmark rows in 1 distinct disagreement pattern(s)):

| Count | CSV category (mapped code) | LLM code(s) | LLM quote | Example recipients |
|---|---|---|---|---|
| 167 | emergency_shelter (SS070400) | SS070102, SS070106 | “emergency shelters - transitional/supportive housing” | PHS Community Services Society, Fonds dédié à l'habitation communautaire de Montréal, Maison St-Dominique, and 164 more |

## 25 random high-confidence classifications for review

| Text (truncated) | Code(s) | Quote | Rationale |
|---|---|---|---|
| Canada History Fund - CHF - Strategic Initiatives Canada History Fund - CHF - Strategic Initiatives | SA040100,SA070400 | “Canada History Fund - CHF - Strategic Initiatives” | supports cultural awareness and museums |
| Canada Arts Presentation Fund - CAPF - Budget 2021 Re-engaging Canada Arts Presentation Fund - CAPF  | SA060300 | “Canada Arts Presentation Fund - CAPF” | Funds performing arts, specifically music. |
| The objectives of the Program are: • promoting volunteerism among seniors; • engaging seniors in the | SD030000,SG090400 | “promoting volunteerism among seniors” | focuses on senior engagement and mentorship, targeting the elderly population. |
| Canada Research Chair - Tier 1 | SF040600,SB050200 | “Canada Research Chair - Tier 1” | Research and university education. |
| Support aboriginal communities in becoming successful participants in commercial fisheries and aquac | SM040200,SM040100 | “Support aboriginal communities in becoming successful participants in commercial fisheries and aquaculture” | Supports aboriginal involvement in fishing and aquaculture. |
| The project helps Canadian dairy farmers improve productivity through upgrades to their equipment. D | SM010400,SJ060700 | “improve productivity through upgrades to their equipment.” | focuses on dairy farming and infrastructure development. |
| ;Non-repayable contribution Community Infrastructure Improvement Fund (QEDP-CIIF) | SN030200,SN040100 | “Community Infrastructure Improvement Fund (QEDP-CIIF)” | Supports infrastructure and housing development. |
| To strengthen responses to drug and substance use issues in Canada. Substance Use and Addictions Pro | SE120500,SE120503 | “Substance Use and Addictions Program” | Focuses on drug and substance use, supporting addiction services. |
| The NSERC’s Strategic Partnership Grants aims to increase research and training in targeted areas th | SA050600,SB040200 | “Strategic Partnership Grants” | Focuses on applied research with industry partners. |
| The Dairy Farm Investment Program helps Canadian dairy farmers improve productivity through upgrades | SM010400,SN030200 | “The Dairy Farm Investment Program helps Canadian dairy farmers improve productivity through upgrades to their equipment.” | Agricultural improvement, community improvement |
| Enhancement of Official Languages Program Enhancement of Official Languages Program - Second Languag | SA060302,SB050100 | “- Second Language Learning” | Focuses on second language learning within higher education. |
| This program attracts and retains the best and brightest by recognizing and providing support to res | SB050300,SE140900 | “including funding for graduate or postgraduate level research” | Supports research talent, includes graduate/postgraduate funding. |
| The objective of this project is to help Canadian dairy farmers improve productivity through upgrade | SM010400,SN060500 | “help Canadian dairy farmers improve productivity through upgrades to their equipment” | focuses on farming equipment and farmer support |
| Support aboriginal communities in becoming successful participants in commercial fisheries and aquac | SM040200,SM010400 | “Support aboriginal communities in becoming successful participants in commercial fisheries and aquaculture” | focuses on supporting ab原住民社区参与商业渔业和水产养殖,归类于农业、渔业和支持性农业活动。 |
| Multiculturalism and Anti-Racism Program - CAHAP - Events Multiculturalism and Anti-Racism Program - | SA040300,SG100000 | “Multiculturalism and Anti-Racism Program - CAHAP - Events” | supports specific cultural awareness and social sciences |
| Building Communities Through Arts and Heritage Building Communities Through Arts and Heritage | SA020000,SG010300 | “Building Communities Through Arts and Heritage” | focuses on cultural awareness and anthropology |
| University Undergraduate Student Research Awards | SB031000,SE140400 | “Undergraduate Student Research Awards” | Supports undergraduate education and medical research. |
| Alexander Graham Bell Canada Graduate Scholarship M | SB060100,SG090400 | “Alexander Graham Bell Canada Graduate Scholarship” | Supports graduate studies and interdisciplinary research. |
| Welcome international students from a wide range of countries and territories on a short-term study  | SE050700,SJ060612 | “Emerging Leaders in the Americas and/or Study in Canada Scholarships Programs” | programs involve short-term study exchanges, which relate to scholarships and academic relations. |
| Sport Support Program - National Multisport Service Organizations Sport Support Program - National M | SQ020000,SQ021600 | “Sport Support Program - National Multisport Service Organizations” | Supports national multisport service organizations which provides sports training. |
| Canada Research Chair - Tier 2 | SF040600,SB050200 | “Canada Research Chair - Tier 2” | Supports university education and academic research. |
| Museum Assistance Program (MAP) Museum Assistance Program (MAP) - Exhibition Circulation | SA070100 | “Exhibition Circulation” | Supports museum activities. |
| The objective of the EAF is to support community based projects across Canada that improve accessibi | SM050100,SS060400 | “improve accessibility, remove barriers, and enable Canadians with disabilities to participate in and contribute to their community” | focuses on disability support and self-advocacy |
| The purpose of this project is to undertake deeper consultations with potentially impacted Indigenou | SA040200 | “Indigenous Participation in Dialogues” | Supports participation in cultural consultation. |
| NSERC's PromoScience Program offers financial support for Science Odyssey which is Canada's largest  | SF000000,SG000000 | “NSERC's PromoScience Program offers financial support for Science Odyssey which is Canada's largest celebration of science, technology, engineering and mathematics” | PromoScience supports education events and research training. |
