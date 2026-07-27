"""
Regression tests for a fuzzy-match blocking gap in Resolver.resolve()
(analysis/build_entity_graph.py), found while investigating why POP Montreal
International Music Festival (a real registered charity, BN 853671972) wasn't
merging with its own name across T3010 donee-schedule records.

Root cause: fuzzy candidates are blocked by (province, name-prefix) via
block_key(). T3010 donee-name fields routinely leave province blank --
t3010_non_qualified_donee has no province/city column in the CRA source at
all, and t3010_qualified_donee's is frequently empty even when a donee name
is present (confirmed: 3 of 4 real POP Montreal donee rows had a NULL
Province). A blank-province record produced block key "|POP " while the
charity itself, seeded from the T3010 identification extract with its real
registered province, sat under "QC|POP " -- an entirely different bucket, so
the fuzzy matcher never even considered the correct candidate regardless of
name similarity.

Fixed by adding a second index, fuzzy_index_by_prefix (keyed on name-prefix
only, spanning all provinces), used ONLY when the incoming record's province
is blank. Records that do carry a province keep the original, tighter
(province, prefix) blocking completely unchanged -- this is a fallback for
the "we don't know the province" case, not a loosening of blocking in general.

Run with:
    .venv/bin/python tests/test_province_blank_fuzzy_fallback.py
    .venv/bin/python -m pytest tests/
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.build_entity_graph import Resolver


def fuzzy_link(resolver, name, province):
    resolver.resolve("t3010_qualified_donee", name, None, province, allow_fuzzy=True)
    return resolver.links[-1]


def test_blank_province_still_matches_charity_in_known_province():
    # Note: this uses a non-bilingual charity name deliberately. POP
    # Montreal's real CRA name is bilingual (".../FESTIVAL INTERNATIONAL DE
    # MUSIQUE POP MONTREAL"), and a short English-only donee name scores
    # only 25-65 (token_sort_ratio) against that full bilingual string --
    # well under FUZZY_ACCEPT -- regardless of province blocking. That's a
    # separate, unfixed gap (normalize_name() only splits pipe-formatted
    # bilingual names, not "/"-formatted ones like the T3010 registry uses).
    # This test isolates the province-blocking fix from that separate issue.
    r = Resolver()
    charity_eid = r.add_charity(
        "853671972", "POP Montreal International Music Festival", "Montreal", "QC",
    )
    # Mirrors the real bug: a T3010 donee-schedule row naming this exact
    # charity but with no Province filled in.
    link = fuzzy_link(r, "POP Montreal International Music Festival Society", None)
    assert link.match_method == "fuzzy_accept", (
        f"blank-province record failed to fuzzy-match its own charity (method={link.match_method})"
    )
    assert link.entity_id == charity_eid


def test_blank_province_also_matches_short_name_variant():
    r = Resolver()
    charity_eid = r.add_charity("853671972", "Pop Montreal", "Montreal", "QC")
    link = fuzzy_link(r, "POP Montreal ", None)  # trailing space, as seen in real T3010 data
    assert link.match_method == "fuzzy_accept"
    assert link.entity_id == charity_eid


def test_known_province_blocking_is_unchanged_and_still_scoped():
    # Two different real orgs, same normalized-name prefix, different
    # provinces. When the incoming record DOES carry a (correct) province,
    # each must still resolve to its own province's entity -- the new
    # prefix-wide fallback index must not be consulted, and must not blur
    # this distinction, when province is known.
    r = Resolver()
    qc_eid = r.add_charity("111111111", "Community Food Bank", "Montreal", "QC")
    on_eid = r.add_charity("222222222", "Community Food Bank", "Toronto", "ON")
    link_qc = fuzzy_link(r, "Community Food Bank Inc", "QC")
    link_on = fuzzy_link(r, "Community Food Bank Inc", "ON")
    assert link_qc.entity_id == qc_eid
    assert link_on.entity_id == on_eid


def test_blank_province_fallback_still_respects_digit_token_gate():
    # The digit-token gate (differing branch/circuit/chapter numbers must not
    # fuzzy-match) must still apply on the province-agnostic fallback path,
    # not just the normal province-scoped path.
    r = Resolver()
    r.add_charity("333333333", "Alberta Circuit 7A of Jehovah's Witnesses", "Edmonton", "AB")
    link = fuzzy_link(r, "ALBERTA CIRCUIT 5A OF JEHOVAH'S WITNESSES", None)
    assert link.match_method != "fuzzy_accept", (
        f"blank-province fallback bypassed the digit-token gate (method={link.match_method})"
    )


def test_blank_province_with_no_prefix_candidates_falls_through_to_residual():
    r = Resolver()
    r.add_charity("444444444", "Some Other Charity", "Halifax", "NS")
    link = fuzzy_link(r, "Completely Unrelated Org Name", None)
    assert link.match_method == "unmatched_new"


TESTS = [
    test_blank_province_still_matches_charity_in_known_province,
    test_blank_province_also_matches_short_name_variant,
    test_known_province_blocking_is_unchanged_and_still_scoped,
    test_blank_province_fallback_still_respects_digit_token_gate,
    test_blank_province_with_no_prefix_candidates_falls_through_to_residual,
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
