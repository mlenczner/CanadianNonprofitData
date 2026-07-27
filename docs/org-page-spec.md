# Spec: Organization Profile Pages ("claim and receipt")

**Deliverable:** `analysis/org_page.py` — a CLI that generates one beautiful, self-contained
HTML profile page per organization from `nonprofit_network.duckdb`, plus a test suite.

## The idea

Every page has two postures, toggled by the reader:

1. **The clean layer** (default). A quiet, elegant profile of one organization — the kind of
   page you'd show anyone. No jargon, no IDs, no methodology. Reads like a well-made
   profile card.
2. **The receipt layer.** Every fact on the page is a *claim*, and every claim carries a
   *receipt*: the raw record(s) it came from, how they were matched, and with what
   confidence. Faint dotted underlines mark claims; clicking one opens an inline evidence
   drawer. A single global toggle in the header — **"Show your work"** — highlights every
   claim at once.

The receipts are not new data. `entity_links` already records every match decision
(raw name, raw BN, match method, score, source dataset), and the raw tables retain
everything the clean tables deduplicate away (e.g. amendment chains in `raw_grants`).
This feature *surfaces* the audit trail the resolver already writes.

Honesty is a feature: where a fact rests on a fuzzy match, the receipt must show the
score and both name strings. Do not hide uncertainty to make pages look cleaner.

## CLI

```
python analysis/org_page.py "Salvation Army"            # fuzzy name lookup
python analysis/org_page.py --entity-id 12345
python analysis/org_page.py --bn 107951618
python analysis/org_page.py "salvation" --list          # print candidate matches, build nothing
```

- Name lookup: case-insensitive substring against `entities.canonical_name`, ranked by
  total flow (`entity_role_summary.total_given + total_received`). If multiple candidates
  and no exact match, print the top 10 with entity_id / kind / city / total flow and exit
  nonzero asking the user to pick (`--entity-id` or `--list` to browse). Never guess silently.
- Output: `docs/orgs/<slug>.html` (slugified canonical name; `--out PATH` to override).
  Create `docs/orgs/` if needed.
- Open the DB `read_only=True`. Fail with a clear message if the DB is locked or missing.
- Respect AGENTS.md: never read grants.csv or the t3010 CSVs directly — everything comes
  from DuckDB queries.

## Page anatomy (clean layer)

Match the visual language of the existing dashboards (`docs/grants-dashboard.html`):
same palette (`--red:#d52b1e`, warm off-white background, cards, system font stack),
single file, zero external dependencies, works offline, no localStorage, vanilla JS only.

1. **Header** — canonical name (English half if the stored name is bilingual
   `"English|Français"`), entity kind as a small badge (Registered charity / Federal
   department / Organization), city + province, BN root if known. The "Show your work"
   toggle lives here.
2. **Stat row** — big numbers: total received, total given (omit if zero), number of
   grants received/given, latest reported revenue + fiscal period (from
   `entity_financials`, omit if absent), years active (min–max fiscal_year seen).
3. **Funding timeline** — pure-CSS bar chart, per fiscal year. `given` is always a
   plain bar of dollars given (if the org gives). For `received`, a registered
   charity with a T3010 filing on record for that year gets a declared-vs-identified
   comparison instead of a plain bar, split into government (T3010 line 4570) and
   foundation/charity (line 4510) — see the Decisions section below for why. Every
   other year (non-charity entities; charity-years with no T3010 filing) falls back
   to the original plain received bar; one org's chart can mix both styles across
   its years. Hover tooltips.
4. **Grants received** — table: fiscal year, funder (canonical name), program, amount.
   Sorted newest first. Group visually by funder or year, whichever reads better.
   A small source badge per row (Federal G&C / T3010 gift / Canada Council).
5. **Grants given** — same shape, only if the org gives (regranters are the star case).
6. **Footer** — generated date, one-line data provenance, link text pointing at
   `docs/entity-resolution-methodology.md`.

