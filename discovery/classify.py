"""Stage 1 (charity status) and Stage 2 (social purpose) classification.
Both are three-bucket by design -- the review bucket exists specifically so a
fuzzy *miss* is never silently mislabeled as the negative class; see the
spec's "Core asymmetry" note for why that matters even more for Stage 2.
"""
from collections import Counter, namedtuple

from discovery.config import AUTO_MATCH_SCORE, NEEDS_REVIEW_FLOOR
from discovery.block import NO_BLOCKING_KEY

CharityClassification = namedtuple("CharityClassification", [
    "charity_status", "matched_bn", "matched_cra_name",
    "charity_match_score", "charity_runner_up_score", "review_flag",
    "charity_match_method",  # "exact_bn", "fuzzy", or None (no match found) -- see classify_charity_status_by_bn()
])

SocialClassification = namedtuple("SocialClassification", [
    "social_status", "social_signal", "social_match_score", "review_flag",
])

FederalGrantMatchClassification = namedtuple("FederalGrantMatchClassification", [
    "federal_grant_status",  # "federal_grant_match", "needs_review", or "no_match"
    "matched_grant_entity_id", "matched_grant_entity_name",
    "federal_grant_match_score", "federal_grant_runner_up_score",
    "federal_grants_received", "federal_dollars_received", "review_flag",
])


def classify_charity_status(block_level, match_result):
    """match_result: discovery.match.MatchResult or None. block_level: one of
    "postal"/"fsa"/"city"/NO_BLOCKING_KEY (see block.py)."""
    if block_level == NO_BLOCKING_KEY:
        # No usable postal/FSA/city at all -- can't be scored, so it must not
        # default to non_charity_nonprofit (spec's Blocking note).
        return CharityClassification("needs_review", None, None, None, None, "no_blocking_key", None)

    if match_result is None or match_result.best_score < NEEDS_REVIEW_FLOOR:
        return CharityClassification("non_charity_nonprofit", None, None,
                                      match_result.best_score if match_result else None, None, None, None)

    if match_result.best_score >= AUTO_MATCH_SCORE:
        return CharityClassification(
            "registered_charity", match_result.best.bn_root, match_result.best.legal_name,
            match_result.best_score, match_result.runner_up_score, None, "fuzzy",
        )

    # mid-band, or (handled by caller) multiple close candidates in the block
    return CharityClassification(
        "needs_review", match_result.best.bn_root, match_result.best.legal_name,
        match_result.best_score, match_result.runner_up_score, "mid_band_score", "fuzzy",
    )


def classify_charity_status_by_bn(bn, cra_by_bn):
    """Exact-BN fast path, tried before the postal/FSA/city fuzzy cascade
    above (see run_cc.py). Confirmed necessary for Corporations Canada: 99.0%
    of its NFP Act records carry a BN directly, so nearly every one of them
    can skip fuzzy matching's inherent ambiguity entirely -- the same
    exact_bn-first, fuzzy-fallback order analysis.build_entity_graph
    .Resolver.resolve() already uses for the main entity graph. Returns None
    (not a classification) when bn is falsy or doesn't match any CRA
    charity, so the caller falls through to the existing block/match/
    classify_charity_status path -- REQ has no BN at all, so this is always
    skipped for that source (bn is always None there)."""
    if not bn:
        return None
    charity = cra_by_bn.get(bn)
    if charity is None:
        return None
    return CharityClassification(
        "registered_charity", charity.bn_root, charity.legal_name, 100.0, None, None, "exact_bn",
    )


def reconcile_bn_conflict(charity_cls, bn, cra_by_bn):
    """Downgrades a fuzzy registered_charity classification to needs_review
    when the discovery record carries its OWN business number that does NOT
    match any CRA charity -- direct evidence contradicting the fuzzy name/
    postal match. Confirmed as a real, non-rare pattern in Corporations
    Canada data: 371 cases auto-accepted as registered_charity (343 of them
    a perfect 100 name-score match) despite the corporation's own real BN
    belonging to no registered charity at all. Common real cause: a
    nonprofit "society" and its separately-incorporated affiliated
    "foundation" share a near-identical name but are genuinely distinct
    legal entities -- e.g. "The Advocates' Society" fuzzy-matched "The
    Advocates' Society Foundation" at 100; different organizations, the
    Foundation is the charity, the Society is not.

    Only downgrades registered_charity (the auto-accept, no-review bucket);
    needs_review and non_charity_nonprofit are left alone since there's no
    silent false acceptance to correct there. A record with no BN at all
    (e.g. every REQ record) is unaffected -- `bn` is falsy, so this is a
    no-op for that source."""
    if charity_cls.charity_status != "registered_charity":
        return charity_cls
    if not bn or bn in cra_by_bn or bn == charity_cls.matched_bn:
        return charity_cls
    return charity_cls._replace(charity_status="needs_review", review_flag="bn_contradicts_fuzzy_match")


