"""Candidate blocking: postal -> FSA -> city -> (optional) name-prefix
cascade, degrading gracefully so a full discovery x candidate cross product
is never scored. A discovery row with no usable blocking key at all has no
blocking key at all -- per the spec, that routes straight to needs_review
rather than being silently dropped or falling into an oversized,
low-precision block.

The name-prefix tier is opt-in (only tried when a name_prefix_idx is passed)
-- existing callers matching against CRA charities (which almost always
carry postal/city) are completely unaffected. Added for a candidate pool
that structurally has neither: `entities` residual `other_org` rows created
by analysis.build_entity_graph.py's Resolver only ever persist province, not
city or postal (confirmed -- see discovery/ingest/grant_recipients.py), so
postal/FSA/city blocking would degrade to one province-wide bucket for that
pool. Mirrors the (province, name-prefix) blocking
analysis.build_entity_graph.py's own Resolver.resolve() already uses for the
exact same "no postal available" reason.
"""
from collections import defaultdict

from discovery.normalize import fsa, normalize_org_name

NO_BLOCKING_KEY = "__no_blocking_key__"

NAME_PREFIX_LEN = 4  # matches analysis.build_entity_graph.name_prefix()'s own choice


def _index_by(cra_records, key_fn):
    idx = defaultdict(list)
    for rec in cra_records:
        key = key_fn(rec)
        if key:
            idx[key].append(rec)
    return idx


def name_prefix(legal_name):
    norm = normalize_org_name(legal_name)
    return norm[:NAME_PREFIX_LEN] if norm else None


def candidates_for(discovery_record, cra_records, postal_idx=None, fsa_idx=None, city_idx=None,
                    name_prefix_idx=None):
    """Return (candidates, block_level) for one discovery record. block_level
    is one of "postal", "fsa", "city", "name_prefix", or NO_BLOCKING_KEY.
    name_prefix_idx is only tried (as a last resort, after city) when passed
    explicitly -- omit it to get the original postal/FSA/city-only cascade."""
    postal_idx = postal_idx if postal_idx is not None else _index_by(cra_records, lambda r: r.postal_code)
    fsa_idx = fsa_idx if fsa_idx is not None else _index_by(cra_records, lambda r: fsa(r.postal_code))
    city_idx = city_idx if city_idx is not None else _index_by(cra_records, lambda r: (r.city or "").strip().upper())

    if discovery_record.postal_code:
        hits = postal_idx.get(discovery_record.postal_code)
        if hits:
            return hits, "postal"

    d_fsa = fsa(discovery_record.postal_code)
    if d_fsa:
        hits = fsa_idx.get(d_fsa)
        if hits:
            return hits, "fsa"

    city = (discovery_record.city or "").strip().upper()
    if city:
        hits = city_idx.get(city)
        if hits:
            return hits, "city"

    if name_prefix_idx is not None:
        prefix = name_prefix(discovery_record.legal_name)
        if prefix:
            hits = name_prefix_idx.get(prefix)
            if hits:
                return hits, "name_prefix"

    return [], NO_BLOCKING_KEY


def build_indexes(cra_records):
    """Precompute the three indexes once for a batch run instead of rebuilding
    them per discovery record inside candidates_for()."""
    return (
        _index_by(cra_records, lambda r: r.postal_code),
        _index_by(cra_records, lambda r: fsa(r.postal_code)),
        _index_by(cra_records, lambda r: (r.city or "").strip().upper()),
    )


def build_name_prefix_index(cra_records):
    """Separate from build_indexes() (opt-in, see module docstring) rather
    than folded into it, so existing callers' return-tuple arity is
    completely unaffected."""
    return _index_by(cra_records, lambda r: name_prefix(r.legal_name))