If a section has no data, omit it entirely — no empty placeholders.

**Scale cap:** if an org has more than 300 grants in either direction, embed the 300
largest by amount plus a per-year rollup of the rest, and say so plainly
("Showing the 300 largest of 4,112 grants; totals include all of them").

## Receipts (what backs each claim)

| Claim on the page | Receipt drawer contents |
|---|---|
| The org's identity/name | All rows from `entity_links` for this entity: raw name, raw BN, source dataset, match method, score. Phrase it: "This organization appears under N name variants across M sources", then list them with method badges (exact BN / fuzzy 92.4 / unmatched-new). |
| Total received / given | How computed: sum over `grants_unified` rows, count per source dataset, plus the standing caveats: federal amounts are latest-amendment-per-agreement; T3010 qualified-donee gifts are filer-reported. |
| A single federal grant row | The matching row(s) from `raw_grants` — locate them by joining through this entity's `entity_links.raw_name` variants for `source_dataset='federal_gc'` against `recipient_legal_name`, then match on amount + fiscal year. Show ref_number, department, dates, description, and the **amendment chain** if the agreement was amended: each amendment number and value in order ("$1.0M → $1.4M → $1.2M — the profile shows only the final state"). If the raw row cannot be located unambiguously, say so in the drawer rather than showing a wrong receipt. |
| A T3010 gift row | Source dataset, filer BN, fiscal period end; note it is self-reported by the giving charity. |
| Latest revenue | `entity_financials`: BN, fiscal period end, which T3010 line codes (4700 etc.), and that only the latest filing year is kept. |
| Timeline bars | Per-year sums with per-source breakdown, shown in the bar's own hover tooltip rather than a separate drawer (no "Show your work" toggle needed to see it). For a declared-vs-identified year, the tooltip states the T3010-declared figure (`entity_financials_by_year`, line 4570/4510), the matched `grants_unified` total for that category, and the resulting percentage. Government identified totals fold in federal_gc agreements pro-rated evenly across the fiscal years they span, rather than attributed entirely to the agreement's start year — not separately called out in the tooltip itself, since the number is already the pro-rated one. |

Note: `grants_unified` does not store `ref_number`, so federal receipts require the
runtime join described above. Implement it as a best-effort lookup with an explicit
"receipt not located" fallback. Add a note to AGENTS.md open issues suggesting a
`source_ref` column in `grants_unified` at the next full rebuild — do **not** trigger a
rebuild for this feature.

Receipt drawers are pre-rendered into the HTML (hidden, expanded by JS) — no fetches,
the file stays self-contained. This is why the scale cap matters.

## Tests (pytest, in `tests/test_org_page.py`)

Build a **small fixture database in the test** (a temp-file DuckDB with `entities`,
`entity_links`, `grants_unified`, `entity_role_summary`, `entity_financials`, and a
minimal `raw_grants` — a dozen rows total, covering: one charity with an exact-BN link,
one fuzzy link with a score, one amendment chain of 3 rows, one bilingual pipe name,
one org with zero grants given). Do not depend on the real 1.6GB database for unit tests.

Cover at least:
- Name lookup: exact hit builds; ambiguous prefix exits nonzero and lists candidates.
- Generated HTML parses (use `html.parser`), contains the canonical name, the correct
  totals (assert on formatted numbers), and **no unreplaced template tokens**.
- The receipt for the fuzzy-linked variant contains the raw name and the score.
- The amendment-chain receipt shows all three values in order.
- Bilingual name renders the English half in the header but the full raw string in the
  identity receipt.
- Scale cap: an org with >300 fixture grants embeds exactly 300 rows + rollup note.
- Money/slug helper functions.

One integration test marked `@pytest.mark.skipif(not os.path.exists('nonprofit_network.duckdb'))`
that builds a real page for the top entity by total flow and checks it parses.

Tests must be fast (<10s), offline, and leave no files outside tmp dirs.

