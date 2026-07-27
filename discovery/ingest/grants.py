"""Federal Grants & Contributions ingest -- the Phase 2 social-purpose
positive signal. Queries `grants_unified`/`entities` in nonprofit_network.duckdb
rather than re-parsing grants.csv: build_entity_graph.py's load_raw() already
dedupes amendment rows to the latest per (owner_org, ref_number) before
grants_unified is built (AGENTS.md issue #3 -- raw grants.csv, summed naively,
inflates federal_gc's total by roughly 1.7x). Re-ingesting the raw CSV here
would silently reinherit that exact bug in a second, independent code path.

"Explicitly social federal program" is a definitional judgment call kept in
discovery/config.py (SOCIAL_PROGRAM_NAME_KEYWORDS) -- a starting keyword list,
not a validated taxonomy. Before growing that list by hand, check whether
analysis/classify_l2.py's Candid PCS subject-code classifications (once they
cover more than the current 1,000-text pilot sample) can answer "is this
program social" directly instead -- same classification problem, already
half-solved elsewhere in this repo.

SocialSignalCandidate carries city/province (from `entities`, same columns
discovery/ingest/cra.py uses for its own blocking) and a postal_code field
that's always None -- entities never persisted postal code for grant
recipients. That's deliberate, not an oversight: it makes this record shape
compatible with discovery/block.py's postal -> FSA -> city cascade as-is (the
postal/FSA stages simply always miss and it degrades straight to city-level
blocking), so run.py can block the Phase 2 social match the same way it
blocks the Phase 1 charity match instead of comparing every discovery record
against the full national candidate list -- confirmed necessary: unblocked,
scaling from the Montreal pilot to province-wide REQ data turned this into a
~44,000 x ~440,000 cross product that didn't finish in hours.
"""
from collections import namedtuple

import duckdb

from discovery.config import DB_PATH, SOCIAL_PROGRAM_NAME_KEYWORDS

SocialSignalCandidate = namedtuple("SocialSignalCandidate", [
    "entity_id", "legal_name", "program_name", "description", "fiscal_year", "amount_cad",
    "city", "province", "postal_code",
])


def _looks_social(program_name, description, keywords):
    text = f"{program_name or ''} {description or ''}".lower()
    return any(kw in text for kw in keywords)


def load_federal_social_signal_candidates(db_path=None, keywords=None, province=None):
    """Read-only query; returns federal_gc grants_unified rows whose
    program_name/description matches a social-program keyword, joined to the
    resolved entity's canonical_name (+ city/province) for name-matching and
    blocking in match.py/block.py.

    province: restrict to recipients resolved to this province (e.g. "QC") --
    filtered in SQL so the unmatched rows are never even pulled into Python.
    Pass None (default) for every province (e.g. a future national discovery
    source); the Quebec discovery pipeline always passes "QC" since REQ only
    covers Quebec entities in the first place."""
    keywords = keywords if keywords is not None else SOCIAL_PROGRAM_NAME_KEYWORDS
    con = duckdb.connect(db_path or DB_PATH, read_only=True)
    try:
        query = (
            "SELECT g.recipient_entity_id, e.canonical_name, g.program_name, g.description, "
            "g.fiscal_year, g.amount_cad, e.city, e.province "
            "FROM grants_unified g JOIN entities e ON e.entity_id = g.recipient_entity_id "
            "WHERE g.source_dataset = 'federal_gc'"
        )
        params = []
        if province:
            query += " AND e.province = ?"
            params.append(province)
        rows = con.execute(query, params).fetchall()
    finally:
        con.close()

    return [
        SocialSignalCandidate(entity_id, legal_name, program_name, description, fiscal_year, amount_cad,
                               city, prov, None)
        for entity_id, legal_name, program_name, description, fiscal_year, amount_cad, city, prov in rows
        if _looks_social(program_name, description, keywords)
    ]
