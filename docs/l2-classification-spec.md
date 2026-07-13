# Spec: Level 2 Service Classification ("what does every org do")

**Deliverable:** `analysis/classify_l2.py` — an LLM classification pipeline that assigns
Candid PCS subject codes to federal grant descriptions, with per-classification receipts,
explicit abstention, resumability, and a hard cost cap. Plus tests and an org-level rollup.

**Context.** Decision recorded in the vault Build Plan (2026-07-12): three-level taxonomy —
L1 = CRA sector codes (held), **L2 = Candid PCS** (this spec), L3 = named evidence models
(precision-first, separate pipeline). Fit test: naive substring matching already tags 44.6%
of substantive grant descriptions with a PCS term; this pipeline is the real version.
Governing rule from Mike: classify only what has a reasonable chance of being accurate —
**abstention is a first-class output, never a failure.**

## The cost-shape insight (build around this)

Do NOT classify 1.17M grants. Classify **distinct description texts**. Federal descriptions
are heavily templated ("Not a Project (Mandated or Core Funding)" appears tens of thousands
of times). Pipeline: dedupe latest-amendment grants → normalize text (trim, collapse
whitespace) → group by exact distinct `description_en + prog_name_en` text → classify each
distinct text ONCE → propagate to all grants sharing it. Expect the distinct-text count to
be a small fraction of the grant count, with frequency heavily skewed — classify in
descending order of grant-count-covered so early spend covers the most rows.

## Inputs

- `grants.csv` via DuckDB (AGENTS.md rules apply): latest-amendment dedup by
  `(TRIM(owner_org), TRIM(ref_number))` — same SQL as `raw_grants_latest`.
- `PCS_Taxonomy_Definitions_2024.xlsx` (repo root; move to `evidence/`). Use the Subject
  sheet: PCS Code, level names, and the definition column. Subjects only in v1 —
  no Population/Strategy facets yet.
- Anthropic API key from `ANTHROPIC_API_KEY` env var. Fail at startup with a clear message
  if unset. Model: `claude-haiku-4-5-20251001` for the main run.

## Classification call design

- **One call per distinct text.** System prompt contains the full PCS subject taxonomy
  (code, full hierarchy path, definition — trimmed to ~1 sentence each) and the
  instructions; mark it with a **prompt-caching cache breakpoint** so the taxonomy tokens
  are cached across calls (this is what makes the economics work). User message = the
  grant text only.
- **Strict JSON output** (use structured outputs / tool-use schema if available in the SDK,
  else parse defensively):
  `{"codes": [<=2 PCS codes], "confidence": "high"|"medium"|"abstain", "quote": "<verbatim substring of the input text>", "rationale": "<<=15 words>"}`
- **Instructions must say:** choose the most specific level that the text actually
  supports; max 2 codes; if the text is boilerplate, administrative, or too vague to
  support any code, return `abstain` with empty codes; the quote must be the exact span
  that justifies the classification.
- **Enforce the quote mechanically:** after each response, verify `quote` is a verbatim
  substring of the input text (case-insensitive, whitespace-normalized). If not, downgrade
  the result to `abstain` and record `quote_failed=true`. A classification without a
  verifiable receipt does not exist. This rule is a test case.
- Validate codes against the taxonomy; unknown codes → abstain + `bad_code=true`.

## Pipeline behavior

- `--dry-run`: print distinct-text count, planned call count, estimated token usage and
  cost (state assumptions), classify nothing.
- `--limit N`: classify only the top-N distinct texts by grants covered (pilot mode).
- **Hard cap:** `--max-calls N` (required, no default unlimited). Stop cleanly at the cap.
- **Resumable:** results stored keyed by `(sha256 of normalized text, prompt_version)` in
  `evidence/l2_classifications.duckdb` (a new file — do NOT write into
  nonprofit_network.duckdb). Already-classified texts are skipped on rerun. Bump
  `PROMPT_VERSION` constant whenever the prompt changes; old results stay, keyed separately.
- Rate-limit / retry with backoff on API errors; a failed call after retries is recorded
  as `error`, not abstain, and is retried on next run.
- Tables:
  - `l2_text_classifications` — text_hash, text (first 500 chars), n_grants_covered,
    codes, confidence, quote, rationale, flags, model, prompt_version, ts.
  - `l2_grant_classifications` — VIEW joining hash back to (dept, refnum) pairs.
  - `l2_org_rollup` — VIEW: recipient org × PCS code, n_grants, total dollars, best
    confidence, one example receipt (quote + ref_number). Use recipient_legal_name +
    business_number as the org key (entity_id join is a later enhancement).

## Pilot & QA gate (do not skip)

