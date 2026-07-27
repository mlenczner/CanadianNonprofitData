# Canadian Non-Charity Nonprofit Discovery — Module Spec

## Goal

Enumerate the universe of Canadian nonprofits and, for each, determine two things:

1. **Charitable status** — is it a **registered charity** or a **non-charity nonprofit**? CRA is
   used only as a lookup to flag this, never as a discovery source.
2. **Social purpose** — among the non-charity nonprofits (the interesting set — orgs that never
   appear in CRA data), which ones have a **social purpose** and which don't?

The end objective is national. But the discovery source that makes this cheap in Québec (REQ) has
no free, open equivalent in most other provinces, so the method has to be built in a way that
scales jurisdiction by jurisdiction rather than assuming one clean national feed.

**Therefore: Montreal (Québec) is the pilot.** Prove the full pipeline — discovery → charity flag
→ social-purpose flag → validation — on the one jurisdiction where discovery is a single free feed,
then generalize. Everything below is written so the Québec pilot is a concrete instance of a design
that isn't Québec-specific.

## Where it lives

A new module inside the existing personal analysis repo — **not** a separate codebase.
Rationale: the matcher joins the discovery source against the same CRA data the repo already loads
and cleans; a separate repo would duplicate (or force a shared library for) exactly that CRA loader
plus the name-normalization logic, and require shuttling CRA snapshots between repos. Keep the
internal steps modular so it can be extracted later if that ever changes.

```
discovery/
  config.py          # thresholds, region/jurisdiction filter, snapshot paths
  ingest/
    base.py          # discovery-source interface (see "Discovery as a pluggable layer")
    req.py           # Québec pilot: parse REQ open data; NPO legal form + region filter
    cra.py           # thin adapter over the repo's existing CRA loader (national lookup)
    grants.py        # federal Grants & Contributions ingest (social-purpose signal)
  normalize.py       # shared name + address normalization (used by all sources)
  block.py           # candidate blocking: postal -> FSA -> city
  match.py           # scoring, thresholds, 1:many resolution, bucketing
  classify.py        # charity status + social-purpose status from match results
  output.py          # write flagged dataset + review queues
  run.py             # orchestrate the pipeline end to end
  tests/
  data/              # discovery + CRA + G&C snapshots (gitignored)
```

## Data sources

### Discovery (per-jurisdiction — this is the part that doesn't generalize for free)

**Québec pilot — REQ open data** (Données Québec, "Registre des entreprises").
Fields we rely on: NEQ, legal name, **other/trade names the org operates under**, establishment
address (street + postal), legal form. Filter: legal form = *personne morale sans but lucratif*.
The trade-name field is important — it's half the solution to the legal-vs-operating-name problem.

REQ is the reason Québec is the pilot: it's free, open, machine-readable, and exposes the NPO
legal form directly. Most other provinces have no equivalent (see "Scaling beyond Québec").

### Lookup (national — same everywhere)

**CRA List of Charities** (already in the repo). Confirmed to include **full address + postal
code**, which makes postal-code blocking viable. Key fields: BN, name, address, postal, city,
designation, status (active/revoked), category. This half is national and does not change as the
pilot expands — only the discovery side is jurisdiction-specific.

### Social-purpose signal (national, positive-only)

**Federal Grants & Contributions** (proactive disclosure, open.canada.ca). A nonprofit appearing as
the recipient of a grant/contribution under an **explicitly social federal program** is strong
positive evidence of social purpose. National, free, structured. Its role and limits are defined in
"Social-purpose classification" — critically, it is a one-directional signal.

**The core constraint (applies to every join):** the datasets share no common key — CRA uses BN,
REQ uses NEQ, G&C identifies recipients by name only. Every join in this system is therefore fuzzy
(name + address). This single fact drives the blocking + scoring + review-bucket design, and it
applies equally to the CRA lookup and the G&C social-purpose match.

## Discovery as a pluggable layer

Because discovery is the only part that changes per jurisdiction, `ingest/base.py` defines a thin
discovery-source interface: given a jurisdiction config, yield normalized org records with the same
shape regardless of source — `{source_id, legal_name, trade_names[], address, postal, city,
legal_form, jurisdiction, source_snapshot_date}`. REQ (`req.py`) is the first implementation.
Everything downstream (normalize → block → match → classify) consumes this shape and is
source-agnostic. Adding a province means writing a new ingest adapter, not touching the matcher.

## Scaling beyond Québec

Discovery outside Québec is a patchwork, not a feed. Honest catalog of the options:

- **Corporations Canada / CNCA** (federal not-for-profit corporations) — the one open,
  cross-province source, free and machine-readable. But it covers only **federally-incorporated**
  nonprofits, a minority slice; provincially-incorporated orgs are absent. Good national floor,
  not a complete picture.
- **Provincial corporate registries** — quality ranges widely: some offer partial open data, many
  are search-only or paywalled per-record, and several don't cleanly flag nonprofit legal form.
  Several provinces have **no free open-data discovery source at all** — a real limit to state
  plainly, not engineer around.
