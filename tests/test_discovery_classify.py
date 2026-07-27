"""Regression tests for discovery/classify.py's three-bucket shape in both
stages, especially the cases the spec calls out as the ones that must never
be silently mislabeled: a missing blocking key, and social-signal absence."""
from collections import namedtuple

from discovery.block import NO_BLOCKING_KEY
from discovery.classify import (
    classify_charity_status, classify_charity_status_by_bn, classify_federal_grant_match,
    classify_federal_grant_match_by_bn, classify_social_purpose,
    reconcile_bn_conflict, reconcile_grant_match_collisions, reconcile_low_confidence_grant_matches,
)
from discovery.match import MatchResult

FakeCra = namedtuple("FakeCra", ["bn_root", "legal_name"])
FakeSocial = namedtuple("FakeSocial", ["program_name"])
FakeGrantRecipient = namedtuple("FakeGrantRecipient", ["entity_id", "legal_name", "n_grants", "total_amount_cad"])


def test_no_blocking_key_goes_to_needs_review_not_non_charity():
    result = classify_charity_status(NO_BLOCKING_KEY, None)
    assert result.charity_status == "needs_review"
    assert result.review_flag == "no_blocking_key"


def test_high_score_auto_matches_to_registered_charity():
    match = MatchResult(FakeCra("123456789", "Org A"), 95, None, None)
    result = classify_charity_status("postal", match)
    assert result.charity_status == "registered_charity"
    assert result.matched_bn == "123456789"
    assert result.charity_match_method == "fuzzy"


def test_mid_band_score_goes_to_needs_review_charity():
    match = MatchResult(FakeCra("123456789", "Org A"), 80, None, 70)
    result = classify_charity_status("postal", match)
    assert result.charity_status == "needs_review"


def test_low_score_is_non_charity_nonprofit():
    match = MatchResult(FakeCra("123456789", "Org A"), 40, None, None)
    result = classify_charity_status("postal", match)
    assert result.charity_status == "non_charity_nonprofit"


def test_no_candidates_at_all_is_non_charity_nonprofit():
    result = classify_charity_status("postal", None)
    assert result.charity_status == "non_charity_nonprofit"


def test_social_signal_absence_lands_in_not_social_residual_not_silently_promoted():
    result = classify_social_purpose(None)
    assert result.social_status == "not_social"
    assert result.review_flag == "residual_no_signal"


def test_strong_social_signal_auto_promotes():
    match = MatchResult(FakeSocial("Homelessness Partnering Strategy"), 92, None, None)
    result = classify_social_purpose(match)
    assert result.social_status == "social_purpose"


def test_mid_band_social_signal_goes_to_needs_review_not_auto_promoted():
    match = MatchResult(FakeSocial("Homelessness Partnering Strategy"), 80, None, None)
    result = classify_social_purpose(match)
    assert result.social_status == "needs_review"


# ── classify_charity_status_by_bn: Corporations Canada's exact-BN fast path ──

def test_bn_match_auto_accepts_as_registered_charity():
    cra_by_bn = {"123456789": FakeCra("123456789", "Org A")}
    result = classify_charity_status_by_bn("123456789", cra_by_bn)
    assert result.charity_status == "registered_charity"
    assert result.matched_bn == "123456789"
    assert result.matched_cra_name == "Org A"
    assert result.charity_match_score == 100.0
    assert result.charity_match_method == "exact_bn"


def test_bn_present_but_not_in_registry_returns_none_not_a_negative_classification():
    # Falls through to the caller's fuzzy cascade rather than asserting
    # non_charity_nonprofit here -- a BN Corporations Canada carries that
    # doesn't match the CRA registry doesn't necessarily mean "not a
    # charity," it might mean a fuzzy name/postal match still finds it.
    cra_by_bn = {"123456789": FakeCra("123456789", "Org A")}
    result = classify_charity_status_by_bn("999999999", cra_by_bn)
    assert result is None


def test_blank_bn_returns_none_charity():
    result = classify_charity_status_by_bn(None, {"123456789": FakeCra("123456789", "Org A")})
    assert result is None


# ── reconcile_bn_conflict: own BN contradicts an auto-accepted fuzzy match ──

def test_own_bn_not_in_registry_downgrades_auto_accepted_match_to_needs_review():
    # e.g. "The Advocates' Society" (its own BN, not a charity) fuzzy-matched
    # "The Advocates' Society Foundation" (a different, real charity) at 100.
    match = MatchResult(FakeCra("848687091", "The Advocates' Society Foundation"), 100, None, None)
    charity_cls = classify_charity_status("postal", match)
    assert charity_cls.charity_status == "registered_charity"  # before reconciliation

    result = reconcile_bn_conflict(charity_cls, bn="108070707", cra_by_bn={"848687091": FakeCra("848687091", "x")})
    assert result.charity_status == "needs_review"
    assert result.review_flag == "bn_contradicts_fuzzy_match"