## Definition of done

- [ ] `analysis/org_page.py` with the CLI above, docstring explaining the two-layer design.
- [ ] All new tests + the existing suite pass via `.venv/bin/python -m pytest tests/`.
- [ ] Three sample pages generated and committed under `docs/orgs/`:
      The Salvation Army (regranter — both directions), one small single-source charity,
      and Prince Rupert Port Authority (formerly-split entity — its identity receipt
      should show the multiple raw variants now merged).
- [ ] Eyeball each sample: clean layer readable with receipts hidden; toggle reveals
      dotted underlines; every drawer opens; nothing overflows on a narrow window.
- [ ] README file table + AGENTS.md dashboards section updated with one line each.
- [ ] No network access at page-generation or page-view time.

## Non-goals

No server, no search index, no all-orgs directory page, no schema changes, no rebuild
of the entity graph. One org in, one file out.

## Decisions

Choices made while implementing where this spec didn't specify, smallest-reasonable-choice
rather than asking:

- **Grants tables grouped by fiscal year** (newest first), amount descending within each
  year. Reads better than grouping by funder/recipient for orgs with many counterparties,
  and matches the timeline chart's ordering.
- **Bilingual pipe names (`English|Français`) only** — the spec's normalization rule is
  scoped to the pipe format used in `grants.csv` recipient names. T3010 canonical names
  sometimes use other bilingual separators (e.g. a slash: `"...IN CANADA/CONSEIL DE
  DIRECTION DE L'ARMÉE..."`), which are left intact and displayed in full. A blanket
  split on `/` risked corrupting legitimate single-language names that happen to contain
  one (addresses, "A/B" style names), so it was left alone rather than guessed at.
- **Identity-receipt list also scale-capped, same as the grants tables.** The spec caps
  grants tables at 300 but doesn't mention the identity receipt's name-variant list. One
  real entity (The Salvation Army's national entity) has 8,883 distinct raw name
  variants across its ~45k linked records — rendering all of them made the page 5.8MB.
  Applied the same 300-cap-plus-rollup pattern, ordered by how many records each variant
  backs (most-linked first) rather than alphabetically, since that surfaces the variants
  that matter most.
- **`canada_council` and `t3010_non_qualified_donee` grant receipts show fields already
  in `grants_unified`** (program name, description) rather than doing a raw-table lookup,
  since the spec's receipts table only describes a raw-row lookup for `federal_gc` and
  `t3010_qualified_donee`.
- **Ambiguous name lookup:** exact case-insensitive match (after taking the English half
  of a bilingual name) auto-selects even when other candidates substring-match; a single
  substring match always auto-builds regardless of exact match.
- **Money formatting** mirrors `docs/grants-dashboard.html`'s convention ($B / $M / plain
  dollar figure) for visual consistency between the two features.
- **Grants tables wrapped in a horizontally-scrollable container** (`.table-scroll`)
  rather than clipped — an earlier draft used `overflow:hidden` on the table itself (for
  rounded corners) which silently clipped the rightmost column on narrow viewports found
  during the eyeball pass. The wrapper scrolls; nothing is hidden.
- **Drawers relocate to open inline next to their claim, not at the bottom of the
  document.** An earlier draft rendered every drawer inside a single
  `<div style="display:none">` at the end of the page for later JS placement — but an
  inline `display:none` on an ancestor overrides any descendant's own `display`, so
  toggling `.open` on a drawer inside it was a permanent no-op (independent review
  caught this; see `tests/test_org_page.py`'s `assert_drawers_are_reachable` /
  `test_drawers_not_trapped_in_hidden_wrapper`). Fixed by having `toggleDrawer()` move
  the drawer to right after the clicked claim on first open: after the claim's `<tr>`
  as a new `<tr><td colspan></td></tr>` for grants-table rows (a table row can only
  contain cells, so a bare sibling insert would get mangled into an anonymous cell), or
  via `insertAdjacentElement('afterend', ...)` after the closest `div`/`h1` otherwise.
  `.stats` switched from CSS grid to flex-wrap so an opened stat's drawer can force a
  `flex-basis:100%` line break directly beneath the row of stat boxes.