def classify_social_purpose(social_match_result):
    """Runs only on the non_charity_nonprofit set. Every signal here is
    positive-only (spec's "Core asymmetry"): a hit can auto-promote to
    social_purpose, but signal-absence must NEVER auto-label not_social --
    that bucket is a residual, assigned by the caller only after this
    function returns no promotion and review has had a chance to look."""
    if social_match_result is None:
        return SocialClassification("not_social", None, None, "residual_no_signal")

    if social_match_result.best_score >= AUTO_MATCH_SCORE:
        return SocialClassification(
            "social_purpose", social_match_result.best.program_name, social_match_result.best_score, None,
        )

    if social_match_result.best_score >= NEEDS_REVIEW_FLOOR:
        return SocialClassification(
            "needs_review", social_match_result.best.program_name, social_match_result.best_score, "mid_band_score",
        )

    return SocialClassification("not_social", None, social_match_result.best_score, "residual_below_floor")


def classify_federal_grant_match_by_bn(bn, grant_recipients_by_bn):
    """Exact-BN fast path for linking a discovery record directly to federal
    grant totals, tried before the fuzzy fallback below -- same exact-first,
    fuzzy-fallback order as classify_charity_status_by_bn(). Unlike that
    fuzzy fallback, a BN match here carries zero collision risk by
    construction: a BN is a hard, unique key (one Corporation Number's BN
    maps to at most one entity), unlike name/address fuzzy scoring, where
    completely different organizations can legitimately share a generic name
    (see reconcile_grant_match_collisions()'s docstring for confirmed real
    examples of that). Built for Corporations Canada (~99% BN coverage on
    NFP Act records) -- REQ never has a BN, so this is always skipped for
    that source. grant_recipients_by_bn: dict of bn_root ->
    GrantRecipientCandidate (see discovery/ingest/grant_recipients.py),
    built including entity_kind='charity' as well as 'other_org' -- unlike
    the fuzzy fallback, this exact path isn't limited to the non-charity set,
    since a charity's own federal grant total is just as directly answerable
    via its BN. Returns None (not a classification) when bn is falsy or
    doesn't match any candidate, so the caller falls through to the fuzzy
    cascade."""
    if not bn:
        return None
    c = grant_recipients_by_bn.get(bn)
    if c is None:
        return None
    return FederalGrantMatchClassification(
        "federal_grant_match", c.entity_id, c.legal_name, 100.0, None,
        c.n_grants, c.total_amount_cad, None,
    )


def classify_federal_grant_match(block_level, match_result):
    """Runs only on the non_charity_nonprofit set (mirrors classify_social_
    purpose's gating in run.py) -- charities are already covered by Stage
    1's CRA-charity match. match_result: discovery.match.MatchResult against
    discovery.ingest.grant_recipients.GrantRecipientCandidate, or None.
    block_level: one of "postal"/"fsa"/"city"/"name_prefix"/NO_BLOCKING_KEY.

    No BN safety net exists here the way reconcile_bn_conflict() has one for
    Corporations Canada -- REQ carries no BN at all -- so an auto-accepted
    match here rests on name/address fuzzy scoring alone. Validate against a
    labeled sample before trusting the federal_grant_match bucket at scale,
    same caveat Stage 1's REQ-vs-CRA match got before its token_set_ratio
    false-positive bug was found."""
    if block_level == NO_BLOCKING_KEY:
        return FederalGrantMatchClassification("needs_review", None, None, None, None, None, None, "no_blocking_key")

    if match_result is None or match_result.best_score < NEEDS_REVIEW_FLOOR:
        return FederalGrantMatchClassification(
            "no_match", None, None, match_result.best_score if match_result else None, None, None, None, None,
        )

    c = match_result.best
    if match_result.best_score >= AUTO_MATCH_SCORE:
        return FederalGrantMatchClassification(
            "federal_grant_match", c.entity_id, c.legal_name,
            match_result.best_score, match_result.runner_up_score,
            c.n_grants, c.total_amount_cad, None,
        )

    return FederalGrantMatchClassification(
        "needs_review", c.entity_id, c.legal_name,
        match_result.best_score, match_result.runner_up_score,
        c.n_grants, c.total_amount_cad, "mid_band_score",
    )