1. Run `--dry-run`, report numbers.
2. Pilot: `--limit 1000 --max-calls 1100`.
3. **Housing benchmark:** the 394 rows in `evidence/seed-classifications-housing-canada.csv`
   are independently-derived labels. Map the four housing categories to their PCS codes
   (look them up in the Subject sheet — homeless services / housing subjects; record the
   mapping in the report). For benchmark orgs' grants that the pilot classified, report
   agreement: % where the LLM's code matches or is an ancestor/descendant of the mapped
   code. Disagreements listed with both labels + the LLM's quote.
4. Write `docs/l2-pilot-report.md`: call counts, cost actuals vs. estimate, confidence
   distribution (high/medium/abstain %), housing-benchmark agreement, and 25 random
   high-confidence classifications with their quotes for human review.
5. **STOP after the pilot.** Do not scale to the full distinct-text set until Mike has
   reviewed the pilot report. The scale run is a separate instruction.

## Tests (pytest, `tests/test_classify_l2.py` — no network, no API key needed)

Inject a fake client (constructor/parameter injection) returning canned responses. Cover:
- Distinct-text dedup: identical descriptions on different grants → one classification
  propagated to both.
- Quote enforcement: canned response with a non-substring quote → recorded as abstain
  with `quote_failed=true`.
- Unknown PCS code → abstain with `bad_code=true`.
- Abstain response → stored as abstain, no codes.
- Resume: second run with same fixture skips already-classified hashes (fake client
  call-count assertion).
- `--max-calls` stops at the cap.
- Rollup view math on a small fixture (org with 2 grants, 2 codes).
- Malformed JSON from the fake client → error status, not a crash.
Fast, offline, tmp dirs only. The real-API path is exercised only by the pilot itself.

## Cost & safety rails

- State the cost estimate in the dry-run using current published Haiku pricing; do not
  hardcode stale prices silently — put the assumed rates in the output.
