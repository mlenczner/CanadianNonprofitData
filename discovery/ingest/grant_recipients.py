"""Federal grant-recipient entities -- the candidate pool for matching REQ's
confirmed Quebec nonprofits against federal grant recipients that
analysis/build_entity_graph.py's Resolver already created an `entities` row
for but never linked to a real legal identity beyond whatever name the raw
grants.csv row itself carried.

Every federal_gc grant recipient gets *some* entity (exact_bn, fuzzy_accept
against a CRA charity, or an unmatched_new residual `other_org`) regardless
of what kind of organization it is -- Resolver.resolve()'s NFP-type
restriction (GRANTS_NFP_TYPES in build_entity_graph.py) only gates whether a
*fuzzy match attempt against the charity registry* happens, not whether an
entity gets created at all. Confirmed on real data: the `other_org` pool of
Quebec federal_gc recipients is NOT nonprofit-filtered -- it's dominated by
farms and ordinary businesses ("Ferme Agri-Valleyfield S.E.N.C.", "la Récolte
de la Rouge SENC"), not just missed nonprofits. Matching REQ's own
already-nonprofit-filtered list (COD_FORME_JURI='APE') against this pool is
what actually answers "which of these anonymous grant recipients are
confirmed legal nonprofits" -- something federal_gc's own data can't do
reliably by itself.

Deliberately excludes entity_kind='charity': those are already covered by
Phase 1's CRA-charity match (discovery/ingest/cra.py) and don't need this
stage.

These residual entities carry province only, never city or postal code (see
analysis.build_entity_graph.Resolver's residual branch, which only stores
province on EntityRow) -- confirmed here too, not assumed. That's why
discovery/block.py's name-prefix tier exists: postal/FSA/city blocking would
put all of one province's candidates in a single bucket otherwise.
"""
from collections import namedtuple

import duckdb

from discovery.config import DB_PATH

GrantRecipientCandidate = namedtuple("GrantRecipientCandidate", [
    "entity_id", "legal_name", "city", "province", "postal_code", "bn_root",
    "n_grants", "total_amount_cad",
])


def load_federal_grant_recipient_candidates(db_path=None, province=None, entity_kinds=("other_org",)):
    """Read-only query against nonprofit_network.duckdb. province: restrict to
    entities resolved to this province (e.g. "QC") -- filtered in SQL, same
    convention as discovery/ingest/cra.py and discovery/ingest/grants.py.
    entity_kinds: which entity_kind values to include (default other_org
    only -- see module docstring for why charity is excluded)."""
    con = duckdb.connect(db_path or DB_PATH, read_only=True)
    try:
        kinds_sql = ", ".join("?" for _ in entity_kinds)
        query = f"""
            SELECT e.entity_id, e.canonical_name, e.city, e.province, e.bn_root,
                   COUNT(*) AS n_grants, SUM(g.amount_cad) AS total_amount_cad
            FROM grants_unified g
            JOIN entities e ON e.entity_id = g.recipient_entity_id
            WHERE g.source_dataset = 'federal_gc' AND e.entity_kind IN ({kinds_sql})
        """
        params = list(entity_kinds)
        if province:
            query += " AND e.province = ?"
            params.append(province)
        query += " GROUP BY e.entity_id, e.canonical_name, e.city, e.province, e.bn_root"
        rows = con.execute(query, params).fetchall()
    finally:
        con.close()
    return [
        GrantRecipientCandidate(eid, name, city, prov, None, bn_root, n, total)
        for eid, name, city, prov, bn_root, n, total in rows
    ]
