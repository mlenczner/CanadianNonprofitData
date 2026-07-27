# Spec: Bug fixes, search/UI improvements, and official-source deep links

Status: draft for implementation. Scope: `analysis/build_entity_graph.py`, `analysis/org_page.py`, `analysis/grant_search.py`, `analysis/webapp.py`, plus one new build step. Respect AGENTS.md conventions throughout (never read grants.csv / T3010 CSVs directly in the web layer; everything through `nonprofit_network.duckdb`).

All findings below were verified against the live database and a running webapp on 2026-07-18, with concrete entity/grant IDs. Do not treat them as hypothetical.

---

## Part A — Bug fixes (do these first; A1 gates everything else)

### A1. Name hygiene: HTML markup and entities in `canonical_name` are defeating entity resolution

**Verified problem.** 22 entities have literal HTML in `canonical_name`, e.g. entity 101428 is
`<span lang='fr' xml:lang='fr'>F&eacute;d&eacute;ration acadienne de la Nouvelle-&Eacute;cosse</span>`.
Because normalization never decodes this, "Fédération acadienne de la Nouvelle-Écosse" exists as **at least 4 separate entities**: 101428 (span-wrapped), 179278 (HTML-entity-encoded, no tags), 101557 (clean mixed case), 115442 (clean uppercase). Also `FÉDÉRATION CULTURELLE ACADIENNE DE LA NOUVELLE-ÉCOSSE` exists twice (111525, 129270) with byte-identical names.

**Fix.**
1. In `build_entity_graph.py`, add a pre-normalization step applied to every incoming raw name before any matching or canonical-name selection:
   - strip HTML tags (`re.sub(r'<[^>]+>', '', name)`)
   - decode HTML entities (`html.unescape`, applied repeatedly until stable — some sources double-encode)
   - collapse whitespace.
   The *display* canonical name keeps accents and case; the *matching* key additionally applies the existing unidecode/case folding.
2. Investigate why 111525/129270 (identical names) are separate entities — likely the `(normalized name, province)` residual-dedup key with one side blank-province (same mechanism as the known Prince Rupert Port Authority gap in AGENTS.md). If a name-identical pair differs only by one side having NULL province/city, merge them.
3. Rebuild the DB. After rebuild, assert and report:
   - `SELECT count(*) FROM entities WHERE canonical_name LIKE '%<%' OR canonical_name LIKE '%&eacute;%' OR canonical_name LIKE '%&amp;%'` → 0
   - the Fédération acadienne cluster resolves to 1 entity (or a documented residual with the reason)
4. Regression tests in `tests/`: normalization function handles span-wrapped, double-encoded, and tag-free-but-entity-encoded names; name-identical blank-province merge.

### A2. Probable double-count in T3010 qualified-donee gifts

**Verified problem.** CanadaHelps → Canadian Red Cross $3,224,402 (FY2024) appears as two rows in `grants_unified` (grant_ids 4637561 and 4642060, both `source_ref` NULL). It renders as a visible duplicate line on the Red Cross org page and inflates totals.

**Fix.** Investigate in `raw_t3010_qd`: is this one filing ingested twice (e.g. the same schedule appearing in two source files/years), or a re-filed/amended return? Then:
- If ingestion duplication: dedup at ingest on a natural key (funder BN, recipient BN/name, amount, fiscal year, source file line identity — determine empirically).
- If amended re-filings: apply the same latest-state-wins rule already used for federal amendment dedup.
- Quantify before/after: report how many rows and dollars the dedup removes, overall and for the top 20 affected orgs. Add a regression test.

### A3. HTML injection (stored XSS) in live search results

**Verified problem.** `webapp.py`'s `/orgs/search.json` and `/grants/search.json` return raw names/descriptions; both search pages render them via template-literal `innerHTML` with no escaping (`render()` in `render_orgs_search_page` and `render_grants_search_page`). The span-wrapped names in A1 inject real markup today; a hostile grant description is an XSS vector. Grant descriptions in the wild already contain `<` (26 rows verified).

**Fix.** Escape all server-supplied strings client-side before interpolation (small `esc()` helper: `&`, `<`, `>`, `"`, `'`), or build nodes with `textContent`. Apply to *every* interpolated field (`n`, `k`, `loc`, `f`, `t`, `p`, `y`, `amt`). This fix stands on its own even after A1 cleans the data — descriptions remain untrusted. Test: inject a fake row with `<img onerror>` payload through the render path and assert it appears as text.

### A4. Accent-blind search in a bilingual dataset

**Verified problem.** `/orgs/search.json?q=ecole` and `?q=école` return completely disjoint result sets. Also `_word_start_pattern`'s boundary class `[^a-zA-Z0-9]` treats accented letters as word boundaries, so "cole" word-start-matches inside "École".

