# Canadian Nonprofit Data

Links federal Grants & Contributions (`grants.csv`), the CRA T3010 charity registry (`data/t3010/`), and Canada Council for the Arts grants (`data/canada_council_grants.csv`) into one entity graph in `nonprofit_network.duckdb`, built by `analysis/build_entity_graph.py`. See `docs/entity-resolution-methodology.md` for the matching approach and match-rate results.

## Rule: never read the raw data files directly

`grants.csv` (~2GB), `nonprofit_network.duckdb`/`.wal`, and everything under `data/t3010/` (48 files, 12 years × 4 kinds) are large. **Never `Read` them directly — always query via DuckDB aggregates**, e.g.:

```bash
.venv/bin/python -c "import duckdb; con = duckdb.connect('nonprofit_network.duckdb', read_only=True); print(con.execute('SELECT ... LIMIT 20').fetchall())"
```

## Table schemas (`nonprofit_network.duckdb`)

**`entities`** — one row per resolved organization
- `entity_id` INTEGER, `bn_root` VARCHAR (9-digit CRA business number root, nullable), `canonical_name` VARCHAR, `city` VARCHAR, `province` VARCHAR, `entity_kind` VARCHAR (`charity` / `federal_dept` / `funder_org` / `other_org`)

**`entity_links`** — audit trail of every match decision
- `entity_id` INTEGER, `source_dataset` VARCHAR (`federal_gc` / `canada_council` / `t3010_qualified_donee` / `t3010_non_qualified_donee`), `raw_name` VARCHAR, `raw_bn` VARCHAR, `match_method` VARCHAR (`exact_bn` / `fuzzy_accept` / `unmatched_new`), `match_score` DOUBLE (nullable)

**`grants_unified`** — one row per grant/gift from any source
- `grant_id` INTEGER, `source_dataset` VARCHAR, `funder_entity_id` INTEGER, `recipient_entity_id` INTEGER, `amount_cad` DOUBLE, `fiscal_year` INTEGER, `program_name` VARCHAR, `description` VARCHAR (nullable)

**`entity_role_summary`** — per-entity give/receive totals and role
- `entity_id` INTEGER, `canonical_name` VARCHAR, `entity_kind` VARCHAR, `total_given` DOUBLE, `total_received` DOUBLE, `n_grants_given` INTEGER, `n_grants_received` INTEGER, `given_share` DOUBLE (nullable), `role` VARCHAR (`primarily_funder` given_share≥0.9 / `primarily_recipient` given_share≤0.1 / `dual_role` / `no_flows`)

**`entity_financials`** — T3010 line-code data joined to entities by BN root
- `entity_id` INTEGER, `bn_full` VARCHAR, `fiscal_period_end` DATE, `total_revenue` DOUBLE (line 4700), `total_expenditures` DOUBLE (4950), `total_expenditures_incl_disbursements` DOUBLE (5100), `total_gifts_to_qualified_donees` DOUBLE (5050), `revenue_from_federal_gov` DOUBLE (4540), `revenue_from_any_cdn_gov` DOUBLE (4570)
- ⚠️ See "multi-year dedup gap" below — currently has up to 12 rows per entity (one per fiscal year filed), not one.

## open.canada.ca WAF workaround

`open.canada.ca` rejects `urllib`'s default request signature but accepts `curl`'s. Both `analysis/download_sources.py`'s CSV downloads and its CKAN API calls (`package_search`/`package_show`) shell out to `curl -sL` rather than using `urllib.request` or `requests`. If adding new downloads from this domain, follow the same pattern.

## Open issues

1. **`entity_financials` multi-year dedup gap** (`analysis/build_entity_graph.py`, `build_entity_financials`). `raw_t3010_fin` now spans 2013-2024, but the join to `entities` has no year filter or dedup and doesn't even select `source_year` — so `entity_financials` produces up to 12 rows per entity instead of one, silently. Any query assuming one row per entity (e.g. comparing federal funding to reported revenue) will double- or 12x-count until this is decided: either filter to latest fiscal year per entity, or treat it as an explicit time series and update downstream queries accordingly.

2. **Silent row drops from `ignore_errors=true`.** The multi-year T3010 loads in `load_raw` (`identification_*.csv`, `qualified_donees_*.csv`, `non_qualified_donees_*.csv`, `financials_*.csv`) all use `ignore_errors=true` across 48 files spanning a form whose columns changed over the years (e.g. line codes 5045/5840-5843 only exist from 2023 onward). Malformed rows are dropped with no count logged anywhere. Before trusting match-rate numbers from the multi-year run, add per-file reject-row counts (DuckDB supports a `rejects_table` option on `read_csv`).

3. **Numeric-suffix fuzzy-match false positive** (confirmed, not hypothetical): `ALBERTA CIRCUIT 5A OF JEHOVAH'S WITNESSES` matched `Alberta Circuit 7A of Jehovah's Witnesses` at a 97.4 `token_sort_ratio` score — branch/circuit/chapter/district numbers barely affect the score in an otherwise-long, otherwise-identical name. Expanding T3010 to 12 years increases fuzzy-match volume against this known precision bug. Fix options: boost numeric-token weight in scoring, or require exact match on any numeric token before auto-accepting.