- **CRA lookup is unaffected** — it's already national, so as new discovery sources come online the
  charity-flag logic is reused unchanged.

Design consequence: national coverage will be **uneven and source-dependent**, and every row should
carry its `jurisdiction` and `discovery_source` so coverage gaps are explicit in the output rather
than hidden. Expansion order should follow discovery-source availability (federal CNCA floor first,
then provinces with usable open data), not population.

## Pipeline

1. **Ingest discovery source** (REQ for the pilot) → filter to NPO legal form and the target region
   → dedupe multiple establishments per org (keep primary address, retain others for matching).
2. **Ingest CRA** → target region, active + revoked (revoked still answers "is it a charity").
3. **Ingest federal G&C** → recipients under programs labeled social (see social-purpose stage).
4. **Normalize** all sides (see rules).
5. **Block** candidate pairs by postal code.
6. **Match & score** discovery↔CRA (charity flag) and discovery↔G&C (social-purpose signal).
7. **Classify** each org: charity bucket, then social-purpose bucket.
8. **Output** the flagged dataset + review queues.

## Normalization rules

Apply to discovery legal name, every trade name, the CRA name, and G&C recipient names:

- lowercase; strip accents (é→e); collapse whitespace
- drop legal suffixes: inc., ltée, ltd., corp., enr.
- standardize Saint/St/Ste; expand common abbreviations
- strip punctuation
- keep a raw copy alongside the normalized copy for the output/audit trail

Addresses: normalize postal code to 6 chars no space (`H2X1Y4`); derive FSA (first 3).

## Blocking

Because a full discovery × CRA cross product is millions of pairs, only score pairs that share a
blocking key. Cascade so it degrades gracefully:

1. **Postal code** (primary — viable because CRA extract has it)
2. **FSA** (first 3 chars) for rows where postal doesn't produce a candidate
3. **City** as last resort (large block; name score must carry it)

> **Note (missing/malformed address).** The cascade assumes every discovery row has at least a
> usable city. A row with no usable postal, FSA, *or* city has no blocking key and will otherwise
> drop out silently or fall into an oversized city block — either way it risks being labeled
> `non_charity_nonprofit` by accident. Since the whole trust argument rests on never silently
> mislabeling a miss as non-charity, make this a rule, not an accident: **any discovery row with no
> usable blocking key routes directly to `needs_review`.** Track the count of such rows as a data-
> quality signal on each run. (Note: G&C recipient data is often address-poor, so the discovery↔G&C
> match will lean harder on name blocking and should carry a lower auto-accept confidence.)

## Matching & scoring

- Fuzzy score = `token_set_ratio` (RapidFuzz) of the CRA name against **both** the discovery legal
  name **and each trade name**; take the **max**. This is what catches "operates as X, registered
  as Y." The same approach scores discovery↔G&C recipient names.
- Optional address-similarity tiebreaker when two candidates score similarly on name.
- **1:many:** if a block has multiple candidates, take the best; retain the runner-up and its score
  so close calls surface in review.

