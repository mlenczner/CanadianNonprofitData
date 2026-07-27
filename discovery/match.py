"""Fuzzy scoring: token_sort_ratio (RapidFuzz) of the candidate CRA name
against both the discovery legal name and each trade name, taking the max --
catches "operates as X, registered as Y." Same function scores discovery<->G&C
recipient names (grants.py candidates are shaped with a .legal_name/.trade_names
pair too, so this is source-agnostic).

Was token_set_ratio; switched after a manual validation pass (spec step 7)
found it scoring genuinely unrelated organizations that merely share one
common word as high as real matches -- e.g. "THE ROYAL MONTREAL CURLING
CLUB" vs "FONDATION DE LA MODE DE MONTREAL" (share only "MONTREAL") scored
76/100 under token_set_ratio, indistinguishable from real matches in the
same score range. token_sort_ratio (plus normalize_for_scoring's stopword
stripping and the bilingual "/"-split handling below) separated a sample of
confirmed-wrong matches (scores 43-68) from confirmed-correct ones (89-96)
cleanly; see docs/montreal-discovery-spec.md's Deviations note for the full
before/after numbers.

1:many: if a block has multiple candidates, keep the best match and the
runner-up (name + score) so close calls surface in review instead of
silently picking a winner.
"""
from collections import namedtuple

from rapidfuzz import fuzz

from discovery.normalize import normalize_for_scoring

MatchResult = namedtuple("MatchResult", [
    "best", "best_score", "runner_up", "runner_up_score",
])


def _name_variants(name):
    """A name, plus (if it contains a bilingual "/" separator) each half on
    its own -- lets a French-only or English-only name match against just
    its own-language half of a bilingual "English/French" candidate instead
    of being penalized by token_sort_ratio for the other language's text
    being extra, unmatched content (confirmed case: a discovery record named
    only in French scored 67/100 against its own bilingual CRA record before
    this, because token_sort_ratio has no special handling for "one side has
    an entire extra clause" the way token_set_ratio incidentally did)."""
    variants = [name]
    if name and "/" in name:
        variants.extend(h.strip() for h in name.split("/") if h.strip())
    return variants


def score_names(candidate_name, legal_name, trade_names):
    """Max token_sort_ratio of candidate_name against legal_name and every
    trade name, each tried against the other's bilingual-split halves too
    (see _name_variants). Names are normalized via normalize_for_scoring
    before comparing -- legal-suffix noise ("Inc.", "Enr.") and generic
    category words ("Centre", "Eglise", "Club", ...) alike don't drag scores
    in either direction."""
    cand_variants = [normalize_for_scoring(v) for v in _name_variants(candidate_name)]
    query_variants = [
        normalize_for_scoring(v)
        for n in ([legal_name] + list(trade_names or ()))
        for v in _name_variants(n)
    ]
    best = 0
    for cand_norm in cand_variants:
        if not cand_norm:
            continue
        for q_norm in query_variants:
            if not q_norm:
                continue
            score = fuzz.token_sort_ratio(cand_norm, q_norm)
            if score > best:
                best = score
    return best


def best_match(discovery_record, candidates, name_of=lambda c: c.legal_name):
    """candidates: list of arbitrary records with a name accessor (name_of).
    Returns a MatchResult, or None if candidates is empty."""
    if not candidates:
        return None
    scored = sorted(
        (
            (score_names(name_of(c), discovery_record.legal_name, discovery_record.trade_names), c)
            for c in candidates
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = scored[0]
    if len(scored) > 1:
        runner_up_score, runner_up = scored[1]
    else:
        runner_up, runner_up_score = None, None
    return MatchResult(best, best_score, runner_up, runner_up_score)
