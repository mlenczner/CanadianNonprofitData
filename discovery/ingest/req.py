"""REQ (Données Québec "Registre des entreprises") ingest -- the Québec pilot
discovery source. Reads LOCAL CSV files only; this module never downloads
anything on its own.

The real REQ export is NOT one denormalized CSV -- it's several relational
files joined by NEQ (verified against a real 2026-07-01 export; the original
placeholder assumed a single flat file with columns like "Nom"/"Municipalité"/
"Code postal" -- see docs/montreal-discovery-spec.md's Deviations note):

- Entreprise.csv: one row per company. COD_FORME_JURI is a CODE, not text
  (resolved via DomaineValeur.csv -- see config.py's REQ_NPO_FORME_JURI_CODE).
  Address is 4 free-text lines (ADR_DOMCL_LIGN1_ADR..LIGN4_ADR). Postal code
  is its own clean field (LIGN4_ADR, e.g. "H2X1Y4") -- no parsing needed.
  City is NOT a separate field: it's whichever of LIGN2/LIGN3 is the last
  non-null line before the postal code -- normally LIGN2, but an intervening
  floor/suite/PO-box line (e.g. "3E ÉTAGE, TOUR OUEST", "CP 51") pushes the
  real city into LIGN3 for that row (confirmed against real sample rows).
- Nom.csv: one row per name. See config.py's REQ_NAME_TYPE_*/REQ_NAME_STATUS_
  CURRENT for what the type/status codes mean.

Both files are large (Entreprise.csv ~640MB, Nom.csv ~290MB for the full
Quebec registry) -- the whole NPO-filter + name-join runs as one SQL query
(~0.6s against the real files) rather than pulling either file into Python,
per AGENTS.md's "query large files via DuckDB aggregates" convention.
"""
import os
import re

import duckdb

from discovery.config import (
    REQ_ENTREPRISE_FILE, REQ_NOM_FILE, REQ_NPO_FORME_JURI_CODE, REQ_ACTIVE_STAT_IMMAT_CODE,
    REQ_NAME_TYPE_LEGAL, REQ_NAME_TYPE_TRADE, REQ_NAME_STATUS_CURRENT,
    MONTREAL_ISLAND_MUNICIPALITIES, MONTREAL_FSA_PREFIXES,
)
from discovery.ingest.base import DiscoveryRecord
from discovery.normalize import normalize_postal, fsa

# Matches a trailing "(Québec)"/"(QUÉBEC)"/" QC" province marker on a city
# line -- real rows use both a parenthetical French province name and a bare
# "QC" suffix (e.g. "Montréal (Québec)" vs "ST-VALLIER DE BELLECHASSE QC").
_PROVINCE_SUFFIX = r"\s*\(?\s*(QU[ÉE]BEC|QC)\s*\)?\s*$"


class ReqDataError(RuntimeError):
    pass


def _require_files(data_dir):
    for fname in (REQ_ENTREPRISE_FILE, REQ_NOM_FILE):
        path = os.path.join(data_dir, fname)
        if not os.path.exists(path):
            raise ReqDataError(
                f"Expected {fname} in {data_dir!r} (as shipped in the REQ open-data ZIP) -- not found."
            )


def load_req_records(data_dir, region_filter=True, snapshot_date=None):
    """data_dir: directory containing Entreprise.csv and Nom.csv (as shipped,
    unzipped, in the REQ open-data download)."""
    _require_files(data_dir)
    entreprise_path = os.path.join(data_dir, REQ_ENTREPRISE_FILE)
    nom_path = os.path.join(data_dir, REQ_NOM_FILE)

    con = duckdb.connect(":memory:")
    try:
        rows = con.execute(f"""
            WITH ape AS (
                SELECT NEQ, ADR_DOMCL_LIGN1_ADR AS lign1, ADR_DOMCL_LIGN2_ADR AS lign2,
                       ADR_DOMCL_LIGN3_ADR AS lign3, ADR_DOMCL_LIGN4_ADR AS lign4
                FROM read_csv('{entreprise_path}', all_varchar=true)
                WHERE COD_FORME_JURI = '{REQ_NPO_FORME_JURI_CODE}'
                  AND COD_STAT_IMMAT = '{REQ_ACTIVE_STAT_IMMAT_CODE}'
            ),
            names AS (
                SELECT NEQ,
                       MAX(CASE WHEN TYP_NOM_ASSUJ = '{REQ_NAME_TYPE_LEGAL}' THEN NOM_ASSUJ END) AS legal_name,
                       array_agg(DISTINCT NOM_ASSUJ) FILTER (
                           WHERE TYP_NOM_ASSUJ = '{REQ_NAME_TYPE_TRADE}' AND NOM_ASSUJ IS NOT NULL
                       ) AS trade_names
                FROM read_csv('{nom_path}', all_varchar=true)
                WHERE STAT_NOM = '{REQ_NAME_STATUS_CURRENT}'
                GROUP BY NEQ
            )
            SELECT c.NEQ, c.lign1, c.lign2, c.lign3, c.lign4, n.legal_name, n.trade_names
            FROM ape c
            JOIN names n ON n.NEQ = c.NEQ
            WHERE n.legal_name IS NOT NULL
        """).fetchall()
    finally:
        con.close()

    records = []
    for neq, lign1, lign2, lign3, lign4, legal_name, trade_names in rows:
        city = _pick_city(lign2, lign3)
        postal = normalize_postal(lign4)
        if region_filter and not _in_montreal(city, postal):
            continue
        records.append(DiscoveryRecord(
            source_id=neq,
            jurisdiction="QC",
            discovery_source="req",
            legal_name=legal_name,
            trade_names=tuple(trade_names or ()),
            address=lign1,
            postal_code=postal,
            city=city,
            legal_form=REQ_NPO_FORME_JURI_CODE,
            source_snapshot_date=snapshot_date,
        ))
    return records


def _clean_city(raw):
    if not raw:
        return None
    return re.sub(_PROVINCE_SUFFIX, "", raw, flags=re.IGNORECASE).strip() or None


def _pick_city(lign2, lign3):
    """City is whichever of LIGN2/LIGN3 is the last non-null line before the
    postal code (LIGN4) -- normally LIGN2, but an intervening floor/suite/
    PO-box line pushes the real city into LIGN3 for that row."""
    if lign3 and lign3.strip():
        return _clean_city(lign3)
    return _clean_city(lign2)


def _in_montreal(city, postal_code):
    city_u = (city or "").strip().upper()
    if city_u in MONTREAL_ISLAND_MUNICIPALITIES:
        return True
    f = fsa(postal_code)
    return bool(f and f[:2] in MONTREAL_FSA_PREFIXES)
