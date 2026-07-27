"""CRA lookup adapter -- national, same everywhere, used only to answer "is
this a registered charity."

Deliberately does NOT read data/t3010/identification_*.csv directly, and does
NOT go through the resolved `entities` table either. Two reasons:

1. `entities`/`entity_financials` in nonprofit_network.duckdb never persisted
   postal code (only city/province) -- but postal code is this module's
   primary blocking key, so that path can't supply it.
2. The raw identification_*.csv files are read through the same
   ignore_errors=true multi-year union load documented as having unresolved
   encoding/dialect row-drop issues (AGENTS.md open issue #1). Re-implementing
   a second ad hoc CSV reader here would risk quietly reintroducing that same
   class of bug in a second, less-hardened code path.

Instead this queries `raw_t3010_ident` -- the table analysis/build_entity_graph.py's
load_raw() already builds via the hardened, reject-counted _load_t3010_table()
loader, which does carry Postal Code/Category/Designation. Requires
nonprofit_network.duckdb to already exist (i.e. build_entity_graph.py has been
run at least once) -- this module has no independent CRA ingest of its own.
"""
from collections import namedtuple

import duckdb

from discovery.config import (
    DB_PATH, MONTREAL_ISLAND_MUNICIPALITIES, MONTREAL_FSA_PREFIXES, QUEBEC_PROVINCE_CODE,
)
from discovery.normalize import normalize_bn, normalize_postal, fsa

CraCharityRecord = namedtuple("CraCharityRecord", [
    "bn_root", "legal_name", "city", "province", "postal_code",
    "category", "designation", "status", "source_year",
])


def _pick_latest_per_bn(rows):
    """rows: iterable of (bn_raw, legal_name, city, province, postal_raw,
    category, designation, source_year). Keeps the latest source_year per
    normalized BN root -- same "latest year wins" rule
    build_entities_and_grants() uses for the main entity graph, so this
    module's notion of an org's current address/name matches the rest of the
    repo instead of drifting from it.

    status is a heuristic, not a real CRA field: identification_*.csv has no
    active/revoked flag, so a BN whose latest row isn't in the most recent
    year present is treated as no-longer-current. This is the same
    "deregistered before 2024" inference build_entities_and_grants() already
    relies on implicitly (its own comment: "including charities deregistered
    before 2024 that only appear in earlier years")."""
    latest = {}
    max_year = 0
    for bn_raw, legal_name, city, province, postal_raw, category, designation, source_year in rows:
        root = normalize_bn(bn_raw)
        if not root:
            continue
        max_year = max(max_year, source_year)
        prev = latest.get(root)
        if prev is None or source_year > prev[-1]:
            latest[root] = (legal_name, city, province, postal_raw, category, designation, source_year)

    out = []
    for root, (legal_name, city, province, postal_raw, category, designation, source_year) in latest.items():
        status = "active" if source_year == max_year else "revoked_or_inactive"
        out.append(CraCharityRecord(
            bn_root=root, legal_name=legal_name, city=city, province=province,
            postal_code=normalize_postal(postal_raw), category=category,
            designation=designation, status=status, source_year=source_year,
        ))
    return out


def _in_montreal(rec):
    city = (rec.city or "").strip().upper()
    if city in MONTREAL_ISLAND_MUNICIPALITIES:
        return True
    f = fsa(rec.postal_code)
    return bool(f and f[:2] in MONTREAL_FSA_PREFIXES)


def _in_quebec(rec):
    return (rec.province or "").strip().upper() == QUEBEC_PROVINCE_CODE


def load_cra_records(db_path=None, region="quebec"):
    """Read-only query against nonprofit_network.duckdb's raw_t3010_ident.
    Raises duckdb.IOException if a writer (e.g. a build_entity_graph.py rebuild)
    currently holds the lock -- that's a genuine "try again later" condition,
    not something to silently swallow.

    region: "quebec" (default, province-wide via CRA's own Province field),
    "montreal" (the original pilot's narrower island-only subset), or None/
    falsy for no region filter at all (every charity in the country -- not
    useful against a Quebec-only discovery source like REQ, but left
    available for a future national discovery source)."""
    con = duckdb.connect(db_path or DB_PATH, read_only=True)
    try:
        rows = con.execute(
            'SELECT BN, "Legal Name", City, Province, "Postal Code", Category, Designation, source_year '
            "FROM raw_t3010_ident"
        ).fetchall()
    finally:
        con.close()
    records = _pick_latest_per_bn(rows)
    if region == "montreal":
        records = [r for r in records if _in_montreal(r)]
    elif region == "quebec":
        records = [r for r in records if _in_quebec(r)]
    return records