def test_no_own_bn_leaves_fuzzy_match_unchanged():
    # REQ records (and any BN-less discovery source) never carry a bn -- must
    # be a complete no-op, not accidentally downgrade every REQ match.
    match = MatchResult(FakeCra("848687091", "Org A"), 100, None, None)
    charity_cls = classify_charity_status("postal", match)
    result = reconcile_bn_conflict(charity_cls, bn=None, cra_by_bn={})
    assert result == charity_cls


def test_own_bn_matches_the_fuzzy_matched_charity_leaves_it_unchanged():
    # Own BN happens to equal the matched charity's BN -- no conflict at all.
    match = MatchResult(FakeCra("848687091", "Org A"), 100, None, None)
    charity_cls = classify_charity_status("postal", match)
    result = reconcile_bn_conflict(charity_cls, bn="848687091", cra_by_bn={"848687091": FakeCra("848687091", "Org A")})
    assert result == charity_cls


def test_needs_review_status_is_not_touched_by_reconciliation():
    # Only the auto-accept bucket needs correcting -- a mid-band match is
    # already flagged for human review regardless of BN conflict.
    match = MatchResult(FakeCra("848687091", "Org A"), 80, None, 70)
    charity_cls = classify_charity_status("postal", match)
    result = reconcile_bn_conflict(charity_cls, bn="999999999", cra_by_bn={"848687091": FakeCra("848687091", "Org A")})
    assert result == charity_cls
    assert result.charity_status == "needs_review"


# ── classify_federal_grant_match_by_bn: Corporations Canada's exact-BN fast
# path for federal grant linking (zero collision risk, unlike the fuzzy path) ──

def test_bn_match_auto_accepts_as_federal_grant_match():
    grant_recipients_by_bn = {"123456789": FakeGrantRecipient(555, "Org A", 7, 250000.0)}
    result = classify_federal_grant_match_by_bn("123456789", grant_recipients_by_bn)
    assert result.federal_grant_status == "federal_grant_match"
    assert result.matched_grant_entity_id == 555
    assert result.matched_grant_entity_name == "Org A"
    assert result.federal_grant_match_score == 100.0
    assert result.federal_grants_received == 7
    assert result.federal_dollars_received == 250000.0
    assert result.review_flag is None


def test_bn_present_but_not_in_pool_returns_none():
    # Falls through to the caller's fuzzy cascade rather than asserting
    # no_match here -- a BN that isn't a federal_gc recipient doesn't
    # necessarily mean the org never received one under a different name.
    grant_recipients_by_bn = {"123456789": FakeGrantRecipient(555, "Org A", 7, 250000.0)}
    result = classify_federal_grant_match_by_bn("999999999", grant_recipients_by_bn)
    assert result is None


def test_blank_bn_returns_none_grant():
    result = classify_federal_grant_match_by_bn(None, {"123456789": FakeGrantRecipient(555, "Org A", 7, 250000.0)})
    assert result is None


# ── classify_federal_grant_match: REQ non-charity nonprofits vs. federal_gc
# grant-recipient entities (discovery/ingest/grant_recipients.py) ───────────

def test_no_blocking_key_goes_to_needs_review_not_no_match():
    result = classify_federal_grant_match(NO_BLOCKING_KEY, None)
    assert result.federal_grant_status == "needs_review"
    assert result.review_flag == "no_blocking_key"


def test_high_score_auto_matches_to_federal_grant_match():
    candidate = FakeGrantRecipient(555, "Refuge des Jeunes de Montreal", 12, 450000.0)
    match = MatchResult(candidate, 95, None, None)
    result = classify_federal_grant_match("name_prefix", match)
    assert result.federal_grant_status == "federal_grant_match"
    assert result.matched_grant_entity_id == 555
    assert result.matched_grant_entity_name == "Refuge des Jeunes de Montreal"
    assert result.federal_grants_received == 12
    assert result.federal_dollars_received == 450000.0


def test_mid_band_score_goes_to_needs_review_grant():
    candidate = FakeGrantRecipient(555, "Refuge des Jeunes de Montreal", 12, 450000.0)
    match = MatchResult(candidate, 80, None, 70)
    result = classify_federal_grant_match("name_prefix", match)
    assert result.federal_grant_status == "needs_review"
    assert result.review_flag == "mid_band_score"


def test_low_score_is_no_match():
    candidate = FakeGrantRecipient(555, "Unrelated Org", 1, 1000.0)
    match = MatchResult(candidate, 40, None, None)
    result = classify_federal_grant_match("name_prefix", match)
    assert result.federal_grant_status == "no_match"


def test_no_candidates_at_all_is_no_match_not_needs_review():
    result = classify_federal_grant_match("name_prefix", None)
    assert result.federal_grant_status == "no_match"