def reconcile_grant_match_collisions(federal_grant_classifications):
    """Downgrades every federal_grant_match classification whose
    matched_grant_entity_id was independently claimed by more than one
    discovery record to needs_review. One grant-recipient entity cannot
    legitimately be more than one real-world organization, so a collision is
    certain evidence at least one side is wrong -- with no reliable way to
    tell which (if any) is correct, all colliding claims are downgraded
    rather than guessing. Same "don't silently assert a shaky match"
    philosophy as the rest of this module (NO_BLOCKING_KEY, mid-band scores,
    reconcile_bn_conflict()) -- just applied across records instead of
    within one.

    Confirmed necessary on real REQ data, not a hypothetical: a first real
    run (58,749 Quebec NPOs vs. 50,204 federal_gc grant-recipient entities)
    found 111 grant-recipient entities each claimed by 2-6 different Quebec
    nonprofits -- e.g. "Le club des 50 ans et plus" claimed by 6 different
    town-specific Golden Age Clubs (Ste-Jeanne-d'Arc, St-Bruno-de-Kamouraska,
    St-Anaclet, ...), sharing only the generic "club"/"age d'or" phrase. No
    amount of stopword tuning fixes this class: the candidate's own
    registered name carries no town-specific information to match against in
    the first place, so the ambiguity is inherent to the data, not the
    scorer. Stopword-list tuning (discovery/normalize.py's
    MATCHING_CATEGORY_WORDS, built from a REQ-vs-CRA-*charity* sample) may
    still reduce single-instance false positives that don't collide, but
    that's a separate, unvalidated improvement -- not attempted here.

    federal_grant_classifications: list of FederalGrantMatchClassification
    (or None) in discovery-record order. Returns a new list, same order and
    length -- only colliding federal_grant_match entries are replaced."""
    entity_counts = Counter(
        c.matched_grant_entity_id for c in federal_grant_classifications
        if c is not None and c.federal_grant_status == "federal_grant_match"
    )
    out = []
    for c in federal_grant_classifications:
        if (c is not None and c.federal_grant_status == "federal_grant_match"
                and entity_counts[c.matched_grant_entity_id] > 1):
            out.append(c._replace(federal_grant_status="needs_review", review_flag="grant_entity_claimed_by_multiple_orgs"))
        else:
            out.append(c)
    return out


def reconcile_low_confidence_grant_matches(federal_grant_classifications, min_score=100):
    """Downgrades a fuzzy federal_grant_match scoring below min_score to
    needs_review -- CC-specific (see run_cc.py), NOT applied to REQ.

    Confirmed necessary for Corporations Canada's fuzzy fallback (used for
    the small remainder of BN-less, non_charity_nonprofit records) on real
    data: unlike REQ's Quebec-only, mostly postal/city-blocked pool, this
    pool is matched nationwide with no province restriction, blocked only by
    name-prefix -- a much larger, more heterogeneous candidate set per
    block. reconcile_grant_match_collisions() catches duplicate claims, but
    not a UNIQUE wrong match, and a spot-check of 12 real sub-100 fuzzy
    matches found roughly half wrong: `"Harbour Authority of Westport"`
    wrongly matched `"...of Newport"`; `"Opportunify"` wrongly matched
    `"Opportunity International Canada"` ($22.1M); `"Hungarian Canadian
    Medical Association"` wrongly matched `"HUNGARIAN - CANADIAN MEDIA
    CORPORATION"`. Matches scoring exactly 100 (perfect normalized-name
    equality, whether or not BN-backed -- e.g. `"CANADA MEDIA FUND"` vs
    `"CANADA MEDIA FUND CORPORATION"`, $1.7B) were not part of this failure
    pattern in the same spot-check and are left alone by the default
    min_score=100.

    Not applied to REQ's own fuzzy federal-grant matching (discovery/run.py):
    a post-collision-fix spot-check of REQ's sub-100 matches (scores
    95-98) found them all genuinely correct (name variants of the same
    town-specific org, not generic collisions) -- REQ's Quebec-only,
    mostly-postal/city-blocked pool doesn't show this failure pattern, so
    tightening it the same way would only lose real matches for no
    precision gain."""
    out = []
    for c in federal_grant_classifications:
        if (c is not None and c.federal_grant_status == "federal_grant_match"
                and c.federal_grant_match_score is not None
                and c.federal_grant_match_score < min_score):
            out.append(c._replace(federal_grant_status="needs_review", review_flag="low_confidence_fuzzy_match"))
        else:
            out.append(c)
    return out