**Thresholds** (starting points — calibrate on a labeled sample, don't trust these blind):

- score ≥ 90 → auto-match
- 75–89 → needs review
- < 75 → no match

## Classification — Stage 1: charitable status (three buckets, not two)

- **registered_charity** — confident CRA match → attach BN, CRA name, status, category.
- **non_charity_nonprofit** — no CRA candidate above the no-match floor.
- **needs_review** — mid-band score, or multiple close CRA candidates in the block.

The review bucket is deliberate: a fuzzy *miss* must not be silently mislabeled non-charity. That's
the main threat to the trustworthiness of the whole output.

## Classification — Stage 2: social purpose (three buckets, on the non-charity set)

Runs on the `non_charity_nonprofit` set — that's the population of interest. Same three-bucket
shape as Stage 1: **social_purpose / not_social / needs_review**.

**Core asymmetry (this shapes the whole stage).** Every available signal is *positive-only*: a hit
is good evidence an org *has* social purpose, but no signal establishes the *absence* of social
purpose. The overwhelming majority of genuinely social nonprofits are small, provincially/municipally
or foundation-funded, or simply never appear in any structured signal — so signal-absence is not
evidence of not-social. Consequently:

- Positive signals can **auto-promote** an org to `social_purpose`.
- **No signal, or signal-absence, can auto-label `not_social`.** The `not_social` bucket is a
  *residual*, assigned only after positive signals + review — and it needs its own labeled-sample
  validation, because it's the class most prone to error.

**Signals (positive indicators, precision-oriented):**

- **Federal G&C recipient of a social program** — primary signal. Requires first labeling federal
  *programs* as social/not (its own definitional task, kept in `config.py`). Federal-only, so it
  misses provincial/municipal/foundation grants. Matched by the same fuzzy name+address join as
  CRA, so it inherits that lossiness. A confident match to a social-program grant → auto-promote.
- **Secondary signals** — discovery-source activity/legal descriptors; name + mission text; whether
  the org has a findable website/mission statement. Used to corroborate, and to route ambiguous
  cases to review.

Everything not positively flagged and not resolved in review falls to `not_social` as the residual,
explicitly labeled low-confidence.

## Output schema

> **Note (treat as provisional).** This schema — and the normalization ruleset — are design
> commitments made before seeing real discovery data. Expect them to shift once ingest runs: in
> particular whether REQ trade names come through cleanly as a repeatable field, whether CRA postal
> coverage is as complete as "confirmed" implies, and how address-poor the G&C recipient data is.
> Lock the schema after the first real ingest + count sanity-check (build step 2), not before.

`source_id (neq/etc.), jurisdiction, discovery_source, legal_name, trade_names, address, postal,
city, legal_form, charity_status, matched_bn, matched_cra_name, charity_match_score,
charity_runner_up_score, social_status, social_signal, social_match_score, review_flag,
discovery_snapshot_date, cra_snapshot_date, grants_snapshot_date`

Files: the full flagged dataset, plus two review queues (charity `needs_review` and social-purpose
`needs_review`).

## Validation

Validate each classification stage separately, since they have different error costs.

- **Charity flag:** hand-label a random sample (~100 discovery orgs) as charity / non-charity, run
  the matcher, measure **precision and recall** on the charity flag, tune thresholds to the error
  you care about (false "non-charity" labels are the costly ones). Report the confusion matrix.
- **Social-purpose flag:** separate labeled sample drawn from the non-charity set. Because the
  positive signals are precision-oriented and `not_social` is a residual, pay special attention to
  the residual's error rate — that's where mislabeling concentrates.

Re-run whenever a new discovery, CRA, or G&C snapshot lands.

## Known risks

- **Fuzzy match error** — mitigated by postal blocking + trade-name matching + the review bucket + calibration. Applies to CRA and G&C joins alike.
- **Snapshot skew** — discovery, CRA, and G&C are dated snapshots; an org can be registered/revoked or receive a grant between them. Record all relevant snapshot dates in every row.
- **Region/jurisdiction definition** — "Montreal" is ambiguous (island vs. agglomeration vs. CMM). Pin it down in `config.py` (city list and/or FSA set) and state the choice explicitly. The same config carries the jurisdiction filter as the pilot expands.
- **Uneven national coverage** — outside Québec, discovery is source-dependent and incomplete (federal-only CNCA, patchy/paywalled provincial registries, some provinces with no free source). Coverage gaps must be visible in the output via `jurisdiction` + `discovery_source`, not hidden.
- **Trade-name / recipient-name gaps** — not every org lists its operating name, and G&C recipient names are inconsistent; some legal-vs-operating misses are unavoidable → land in review, not silently mislabeled.
- **Missing/malformed discovery address** — rows with no usable blocking key can't be matched at all; route them to `needs_review` rather than defaulting them to non-charity (see Blocking note).
- **Social-purpose asymmetry** — no signal can establish *absence* of social purpose; treating signal-absence as `not_social` would mislabel most of the sector. The `not_social` bucket is a low-confidence residual and must be validated as such.

## Build sequence

**Phase 1 — Montreal pilot (charity flag)** ✅ done, then scaled to all of Quebec
1. Scaffold module + `config.py` (region filter, thresholds, paths) + discovery-source interface.
2. REQ ingest + NPO/region filter → sanity-check the count.
3. Normalization utils + unit tests.
4. Blocking.
5. Matching + bucketing (discovery↔CRA).
6. Stage-1 classification + output.
7. Validation on the labeled sample; tune thresholds. — Run for real against a 2026-07-01 REQ snapshot; found and fixed a real matcher bug (see AGENTS.md's Deviations note). Re-run province-wide (2026-07-16): 58,749 Quebec NPOs → 11,576 `registered_charity` / 44,440 `non_charity_nonprofit` / 2,733 `needs_review`.

**Phase 2 — social-purpose stage (on the non-charity set)** ✅ done, province-wide
8. Federal G&C ingest + social-program labeling in `config.py`.
9. Discovery↔G&C matching → social-purpose auto-promote; add secondary signals.
10. Stage-2 classification + second review queue.
11. Validation on a separate labeled sample (focus on the `not_social` residual). — Run province-wide (2026-07-16) on the 44,440 non-charity set: 150 `social_purpose` / 484 `needs_review` / 43,806 `not_social`. Scaling this stage from the Montreal pilot to province-wide surfaced an unblocked-matching performance bug (fixed: see AGENTS.md's Deviations note) before this run was even possible in reasonable time.

**Phase 3 — scale beyond Québec**
12. Add Corporations Canada / CNCA discovery adapter (federal floor, all provinces).
13. Add provincial adapters where free open data exists; document jurisdictions with no free source.
14. Re-validate per jurisdiction; surface coverage gaps in the output.

**Later** — contact enrichment (website / email / verified street address).