- **Sample-page filenames slug only the English half of `/`-separated bilingual names
  too** (the display decision above is unchanged — this is filename-only). Taking only
  the segment before the first `/` risks nothing for a filename the way it would for
  display text, and avoids 100+ character slugs for organizations like The Salvation
  Army's national entity.
- **Grants received/given split into three subsections (qualified donee, non-qualified
  donee, government), each showing 30 rows by default with a "Show more" toggle.** User
  request, not originally in the spec. The three subsections share one 300-row embed
  budget per direction rather than 300 each, in a fixed order (qualified donee first,
  since it's the largest category for most orgs) — an independent cap per category was
  tried first and found to multiply the per-row receipt-lookup query cost (confirmed:
  a several-minute runtime for a page that previously took seconds), since every
  embedded row costs a real DB query for its receipt drawer.
- **Org search index (`docs/orgs/index.html`) capped to the top 50,000 organizations by
  total flow by default.** Embedding the full ~533k-entity corpus inline as JSON
  produced a 105MB file — impractical to load in a browser and no longer meaningfully
  "self-contained." The cap is stated plainly on the page ("showing the top N of
  TOTAL"), not silently applied; `--limit` overrides it.
- **Funding timeline's received side compares T3010-declared revenue against
  identified `grants_unified` money, denominated on the *specific* T3010 line each
  category could plausibly be matched against — not total revenue (line 4700).**
  Total revenue includes individual donations, program fees, and investment income
  that were never going to show up as a matched grant regardless of matching
  quality, which would make a well-matched, donation-funded charity look
  artificially "unidentified." Split into government (line 4570) and
  foundation/charity (line 4510, "received from other registered charities")
  instead — each compared only against the `grants_unified` sources that could
  plausibly land on that line (`federal_gc`/`canada_council`/`otf` for government,
  `t3010_qualified_donee` for foundation).
- **The given side of the timeline does not get the same declared-vs-identified
  treatment, and never will without a different data source.** For money a charity
  gives away, the `t3010_qualified_donee` `grants_unified` rows are themselves
  derived from the *giving* charity's own T3010 donee schedule — comparing that sum
  to the same charity's own line 5050 would just be a self-consistency check of one
  filer's own form against itself, not a test of anything this project's matching
  does. `given` stays a plain bar, unconditionally.
- **Federal G&C agreements spanning multiple fiscal years are pro-rated evenly
  across their real `[agreement_start_date, agreement_end_date]` span, live at
  page-render time — not by changing `grants_unified`.** Checked directly against
  `raw_grants_latest`: the majority of federal G&C dollar value is multi-year
  ($164.0B in 2–4yr agreements, $405.5B in 4yr+, vs. $94.3B single-year), but
  `grants_unified` attributes an agreement's entire value to its start fiscal year
  — comparing that against a charity's smoothly-recognized T3010 revenue per year
  would be nonsense without pro-rating. `grants_unified`'s row grain was left alone
  rather than pro-rated at build time, since it also feeds `entity_role_summary`
  totals, the Grants Received table, `grant_search.py`, and
  `discovery/ingest/grants.py` — none of which should change. The pro-rated figure
  exists only inside the timeline chart's own fetch functions
  (`prorate_agreement_by_fiscal_year`, `fetch_federal_gc_prorated_by_year`), joining
  back to `raw_grants_latest` via the same `source_ref` key `locate_federal_receipt`
  already uses. A pro-rated fragment landing on a fiscal year with no other reason
  to appear on the chart (no T3010 filing, no naive `grants_unified` attribution
  either) is correctly invisible rather than shown as an orphan column — there's
  nothing for it to be compared against on that year.