**Fix.**
1. Add a `search_name` column to `entities` at build time: canonical_name → strip HTML (A1) → unidecode → lowercase → collapse whitespace. (unidecode is already a project dependency.)
2. In `search_orgs_live`, fold the incoming query the same way (unidecode + lowercase) and match against `search_name`. Word-boundary regex then operates in pure-ASCII space where `[^a-z0-9]` is correct.
3. Same treatment for grant-text search: fold query and match against an accent-folded expression (or precomputed column on `grants_unified` if the per-request `regexp_replace` cost is measurable).
4. Tests: `ecole` finds ÉCOLE POLYTECHNIQUE; `école` finds ECOLE-named orgs; "cole" does *not* match inside "École".

### A5. Result counts and pagination

**Verified problem.** The count line always reads "50 matches (showing first 50)" because the API caps at 50 — the true total is never computed. No way to see result 51.

**Fix.** Return `{"total": N, "results": [...]}` from both search endpoints (one extra `COUNT(*)` over the same WHERE — cheap relative to the existing scan). UI: "Showing 1–50 of 1,234" plus a "Show more" button that re-requests with `offset` (add `LIMIT ? OFFSET ?`). Keep payload shape change backward-compatible only if the static pages share this JS; otherwise just update both.

### A6. Relevance ranking

**Verified problem.** Ranking is purely `total_flow DESC`: searching "red cross" returns ICRC first, not the Canadian Red Cross.

**Fix.** Rank by match-quality tier, then flow within tier:
1. exact `search_name` match
2. `search_name` starts with query
3. any word starts with query (current behavior)

Implementable in SQL as a CASE expression on the folded columns. Test: "red cross" puts THE CANADIAN RED CROSS SOCIETY above ICRC; "l'arche" puts L'Arche Canada first.

### A7. Cold-start latency

First live search after boot took ~3s (link-manifest build on first request). Build the manifest (and discovery index) eagerly in `main()` before `app.run`, with a one-line timing log.

### A8. Retire the static org index

`docs/orgs/index.html` is a 10.7 MB page embedding 50k orgs and is superseded by the webapp. Stop regenerating it; leave a small redirect/tombstone page pointing at the live app (or delete, per repo owner's preference). Note it in README.

---

## Part B — Search/UI improvements

### B1. Unified search

One search page, one box; results in two labeled sections: **Organizations** and **What the money was for** (grant texts), each with its own "see all" link to the existing dedicated pages. The home page's two separate tiles remain but both lead to the unified box with the respective section focused. Rationale: "org vs grant text" is the repo's architecture, not the user's mental model.

### B2. Search state in the URL

Reflect `q`, filters, and active section into the query string (`history.replaceState` on input, debounced; read on load). Shareable/bookmarkable searches are core for the journalist/researcher audience. Applies to both search pages (or the unified page from B1).

### B3. Non-empty default state

Before any input, show the top ~20 organizations by total flow with a one-line caption, plus 3–4 example queries as clickable chips (e.g. "food bank", "Ontario Trillium Foundation", "housing"). Blank-page-until-typing is wasted first impression.

### B4. Plain-language filter labels

Current labels ("As a non-qualified donee") are T3010 jargon. Keep the precise term but add a muted one-line hint under each checkbox, e.g. "Received gifts from charities despite not being a registered charity itself". Write hints for all 6 category filters + the identity filter.

### B5. Timeline chart

- Add a y-axis: 2–3 gridlines with $ labels (max and midpoint suffice), so bars aren't purely relative.
- Tooltips currently exist only via CSS `:hover` on `<u>` — dead on touch, invisible to keyboard. Make each `barcol` focusable (`tabindex=0`) and toggle the tooltip on click/focus as well as hover.
- Under 640px the year labels are hidden entirely; instead show first/last year labels and every ~5th.
- Add a one-line explanation above the chart when declared/identified bars are present: "Declared = what the charity reported receiving on its T3010; identified = what this project found in funders' own records."

### B6. External-link affordance (used throughout Part C)

One consistent pattern for links leaving the site: text + `↗`, `target="_blank" rel="noopener noreferrer"`, muted color that turns red on hover. Add a `.ext` CSS class in `org_page.py`'s shared CSS.

---

## Part C — Deep links to official records

Goal: every claim's receipt should end in a link to the official source when one exists online. This extends the existing claim-and-receipt design — the drawer currently shows the raw matched record; it should also link out to the government's own copy.

### C1. Federal G&C → search.open.canada.ca record pages

- **Key available:** `grants_unified.source_ref` for `federal_gc` rows is `"{owner_org}|{ref_number}"` (e.g. `wd-deo|GC-WD-DEO-2021-2022-Q1-704`).
- **URL pattern (verified resolving 2026-07-18):** `https://search.open.canada.ca/grants/record/{owner_org},{ref_number}` (page is client-rendered; the route returns the record shell).
- **Where:**
  - every federal grant receipt drawer (`render_grant_receipt` / `_federal_receipt_from_row`): "View official record on open.canada.ca ↗"
  - every row of a grant-text detail page (`grant_search.py` detail tables) — the per-row link replaces or accompanies the existing ↗ that currently points at internal org pages
  - org-page grant rows for federal grants (in the receipt drawer, not the table row, to keep tables scannable).
- URL-encode `ref_number` (some contain spaces/slashes). If a row has no `source_ref`, omit the link silently.

### C2. Registered charities → CRA List of Charities (T3010 filings)

- **Key available:** `entities.bn_root` (9-digit) and, better, the full 15-character BN with RR suffix in `raw_t3010_ident.BN` (e.g. `119219814RR0001`). Carry the full BN onto `entities` at build time (new column `bn_full`, from the most recent ident row) rather than assuming `RR0001`.
- **URL pattern (verify at implementation time — CRA blocks automated fetches, could not confirm from sandbox):** `https://apps.cra-arc.gc.ca/ebci/hacc/srch/pub/dsplyBscInf?selectedCharityBn={bn_full}&dsrdPg=1`. If that pattern has changed, fall back to linking the charity search page. The basic-info page links to each year's T3010 filing, which is exactly the receipt trail we want.
- **Where:**
  - org page header meta line, next to the BN: "CRA charity listing ↗"
  - the identity receipt drawer (`render_identity_receipt`), alongside the BN match evidence
  - T3010 receipt drawers: link the *funder's* CRA listing (the filing is the funder's).