# ── reconcile_grant_match_collisions: same grant entity claimed by >1 org ───
# Confirmed real on REQ data: "Le club des 50 ans et plus" claimed by 6
# different town-specific Golden Age Clubs sharing only a generic phrase.

def _fg_match(entity_id, name="Candidate"):
    candidate = FakeGrantRecipient(entity_id, name, 3, 10000.0)
    match = MatchResult(candidate, 95, None, None)
    return classify_federal_grant_match("name_prefix", match)


def test_two_different_records_claiming_same_entity_both_downgraded():
    a = _fg_match(555, "Le club des 50 ans et plus")
    b = _fg_match(555, "Le club des 50 ans et plus")
    result = reconcile_grant_match_collisions([a, b])
    assert result[0].federal_grant_status == "needs_review"
    assert result[1].federal_grant_status == "needs_review"
    assert result[0].review_flag == "grant_entity_claimed_by_multiple_orgs"


def test_unique_matches_are_left_alone():
    a = _fg_match(555, "Org A")
    b = _fg_match(556, "Org B")
    result = reconcile_grant_match_collisions([a, b])
    assert result[0].federal_grant_status == "federal_grant_match"
    assert result[1].federal_grant_status == "federal_grant_match"


def test_none_and_non_match_entries_pass_through_unchanged():
    a = _fg_match(555, "Org A")
    none_entry = None
    no_match_entry = classify_federal_grant_match("name_prefix", None)
    result = reconcile_grant_match_collisions([a, none_entry, no_match_entry])
    assert result[0].federal_grant_status == "federal_grant_match"
    assert result[1] is None
    assert result[2].federal_grant_status == "no_match"


def test_needs_review_entries_never_collide_with_each_other():
    # A mid-band needs_review classification already carries a
    # matched_grant_entity_id -- must not be swept into the collision check
    # (which only concerns confirmed federal_grant_match entries).
    candidate = FakeGrantRecipient(555, "Candidate", 3, 10000.0)
    mid_band = classify_federal_grant_match("name_prefix", MatchResult(candidate, 80, None, 70))
    auto = _fg_match(555, "Candidate")
    result = reconcile_grant_match_collisions([mid_band, auto])
    assert result[0].review_flag == "mid_band_score"  # unchanged, not relabeled by the collision pass
    assert result[1].federal_grant_status == "federal_grant_match"  # only one real claimant, no collision


def test_three_way_collision_all_three_downgraded():
    a, b, c = _fg_match(555), _fg_match(555), _fg_match(555)
    result = reconcile_grant_match_collisions([a, b, c])
    assert all(r.federal_grant_status == "needs_review" for r in result)


def test_preserves_list_order_and_length():
    a = _fg_match(555)
    b = _fg_match(556)
    c = _fg_match(555)
    result = reconcile_grant_match_collisions([a, b, c])
    assert len(result) == 3
    assert result[1].matched_grant_entity_id == 556
    assert result[1].federal_grant_status == "federal_grant_match"


# ── reconcile_low_confidence_grant_matches: CC-specific confidence floor ────
# (see run_cc.py -- NOT applied to REQ's own pipeline)

def _fg_match_scored(entity_id, score, name="Candidate"):
    candidate = FakeGrantRecipient(entity_id, name, 3, 10000.0)
    match = MatchResult(candidate, score, None, None)
    return classify_federal_grant_match("name_prefix", match)


def test_sub_100_score_downgraded_to_needs_review_by_default():
    match = _fg_match_scored(555, 92)
    result = reconcile_low_confidence_grant_matches([match])
    assert result[0].federal_grant_status == "needs_review"
    assert result[0].review_flag == "low_confidence_fuzzy_match"


def test_perfect_100_score_left_alone_by_default():
    match = _fg_match_scored(555, 100)
    result = reconcile_low_confidence_grant_matches([match])
    assert result[0].federal_grant_status == "federal_grant_match"


def test_exact_bn_match_always_scores_100_and_is_unaffected():
    grant_recipients_by_bn = {"123456789": FakeGrantRecipient(555, "Org A", 7, 250000.0)}
    exact = classify_federal_grant_match_by_bn("123456789", grant_recipients_by_bn)
    result = reconcile_low_confidence_grant_matches([exact])
    assert result[0].federal_grant_status == "federal_grant_match"


def test_none_and_needs_review_entries_pass_through_unchanged():
    none_entry = None
    mid_band = _fg_match_scored(555, 80)  # already needs_review from classify_federal_grant_match itself
    result = reconcile_low_confidence_grant_matches([none_entry, mid_band])
    assert result[0] is None
    assert result[1].federal_grant_status == "needs_review"
    assert result[1].review_flag == "mid_band_score"  # not overwritten by the low-confidence pass


def test_custom_min_score_threshold():
    match = _fg_match_scored(555, 95)
    result = reconcile_low_confidence_grant_matches([match], min_score=90)
    assert result[0].federal_grant_status == "federal_grant_match"  # 95 >= 90, left alone
