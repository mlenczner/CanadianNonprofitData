> **DRAFT — research prototype.** This is an unreleased working draft produced for research purposes only. Figures are derived from public data using experimental methods, contain known data-quality limitations, and have not been reviewed for publication. Do not cite, circulate, or rely on any figure or claim in this document.

# Canadian Nonprofit Data

Analysis of the Government of Canada's **Proactive Disclosure — Grants and Contributions** dataset: compliance gaps, data quality issues, and spending patterns across federal departments.

**Dataset:** [Grants and Contributions](https://open.canada.ca/data/en/dataset/432527ab-7aac-45b5-81d6-7597107a7013)  
**Search portal:** [search.open.canada.ca/grants](https://search.open.canada.ca/grants/)

---

## Repository structure

```
docs/       Documentation — data problems, research questions, and methodology
analysis/   Scripts for downloading, profiling, and linking the datasets
```

| Path | Description |
|------|-------------|
| [`docs/data-publishing-problems.md`](docs/data-publishing-problems.md) | Documented issues with missing mandatory fields, inconsistent data, and structural publishing limitations |
| [`docs/questions-and-insights.md`](docs/questions-and-insights.md) | Working list of research questions and possible insights |
| [`docs/entity-resolution-methodology.md`](docs/entity-resolution-methodology.md) | How funders/recipients are matched across sources, thresholds, and known limitations |
| [`analysis/profile_grants.py`](analysis/profile_grants.py) | `grants.csv` profiler — completeness, field distributions, and quality metrics |
| [`docs/grants-dashboard.html`](docs/grants-dashboard.html) | Self-contained interactive dashboard — totals, department grades, funding chart, curiosities (rebuild: `analysis/build_dashboard.py`) |
| [`docs/data-quality-rankings.html`](docs/data-quality-rankings.html) | Departments ranked best-to-worst on publishing quality, with per-department evidence (rebuild: `analysis/build_quality_report.py`) |
| [`analysis/download_sources.py`](analysis/download_sources.py) | Downloads the T3010 charity registry, Canada Council grants data, and Ontario Trillium Foundation grants data |
| [`analysis/build_entity_graph.py`](analysis/build_entity_graph.py) | Links grants.csv + T3010 + Canada Council + Ontario Trillium Foundation into one entity graph |
| [`analysis/org_page.py`](analysis/org_page.py) | Generates a self-contained "claim and receipt" HTML profile page per organization (rebuild samples: `docs/orgs/`, spec: `docs/org-page-spec.md`) |
| [`analysis/build_evidence_site.py`](analysis/build_evidence_site.py) | Generates the evidence-encyclopedia demo site (`docs/evidence/`) from `evidence/` — intervention pages with side-by-side registry ratings and Canadian org receipts (spec: `docs/evidence-site-spec.md`) |
| [`analysis/classify_l2.py`](analysis/classify_l2.py) | Assigns Candid PCS subject codes to distinct federal grant description texts, with mechanical quote/code enforcement and a hard cost cap (Anthropic or local-Ollama backend; pilot report: `docs/l2-pilot-report.md`, spec: `docs/l2-classification-spec.md`) |

---

## Data

Download the federal G&C CSV from [Open Canada](https://open.canada.ca/data/en/dataset/432527ab-7aac-45b5-81d6-7597107a7013) and place it at the repo root as `grants.csv`.

Neither `grants.csv` (~2 GB) nor the files under `data/` (T3010, Canada Council, Ontario Trillium Foundation) are tracked in this repository — they're either too large for GitHub or third-party downloads that shouldn't be vendored. Fetch them with `analysis/download_sources.py`. OTF's open grants data is [published at otf.ca](https://otf.ca/open) under the Open Government Licence – Ontario; the download URL was unreachable from a sandboxed environment while this pipeline was built, so `download_sources.py` prints manual-download instructions rather than failing if it can't be reached.

---

## Usage

Set up a virtualenv and install dependencies (needed for the linking pipeline; the profiler alone only needs the standard library):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Profile `grants.csv` on its own:

```bash
python analysis/profile_grants.py grants.csv
```

Download the T3010 charity registry, Canada Council, and Ontario Trillium Foundation grants data, then build the linked entity graph:

```bash
.venv/bin/python analysis/download_sources.py
.venv/bin/python analysis/build_entity_graph.py
```

This produces `nonprofit_network.duckdb` (gitignored) containing:

- `entities` — every resolved organization (charities, federal departments, Canada Council, Ontario Trillium Foundation, and unmatched orgs), one row each regardless of how many source datasets it appears in
- `grants_unified` — every grant/gift from all four sources, with funder and recipient both pointing to `entities`
- `entity_role_summary` — total given/received per entity and a `primarily_funder` / `primarily_recipient` / `dual_role` classification
- `entity_links` — audit trail of how each record was matched (exact BN / fuzzy name / unmatched)
- `entity_financials` — T3010-reported revenue/expenditures per entity

See [`docs/entity-resolution-methodology.md`](docs/entity-resolution-methodology.md) for how the matching works and its limitations.
