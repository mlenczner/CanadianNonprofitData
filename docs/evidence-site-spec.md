# Spec: Evidence Encyclopedia Demo ("what works, who does it, show the receipts")

**Deliverable:** `analysis/build_evidence_site.py` — generates a small linked static site under
`docs/evidence/` from two curated input files, plus a test suite. This is the demo for the
intervention/evidence project (design docs live in the Obsidian vault; this spec is
self-contained — you don't need them).

## The idea

Two page types sharing one design language (same palette, cards, and claim-and-receipt
drawer mechanics as `analysis/org_page.py` — reuse that CSS/JS wholesale):

1. **Intervention pages** — one per intervention: what it is, how the major evidence
   registries rate it (side by side, never averaged), what the studies actually found,
   a standing warning that evidence travels imperfectly, and — the differentiator —
   which Canadian organizations were identified as delivering it, with receipts.
2. **An index page** — the front door: one-paragraph framing, the list of intervention
   pages, and an honest "in progress / not yet shown" list.

Ratings are **cited as facts with links, never reproduced as content** — Blueprints'
terms prohibit reproducing their materials without permission (see Build Plan R6 in the
vault). Quote the rating word + link out. Do not mirror registry descriptions.

## Inputs (already in the repo — do not modify them)

- `evidence/evidence-spine-seed.yaml` — interventions with `id`, `name`, `aliases`,
  `mechanism`, `canadian_relevance`, and `evidence[]` entries carrying `source`,
  `finding`, `status`, and inline `#` comments with source URLs/notes. **Status values
  are the contract:** `verified_YYYY-MM-DD` (shown normally, date displayed),
  `from_model_knowledge` (shown de-emphasized with an explicit "UNVERIFIED — drafted
  from model knowledge, not yet checked against the source" badge — never styled like
  a verified entry), `editorial_position` (shown as an italic editorial note).
  Needs PyYAML — add `pyyaml`, pinned, to requirements.txt.
- `evidence/seed-classifications-housing-canada.csv` — 394 org×category rows:
  `category, recipient_legal_name, business_number, city, province, n_grants, total_cad,
  first_year, last_year, example_funder, receipt_description_snippet, receipt_ref_number,
  entity_id, match_method, entity_kind, bn_root, latest_revenue, fiscal_period_end`.
  The CSV `category` values map to YAML intervention `id`s (housing_first,
  supportive_housing, emergency_shelter, transitional_housing).

No database dependency — the CSV already carries everything, so this builds anywhere.

## Intervention page anatomy

1. **Header** — intervention name, aliases as small chips, DRAFT treatment (below).
2. **What it is** — the `mechanism` text, plain language.
3. **Evidence, side by side** — one card per `evidence[]` entry: registry/source name,
   the rating or finding, status badge (verified + date / UNVERIFIED / editorial),
   source link (from the YAML comments). Never combine into a composite score. If
   entries disagree, they just sit next to each other — that's the feature.
4. **Will it work here? (standing transfer caveat)** — a fixed box on every page:
   evidence comes from specific places, populations, and delivery conditions; effects
   often shrink or vanish elsewhere. Where the YAML contains a concrete example (NFP's
   UK Building Blocks null result), cite it here. Keep it to ~4 sentences.
5. **Organizations identified as delivering this (Canada)** — from the CSV, matching
   rows only. Table: org name, city/province, grants, total dollars, year range.
   Each row is a claim: its drawer shows the receipt — the funder-authored description
   snippet, the grant ref_number, the funder, and the classification method ("matched
   on category terms in federal grant descriptions, latest-amendment deduped").
   Cap at 50 rows by total dollars with a "showing 50 of N" note. If the org has a
   generated profile page at `docs/orgs/<slug>.html` (check the filesystem at build
   time), link its name there (relative link `../orgs/<slug>.html`); otherwise plain text.
   For interventions with no CSV rows (NFP, MST, Roots of Empathy): a short honest note —
   "No Canadian organizations identified from federal grant text yet — this intervention
   is provincially/health-system funded in Canada" (NFP/MST) or equivalent.
6. **Footer** — generated date, canonical draft disclaimer, provenance line.

**Category-not-model entries** (emergency_shelter): render the `editorial_position`
text prominently — the page should say plainly that this is a service category, not an
evaluated model, and that it serves as the comparison condition in the Housing First
literature.

## Index page

- Title + one-paragraph framing (write it from this spec's "The idea," not marketing
  copy): two separate questions funders conflate — does the intervention work anywhere,
  and will it work here — and this site's approach: registry ratings cited side by side,
  Canadian org identification with receipts, uncertainty shown rather than smoothed.
- Card list of published intervention pages: name, one-line mechanism, chips showing
  how many registries rate it and how many Canadian orgs were identified.
- **"In progress, not yet shown"** list for interventions whose evidence entries are
  all unverified (Breaking the Cycle) — name + one line on why it's held back
  ("Canadian evaluations exist; not yet read and verified"). This honesty is a feature;
  don't hide the list.
- Draft treatment + footer as everywhere.

## Draft treatment (required, same as the rest of the project)

Sticky amber banner ("DRAFT — research prototype, not for circulation"), the diagonal
low-opacity DRAFT watermark, `[DRAFT]` title prefix, canonical disclaimer text in the
footer — identical implementation to the org pages and dashboards. Reuse, don't rewrite.

## Rendering rules that matter

- Only interventions with ≥1 `verified_*` evidence entry get a page; all-unverified ones
  go to the index's "in progress" list. This rule is a test case.
- `from_model_knowledge` entries on otherwise-published pages render with the UNVERIFIED
  badge and muted styling — present but impossible to mistake for a checked fact.
- Self-reported claims (an org's own site, e.g. Roots of Empathy's "1.2M children")
  carry their YAML note ("self-reported, not independent evidence") visibly.
- Every source link opens the registry/report — no dead text citations where a URL exists
  in the YAML comments.
- Money/number formatting identical to org_page.py's helpers (import them; don't copy).

## Tests (pytest, `tests/test_evidence_site.py`)

Fixture YAML + CSV written in-test (3 interventions: one fully verified, one mixed
verified/unverified, one all-unverified; 5 org rows incl. one mapping to a nonexistent
org page and one to an existing dummy file). Cover:
- All-unverified intervention gets NO page and DOES appear in the index "in progress" list.
- Mixed page: unverified entry carries the UNVERIFIED badge string; verified one doesn't.
- Org receipt drawer contains the description snippet and ref_number from the CSV.
- Org link only rendered when the target file exists.
- 50-row cap + rollup note on a fixture with 60 rows.
- Draft banner + watermark + [DRAFT] title present in every generated file.
- Reuse the drawer-not-inside-hidden-container regression check from test_org_page.py.
- HTML parses; no unreplaced template tokens.
Fast (<10s), offline, tmp dirs only.

## Definition of done

- [ ] `analysis/build_evidence_site.py` runs with no arguments and writes
      `docs/evidence/index.html` + one page per qualifying intervention.
- [ ] Full test suite green (existing + new).
- [ ] Generated demo committed: index + housing_first, nurse_family_partnership,
      multisystemic_therapy, roots_of_empathy, emergency_shelter.
- [ ] Eyeball pass: open index in a browser, click through to Housing First, open an
      org receipt drawer, confirm the Building Blocks caveat renders on the NFP page,
      confirm Breaking the Cycle appears only in "in progress."
- [ ] One line each in README's file table and AGENTS.md's dashboards section.
- [ ] "Decisions" note appended to this spec for anything it didn't cover.

## Non-goals

No scraping, no new evidence research, no composite scores, no search, no DB queries,
no modifications to the two input files, no schema changes anywhere else in the repo.

## Decisions

Choices made while implementing where this spec didn't specify, smallest-reasonable-choice
rather than asking:

- **Qualifying rule reconciled in favor of the Definition of Done.** The spec's rendering
  rule says "≥1 `verified_*` evidence entry" gets a page, but `emergency_shelter`'s only
  entry is `editorial_position`, and the Definition of Done explicitly lists it as one of
  the 5 pages to generate. Implemented as "≥1 entry that is *not* `from_model_knowledge`"
  (so `verified_*` and `editorial_position` both qualify) — this produces exactly the 5
  named pages plus Breaking the Cycle correctly excluded (its one entry is
  `from_model_knowledge`), satisfying both the checklist and the stated test case.
- **`verified_tonight` (a real status value in the seed file, not just `verified_YYYY-MM-DD`)
  is treated as verified with no displayable date** ("Verified — tonight" rendering)
  rather than raising on an undocumented status suffix. Any status not matching
  `verified_*` / `from_model_knowledge` / `editorial_position` still raises — only the
  date-suffix parsing is lenient.
- **Source links are recovered from the YAML's inline `#` comments by scanning the raw
  file text**, since PyYAML discards comments entirely and that's the only place the
  source URLs live. A domain-pattern regex turns the first URL-like substring in a
  comment into a link; the full comment text is always shown too (this is also how
  Roots of Empathy's "self-reported, not independent evidence" note and MST's
  variant-rating caveat surface, with no special-casing needed).
- **"In progress" reason text is generic, not the spec's illustrative wording.** The spec
  quotes "Canadian evaluations exist; not yet read and verified" for Breaking the Cycle
  specifically. Implemented instead as a mechanical, data-derived line ("All evidence
  entries are unverified — not yet checked against source.") that applies to any
  all-unverified intervention without hardcoding per-intervention prose.
- **The "no Canadian orgs identified" note reuses each intervention's own
  `canadian_relevance` YAML field** generically ("No Canadian organizations identified
  from federal grant text yet. {canadian_relevance}") for NFP, MST, and Roots of Empathy,
  rather than writing distinct prose per intervention.
- **Intervention page filenames use the YAML `id` directly** (`housing_first.html`, not a
  re-slugified version of the display name) — already URL/filename-safe and matches how
  the CSV's `category` column references the same ids.
- **"Show your work" toggle appears only on intervention pages**, which have org-table
  claims/drawers; the index page has no claims, so there's nothing for it to reveal. The
  required draft banner/watermark/`[DRAFT]` title/footer disclaimer are on every page
  regardless — that treatment is unconditional, independent of the toggle.
- **CSS/JS are imported byte-identical from `org_page.py`** (`CSS`, `JS` constants). The
  handful of lines of banner/watermark/footer HTML scaffolding are reconstructed inline
  rather than imported, since `org_page.py` doesn't expose them as a separate callable
  fragment — but the disclaimer *text* itself (`DRAFT_BANNER_TEXT`, `DRAFT_FULL_TEXT`) is
  imported, not retyped, so the two pages' wording can't drift apart.
- **Standing transfer-caveat text is original prose** (the spec asks for ~4 sentences but
  doesn't supply exact wording, unlike the canonical draft disclaimer). A "Concrete
  example" sentence is appended automatically when any evidence entry's `finding` text
  matches `context-transfer|null result` (currently only NFP's Building Blocks entry).
- **Observed, not fixed:** `housing_first`'s 6 CSV-matched rows are individual academic
  researchers (CIHR grant PIs on Housing First RCTs), not service-delivery organizations
  — a property of the curated input CSV. Rendered faithfully as-is, since the two input
  files are explicitly not to be modified or re-curated.
