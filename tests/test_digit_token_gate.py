"""
Regression test for the digit-token fuzzy-match gate in Resolver.resolve().

Confirmed bug (see docs/entity-resolution-methodology.md, AGENTS.md open
issue #2): "ALBERTA CIRCUIT 5A OF JEHOVAH'S WITNESSES" fuzzy-matched
"Alberta Circuit 7A of Jehovah's Witnesses" at a 97.4 token_sort_ratio score
— a differing branch/circuit number barely dents the score against an
otherwise-identical long name. Resolver.resolve() now requires a candidate's
digit-bearing tokens to match exactly before accepting a fuzzy match.

This is a plain assert-script, not a pytest suite (no test framework is
installed in this project's minimal venv) — run directly:

    .venv/bin/python tests/test_digit_token_gate.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.build_entity_graph import Resolver, digit_tokens, normalize_name


def fuzzy_result(resolver, name, province):
    resolver.resolve("federal_gc", name, None, province, allow_fuzzy=True)
    return resolver.links[-1][4]  # match_method


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


TESTS = [
    test_differing_branch_number_does_not_match,
    test_same_branch_number_minor_variant_still_matches,
    test_reorder_with_no_numbers_still_matches,
    test_apostrophe_variant_still_matches,
    test_lettered_branch_tokens_are_distinguished_not_collapsed,
    test_gate_reject_is_logged_for_qa,
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