- Never log full API responses with the key; never commit the key; add
  `l2_classifications.duckdb` to git (it's small and is the product) but confirm size
  (<50MB) before committing.
- The classifications are DATA, not published claims. Site integration (which orgs show
  where) stays governed by the evidence-site spec's rules and is out of scope here.

## Definition of done

- [ ] `analysis/classify_l2.py` with the CLI above; `pyyaml`/`openpyxl`/`anthropic`
      pinned in requirements.txt.
- [ ] Full test suite green (existing + new), no network in tests.
- [ ] Dry-run numbers reported; pilot (1,000 texts) executed; `docs/l2-pilot-report.md`
      written with the housing benchmark and the 25-sample review table.
- [ ] STOPPED before any full-scale run.
- [ ] One line in README + AGENTS.md; Decisions note appended here for anything the spec
      didn't cover.

## Non-goals

No website scraping, no L3/named-model claims, no full-corpus run (pilot only), no site
changes, no Population/Strategy facets, no entity-graph writes, no rebuilds.

## Decisions

Choices made while implementing where this spec didn't specify, smallest-reasonable-choice
rather than asking (except where noted as a direct follow-up instruction):

- **No ANTHROPIC_API_KEY was available at all in this environment.** Per a direct follow-up
  instruction, added a second backend: a local Ollama model (`qwen2.5:7b`) over its
  OpenAI-compatible endpoint, selected with `--backend ollama`. `OllamaClient` mimics the
  *exact* minimal shape `classify_l2.py` already expects from the real Anthropic client
  (`client.messages.create(...)` -> an object with `.content[0].type`/`.input` and `.usage`),
  so `call_model()`, `parse_tool_response()`, `classify_one()`, and every bit of quote/code
  enforcement run completely unchanged regardless of backend -- `OllamaClient` is the only
  backend-specific code in the whole file. The pilot in `docs/l2-pilot-report.md` was run
  against this backend, not Anthropic's.
- **`--max-calls` is required even for `--dry-run`**, per an explicit instruction not to
  soften this boundary. Dry-run actually uses it too (as a cap on the planned-call count it
  reports), so it isn't a vestigial unused flag there.
- **`--dry-run` does not require `ANTHROPIC_API_KEY`** (only real classification calls do) --
  it makes zero API calls, so requiring a credential just to preview cost/volume numbers
  seemed like unnecessary friction the spec didn't ask for.
- **`PROMPT_VERSION` bumped 1 -> 2 for the Ollama pilot.** No Anthropic classification ever
  actually completed under version 1 (blocked on the missing key), but resume-skip logic
  only keys on `(text_hash, prompt_version)`, not model -- so a future Anthropic pilot must
  not silently inherit Ollama's results under the same version number. Version 1 stays
  reserved/unused.
- **The Ollama backend uses a condensed taxonomy** (code + full hierarchy path, no per-code
  definition sentence) -- roughly 75% smaller (~61K -> ~16K estimated tokens). Ollama has no
  prompt caching (an Anthropic-specific optimization), so a local 7B model re-processes the
  entire system prompt from scratch on every single call; the full definition-annotated
  version (economical under Anthropic's caching) was impractically slow there. The hierarchy
  path is kept because it's what disambiguates similarly-named leaf codes -- only the prose
  definition is dropped, and only for this backend.
- **Ollama's OpenAI-compatible endpoint silently ignores an inline `options.num_ctx`
  override** -- verified directly: a raw request with `"options": {"num_ctx": 32768}` came
  back with `prompt_tokens` far lower than the actual prompt length implied, consistent with
  silent truncation to a much smaller default context. Fixed by having `OllamaClient`
  self-provision a derived model tag (via Ollama's native `/api/create`, e.g.
  `qwen2.5:7b-ctx32768`) with `num_ctx` baked in via `PARAMETER`, and using that tag for the
  actual wire request -- while still recording the base model name (`qwen2.5:7b`) in the DB,
  since it's the same weights either way and the derived tag only exists to work around a
  context-configuration gap.
- **A local model can omit optional-seeming JSON keys entirely, not just send them empty.**
  Observed directly: `qwen2.5:7b` sometimes omitted `codes`/`quote`/`rationale` altogether on
  a genuine abstain response rather than sending `[]`/`""`. `parse_tool_response()` now
  defaults an *omitted* key to its zero-value instead of raising -- this is parser leniency,
  not weaker enforcement: the same mechanical quote-verbatim and code-validity checks still
  run in full against whatever value results (an omitted, non-abstain quote defaults to `""`
  and then fails the verbatim-substring check exactly like a wrong quote would, downgrading
  through the ordinary `quote_failed` path rather than a special case). A present-but-wrong-
  typed value still raises as malformed.
- **Housing benchmark: a `ref_number` matching more than one distinct `owner_org`** in
  `l2_grant_text_map` is skipped rather than guessed. `ref_number` is not globally unique in
  `grants.csv` (a known, unrelated data-quality issue elsewhere in this project: ~24,851 refs
  collide across departments), and the benchmark CSV doesn't carry the raw `owner_org` needed
  to disambiguate which of the colliding grants it actually meant. An earlier version of this
  function joined on `ref_number` alone and silently picked whichever colliding row came back
  first -- caught because it produced dozens of near-identical "disagreements" that were
  really the same unrelated grant's classification repeated under different benchmark org
  names. Regression tests added.
- **The pilot report's disagreement table groups by the underlying (category, LLM codes,
  quote) pattern**, not one row per benchmark org. A single heavily-templated boilerplate
  text (exactly the phenomenon this pipeline's cost-shape design is built around) can be
  shared by dozens of distinct benchmark orgs -- confirmed in the actual pilot: one shared
  1970s-era "National Homelessness Initiative / SCPI" boilerplate description accounts for
  167 of 167 disagreements, all one root cause. Listing each org individually would make one
  disagreement look like 167 independent ones.
- **The housing-benchmark agreement percentage counts benchmark rows, not distinct grants.**
  The benchmark CSV can (and does) tag the same underlying grant/`ref_number` under more than
  one category -- confirmed: 168 ref_numbers are tagged both `emergency_shelter` and
  `supportive_housing`. Rather than silently deduping (which would require an arbitrary choice
  of which category "wins" for a dual-tagged grant), the report states this plainly and adds
  a per-category breakdown so the headline percentage is interpretable.
- **`codes`/`flags` are stored as comma-joined `VARCHAR`**, not a native DuckDB `LIST` type,
  for simple, dependency-free parsing by whatever reads this DB next (a future site
  integration, per the spec, is explicitly out of scope here).
- **The "classification method" text in each org-rollup receipt is a fixed literal string**
  ("matched on category terms in federal grant descriptions, latest-amendment deduped"),
  not derived from a per-row database column -- this describes how the *category
  classification itself* was produced (the pre-existing housing benchmark CSV), not the
  entity-resolution match method, which is a different, already-existing column.
- **Dedup/classification text = normalized `description_en + prog_name_en`**, joined with a
  single space, used both as the hash/dedup key and as the literal text shown to the model
  (i.e. the model sees the same string that was hashed, not a differently-formatted view of
  the same two fields).
- **The committed `evidence/l2_classifications.duckdb` excludes `l2_grant_text_map`.** The
  full working DB (with that table populated) was 553.76MB -- over the spec's 50MB commit
  ceiling -- almost entirely `l2_grant_text_map`'s ~1.09M rows (one per latest-amendment
  grant, needed for the `l2_grant_classifications`/`l2_org_rollup` views). That table is
  fully derivable from `grants.csv` in seconds (`build_grant_text_map()` rebuilds it via
  `CREATE OR REPLACE TABLE` on every run) and isn't itself "the product" the spec means to
  commit -- `l2_text_classifications` (1,000 rows, ~1MB) is. The committed file has only
  `l2_text_classifications`; `open_db()` already handles this gracefully (`CREATE TABLE IF
  NOT EXISTS` recreates `l2_grant_text_map` empty, and the views still resolve, just showing
  0 rows until the next real run repopulates it from `grants.csv`).
