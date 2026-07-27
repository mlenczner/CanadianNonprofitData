"""
Regression tests for filtering individual (non-organization) recipients out
of entity creation in analysis/build_entity_graph.py.

Before this fix, grants to individuals from federal_gc (recipient_type='P',
Individual/Sole proprietor), Canada Council (Recipient Type='Individual'),
and the T3010 non-qualified-donee schedule (no type field -- individuals
show up as "Lastname  Firstname", a comma apparently dropped upstream)
all minted entities and got their own org pages (e.g. /orgs/mcconnell-joanne)
even though this app is about nonprofit organizations, not people.

federal_gc and Canada Council carry an explicit recipient-type field, checked
directly at their ingestion sites in main(). T3010's non-qualified-donee
schedule has no such field, so looks_like_individual_donee_name() detects the
"Lastname  Firstname" (2+ spaces, no comma) shape instead -- verified against
all 21,462 distinct raw names in data/t3010/non_qualified_donees_*.csv: 5,382
match, and a manual review of all matches found real organization names never
take this shape (one confirmed exception, a community name, accepted as a
rare tradeoff against showing individuals as orgs).

A second gap in federal_gc specifically: recipient_type is blank/NULL for
325,252 of ~1.3M raw rows, and real individuals hide in there too (e.g.
"McConnell, Erin (McMaster University)", a real academic-grant recipient
with no recipient_type at all). looks_like_individual_name_comma() catches
the "Lastname, Firstname [Middle] [(Affiliation)]" shape these take,
scoped deliberately narrowly: only applied when recipient_type is blank
(never overriding an explicit non-'P' declaration -- a spot check under
explicit N/F/G/O/S/A/I codes found a real organization false positive,
"L'Avenue, justice alternative", typed 'N'), and requires the token after
the comma to start with a capital letter and not be a corporate suffix
(Inc/Ltd/Corp/...) -- both guards needed to exclude real counterexamples
found while sampling ("Rouyn-Noranda, ville et village en sante inc." and
"Sumalytics, Inc." respectively).

Run with:
    .venv/bin/python -m pytest tests/test_individual_recipient_filter.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.build_entity_graph import (
    looks_like_individual_donee_name,
    looks_like_individual_name_comma,
)


# ── individual-shaped names (should be filtered) ────────────────────────────

def test_double_space_lastname_firstname_matches():
    assert looks_like_individual_donee_name("McConnell  Joanne")
    assert looks_like_individual_donee_name("Cummings  Barbara")
    assert looks_like_individual_donee_name("Edgecombe  Ron")


def test_double_space_with_nickname_parenthetical_matches():
    assert looks_like_individual_donee_name("Reid  Margaret (Peggy)")


def test_double_space_with_accented_characters_matches():
    assert looks_like_individual_donee_name("Doté  Ghislaine")
    assert looks_like_individual_donee_name("Lagacé  Nathalie")


def test_apostrophe_and_hyphenated_names_match():
    assert looks_like_individual_donee_name("O'Reilly  Amber")
    assert looks_like_individual_donee_name("Cotton-Kinch  Megan")


def test_leading_trailing_whitespace_ignored():
    assert looks_like_individual_donee_name("  McConnell  Joanne  ")


# ── organization-shaped names (should NOT be filtered) ──────────────────────

def test_multi_word_org_names_do_not_match():
    assert not looks_like_individual_donee_name("Ontario Institute for Cancer Research")
    assert not looks_like_individual_donee_name("Canadian Coalition for Action on Tobacco")
    assert not looks_like_individual_donee_name("Antigonish Performing Arts Series")


def test_org_names_with_legal_suffix_do_not_match():
    assert not looks_like_individual_donee_name("NexJ Health Inc.")
    assert not looks_like_individual_donee_name("MONT ST JOSEPH HOME INC.")


def test_single_space_two_word_names_do_not_match():
    # Deliberately conservative: a single-space two-word name is
    # indistinguishable from a real short org name ("Red Cross"), so it's
    # left alone even though some of these may in fact be individuals.
    assert not looks_like_individual_donee_name("Red Cross")


def test_blank_or_none_does_not_match():
    assert not looks_like_individual_donee_name("")
    assert not looks_like_individual_donee_name(None)


# ── looks_like_individual_name_comma() (federal_gc blank-recipient_type gap) ──

def test_comma_lastname_firstname_matches():
    assert looks_like_individual_name_comma("McConnell, Erin (McMaster University)")
    assert looks_like_individual_name_comma("McConnell, Jennifer S")
    assert looks_like_individual_name_comma("McConnell, Meghan")
    assert looks_like_individual_name_comma("McConnell, Rachel (Queen's University)")
    assert looks_like_individual_name_comma("Maarhuis, Calvin")


def test_comma_lowercase_first_token_does_not_match():
    # Real Quebec orgs sometimes take a "Name, lowercase description" form
    # -- confirmed real counterexample found while sampling: "AdMare,
    # centre d'artistes en art actuel des Iles-de-la-Madeleine". Requiring
    # the token after the comma to start with a capital excludes these
    # (at the cost of also missing individuals entered in all-lowercase,
    # an accepted residual gap).
    assert not looks_like_individual_name_comma("L'Avenue, justice alternative")
    assert not looks_like_individual_name_comma("Rouyn-Noranda, ville et village en sante inc.")
    assert not looks_like_individual_name_comma("AdMare, centre d'artistes en art actuel")


def test_comma_corporate_suffix_does_not_match():
    # Confirmed real counterexample: "Sumalytics, Inc." otherwise matches
    # the shape (capitalized token after the comma) but "Inc." is a
    # corporate suffix, not a first name.
    assert not looks_like_individual_name_comma("Sumalytics, Inc.")
    assert not looks_like_individual_name_comma("POPcodes, INC")
    assert not looks_like_individual_name_comma("Smythe, SA")


def test_comma_blank_or_none_does_not_match():
    assert not looks_like_individual_name_comma("")
    assert not looks_like_individual_name_comma(None)


TESTS = [
    test_double_space_lastname_firstname_matches,
    test_double_space_with_nickname_parenthetical_matches,
    test_double_space_with_accented_characters_matches,
    test_apostrophe_and_hyphenated_names_match,
    test_leading_trailing_whitespace_ignored,
    test_multi_word_org_names_do_not_match,
    test_org_names_with_legal_suffix_do_not_match,
    test_single_space_two_word_names_do_not_match,
    test_blank_or_none_does_not_match,
    test_comma_lastname_firstname_matches,
    test_comma_lowercase_first_token_does_not_match,
    test_comma_corporate_suffix_does_not_match,
    test_comma_blank_or_none_does_not_match,
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
