"""
Regression test for the digit-token fuzzy-match gate in Resolver.resolve().

Confirmed bug (see docs/entity-resolution-methodology.md, AGENTS.md open
issue #2): "ALBERTA CIRCUIT 5A OF JEHOVAH'S WITNESSES" fuzzy-matched
"Alberta Circuit 7A of Jehovah's Witnesses" at a 97.4 token_sort_ratio score
— a differing branch/circuit number barely dents the score against an
otherwise-identical long name. Resolver.resolve() now requires a candidate's
digit-bearing tokens to match exactly before accepting a fuzzy match.

This is a plain assert-script (no pytest fixtures needed), but it's also
pytest-discoverable via requirements-dev.txt:

    .venv/bin/python tests/test_digit_token_gate.py
    .venv/bin/python -m pytest tests/
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.build_entity_graph import (
    Resolver, digit_tokens, digit_tokens_match, normalize_name,
)


def fuzzy_result(resolver, name, province):
    resolver.resolve("federal_gc", name, None, province, allow_fuzzy=True)
    return resolver.links[-1].match_method


def test_differing_branch_number_does_not_match():
    r = Resolver()
    r.add_charity("111111111", "Alberta Circuit 7A of Jehovah's Witnesses", "Edmonton", "AB")
    method = fuzzy_result(r, "ALBERTA CIRCUIT 5A OF JEHOVAH'S WITNESSES", "AB")
    assert method != "fuzzy_accept", (
        f"regression: circuit 5A fuzzy-matched circuit 7A (method={method})"
    )


def test_same_branch_number_minor_variant_still_matches():
    r = Resolver()
    r.add_charity("111111111", "Alberta Circuit 7A of Jehovah's Witnesses", "Edmonton", "AB")
    method = fuzzy_result(r, "Alberta Circuit 7A Jehovahs Witnesses Assn", "AB")
    assert method == "fuzzy_accept", f"gate over-rejected a true same-org variant (method={method})"


def test_reorder_with_no_numbers_still_matches():
    r = Resolver()
    r.add_charity("222222222", "University of New Brunswick", "Fredericton", "NB")
    method = fuzzy_result(r, "THE UNIVERSITY OF NEW BRUNSWICK (UNB)", "NB")
    assert method == "fuzzy_accept", f"gate broke a documented legit near-miss, UNB (method={method})"


def test_apostrophe_variant_still_matches():
    r = Resolver()
    r.add_charity("333333333", "Halifax Gay Men's Chorus Society", "Halifax", "NS")
    method = fuzzy_result(r, "Halifax Gay Mens Chorus", "NS")
    assert method == "fuzzy_accept", (
        f"gate broke a documented legit near-miss, Halifax Gay Men's Chorus (method={method})"
    )


def test_lettered_branch_tokens_are_distinguished_not_collapsed():
    # A naive \d+ regex would extract "5" from both "5A" and "5B", silently
    # reintroducing the bug. digit_tokens() must keep them distinct.
    assert digit_tokens(normalize_name("Circuit 5A")) != digit_tokens(normalize_name("Circuit 5B"))


def test_gate_reject_is_logged_for_qa():
    r = Resolver()
    r.add_charity("111111111", "Alberta Circuit 7A of Jehovah's Witnesses", "Edmonton", "AB")
    fuzzy_result(r, "ALBERTA CIRCUIT 5A OF JEHOVAH'S WITNESSES", "AB")
    assert len(r.gate_rejects) == 1, f"expected exactly 1 gate reject, got {len(r.gate_rejects)}"
    raw_name, rejected_canonical, score, source_dataset = r.gate_rejects[0]
    assert rejected_canonical == "Alberta Circuit 7A of Jehovah's Witnesses"
    assert score >= 90


# ── Pattern A: hyphen/space-split vs. fused digit-letter suffix ──────────────
# Confirmed on real production data: a full pipeline run's QA sample showed
# 8.6% of all digit-gate rejects (128 of 1,492) were the same branch/circuit
# number written differently across sources ("1-B" vs "1B", "9 B" vs "9B").

def test_hyphen_split_suffix_matches_fused():
    r = Resolver()
    r.add_charity("444444444", "Saskatchewan Circuit 1B of Jehovah's Witnesses", "Regina", "SK")
    method = fuzzy_result(r, "Saskatchewan Circuit No 1-B of Jehovah's Witnesses", "SK")
    assert method == "fuzzy_accept", f"hyphen-split 1-B wrongly split from fused 1B (method={method})"


def test_space_split_suffix_matches_fused():
    r = Resolver()
    r.add_charity("555555555", "Ontario Circuit 9B of Jehovah's Witnesses", "Toronto", "ON")
    method = fuzzy_result(r, "Ontario Circuit # 9 B of Jehovah's Witnesses", "ON")
    assert method == "fuzzy_accept", f"space-split 9 B wrongly split from fused 9B (method={method})"


def test_french_hyphen_suffix_matches_fused():
    r = Resolver()
    r.add_charity("666666666", "Circonscription des Témoins de Jéhovah du Québec 10A", "Montreal", "QC")
    method = fuzzy_result(r, "Circonscription des Temoins de Jehovah Quebec 10-A", "QC")
    assert method == "fuzzy_accept", f"French 10-A wrongly split from 10A (method={method})"


def test_fuse_is_symmetric_at_token_level():
    assert digit_tokens(normalize_name("Circuit 1-B")) == digit_tokens(normalize_name("Circuit 1B"))
    assert digit_tokens(normalize_name("Circuit # 9 B")) == frozenset({"9B"})


def test_fuse_leaves_possessive_s_alone():
    # 'JEHOVAH'S' -> 'JEHOVAH S'; the lone 'S' must NOT fuse to a preceding number
    assert digit_tokens(normalize_name("Circuit 7A of Jehovah's Witnesses")) == frozenset({"7A"})


def test_fuse_does_not_merge_11B_and_1B():
    assert digit_tokens(normalize_name("Circuit 11B")) != digit_tokens(normalize_name("Circuit 1B"))


# ── Pattern B: incidental year embedded in a legal name ──────────────────────
# Confirmed on real production data: 13.9% of all digit-gate rejects (207 of
# 1,492) were an incorporation/founding year in one name ("Society (1992)")
# with no branch-number counterpart on the other side.

def test_bare_year_suffix_matches():
    r = Resolver()
    r.add_charity("777777777", "The Lethbridge Soup Kitchen Association", "Lethbridge", "AB")
    method = fuzzy_result(r, "THE LETHBRIDGE SOUP KITCHEN ASSOCIATION 2013", "AB")
    assert method == "fuzzy_accept", f"incidental bare year 2013 wrongly split (method={method})"


def test_parenthetical_year_matches():
    r = Resolver()
    r.add_charity("888888888", "Medicine Hat and District Food Bank", "Medicine Hat", "AB")
    method = fuzzy_result(r, "MEDICINE HAT AND DISTRICT FOOD BANK (1992) ASSOCIATION", "AB")
    assert method == "fuzzy_accept", f"incidental (1992) wrongly split (method={method})"


def test_same_year_on_both_sides_matches():
    r = Resolver()
    r.add_charity("101010101", "Strathcona Community Centre (1972)", "Edmonton", "AB")
    method = fuzzy_result(r, "STRATHCONA COMMUNITY CENTRE SOCIETY (1972)", "AB")
    assert method == "fuzzy_accept", f"matching year on both sides wrongly split (method={method})"


def test_differing_year_on_both_sides_still_splits():
    # Both names carry a year and they differ -> keep it as a differentiator,
    # since this could be two genuinely distinct orgs with the same base name.
    r = Resolver()
    r.add_charity("999999999", "Strathcona Community Centre (1972)", "Edmonton", "AB")
    method = fuzzy_result(r, "STRATHCONA COMMUNITY CENTRE (2010)", "AB")
    assert method != "fuzzy_accept", f"orgs differing only by year must stay split (method={method})"


def test_digit_tokens_match_ignores_asymmetric_year():
    assert digit_tokens_match(frozenset(), frozenset({"2013"}))
    assert digit_tokens_match(frozenset({"5A"}), frozenset({"5A", "1992"}))


def test_digit_tokens_match_keeps_symmetric_differing_year():
    assert not digit_tokens_match(frozenset({"1972"}), frozenset({"2010"}))


def test_digit_tokens_match_still_splits_branch_numbers():
    assert not digit_tokens_match(frozenset({"5A"}), frozenset({"7A"}))
    assert not digit_tokens_match(frozenset({"11B"}), frozenset({"1B"}))
    assert not digit_tokens_match(frozenset({"24"}), frozenset({"33"}))


TESTS = [
    test_differing_branch_number_does_not_match,
    test_same_branch_number_minor_variant_still_matches,
    test_reorder_with_no_numbers_still_matches,
    test_apostrophe_variant_still_matches,
    test_lettered_branch_tokens_are_distinguished_not_collapsed,
    test_gate_reject_is_logged_for_qa,
    test_hyphen_split_suffix_matches_fused,
    test_space_split_suffix_matches_fused,
    test_french_hyphen_suffix_matches_fused,
    test_fuse_is_symmetric_at_token_level,
    test_fuse_leaves_possessive_s_alone,
    test_fuse_does_not_merge_11B_and_1B,
    test_bare_year_suffix_matches,
    test_parenthetical_year_matches,
    test_same_year_on_both_sides_matches,
    test_differing_year_on_both_sides_still_splits,
    test_digit_tokens_match_ignores_asymmetric_year,
    test_digit_tokens_match_keeps_symmetric_differing_year,
    test_digit_tokens_match_still_splits_branch_numbers,
]


def main():
    failures = []
    for test in TESTS:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except AssertionError as e:
            failures.append(test.__name__)
            print(f"  FAIL  {test.__name__}: {e}")
    print(f"\n{len(TESTS) - len(failures)}/{len(TESTS)} passed")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