### C3. Quebec nonprofits → Registre des entreprises (REQ)

- **Key available:** `source_id` in `discovery/output/*.csv` for `discovery_source='req'` rows is the **NEQ** (e.g. 1142769836). Ensure `load_discovery_index()` carries `source_id` and `discovery_source` through to the webapp (extend the index value if it currently only holds the badge label).
- **Deep-link reality:** the REQ's état-de-renseignements pages are session-based and historically not stably deep-linkable. Implementation: attempt a direct link pattern if one now exists (check the current registre site manually); otherwise link to the REQ search page and render the NEQ prominently next to it with a copy-to-clipboard button: "NEQ 1142769836 ⧉ — look up in the Registre des entreprises ↗".
- **Where:** org page header badge area (the existing "Confirmed Quebec nonprofit" badge becomes/gains a link) and the identity receipt drawer.

### C4. Federally incorporated nonprofits → Corporations Canada

- **Key available:** `source_id` for `discovery_source='corporations_canada'` rows is the corporation number (e.g. 456926).
- **URL pattern (verify at implementation time):** `https://ised-isde.canada.ca/cc/lgcy/fdrlCrpDtls.html?corpId={source_id}`. Same placement as C3: badge + identity drawer.

### C5. Department links

- For each federal department (funder entities backed by `owner_org`):
  - "All records from this department on open.canada.ca ↗" → the grants search filtered by department. Verify the current filter-URL format of search.open.canada.ca/grants (it's a client-side app; the param may be e.g. `owner_org={slug}`); if no stable filtered URL exists, link the unfiltered search.
  - Department homepage: add a small curated mapping `owner_org → canada.ca URL` for the ~30 largest departments by record count (a dict in one module; no scraping). Render on the department's org page header. Departments outside the mapping just get the open.canada.ca link.

### C6. Organization websites

There is currently **no website field anywhere in the ingested data** (checked `raw_t3010_ident` and discovery CSVs). Plan:
1. **Check the source downloads first:** newer CRA "List of charities" / T3010 dataset versions include a website/URL column in some files — inspect what `download_sources.py` fetches before writing any new code. If present, ingest into a new `entity_websites` table (`entity_id`, `url`, `source`, `retrieved`).
2. REQ and Corporations Canada data may carry a website field per record — check the discovery ingest inputs likewise.
3. Render on the org page header as "Website ↗" when known. Normalize scheme (`https://` default), display bare domain.
4. Anything beyond registry-sourced websites (search-engine enrichment for top orgs) is **phase 2, out of scope here** — but design `entity_websites` so rows carry a `source` and can coexist.

### C7. Receipt drawer layout for official links

Inside each drawer, add a final "Official record" line after the existing match-evidence content, so the hierarchy reads: claim → our matched raw record → the government's own copy. Never make the official link the *only* content — the drawer's job is still to show our work.

---

## Part D — Acceptance & testing

1. All existing tests pass (`pytest`). New tests noted in A1–A6 are written test-first where a bug is being fixed (match repo convention of confirming a failing test against pre-fix code).
2. After DB rebuild: re-run the A1/A2 assertion queries; include the before/after entity count and dollar deltas in the PR/commit description.
3. Manual smoke pass of the webapp: search `école`/`ecole` (same results), `red cross` (Canadian Red Cross first, count line shows true total), an org page with federal + T3010 + discovery links (all three official-link types render), a grant-text detail page (row-level open.canada.ca links), department page links.
4. Official URL patterns: verify each of C1/C2/C4 (and C5's filtered-search URL) resolves in a real browser against 3 sampled records each; record the verified patterns in this spec. Government sites block non-browser fetches, so this check is manual/browser-based.
5. Performance guardrails: warm-start search under 0.5s (A7); org search with a category filter under 1s; no page regressions beyond current numbers noted in AGENTS.md.

## Sequencing

A1 → A2 (both need a rebuild; do one rebuild) → A3/A4/A5/A6/A7 (webapp layer) → B → C (C1 is pure webapp/render work and can go in parallel with B; C2 needs the `bn_full` column from the rebuild — fold it into the A1/A2 rebuild to avoid a third one) → A8 last.
