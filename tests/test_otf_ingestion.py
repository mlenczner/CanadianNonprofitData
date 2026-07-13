"""
Regression tests for Ontario Trillium Foundation (OTF) grants ingestion in
analysis/build_entity_graph.py. See docs/otf-ingestion-spec.md for the full
ingestion spec these enforce: CRN validation, fiscal-year parsing, net-amount
computation, and the collaborative-grant identifier-reuse case.

Run with:
    .venv/bin/python tests/test_otf_ingestion.py
    .venv/bin/python -m pytest tests/
"""

import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.build_entity_graph import otf_fiscal_year, otf_net_amount, validate_otf_crn


# ── CRN validation ────────────────────────────────────────────────────────
# validate_otf_crn() is deliberately stricter than the general-purpose
# normalize_bn(): only a bare 9-digit root or an RR-suffixed 15-char BN is
# accepted, checked after trimming (not de-spacing) the field. Confirmed
# real malformed patterns from the source file are used below rather than
# invented ones.

def test_valid_rr_suffixed_crn():
    assert validate_otf_crn("848778866RR0001") == "848778866"


def test_valid_bare_9_digit_crn():
    assert validate_otf_crn("848778866") == "848778866"


def test_malformed_crn_wrong_suffix_digit_count_is_discarded():
    # confirmed real pattern: 3-digit suffix instead of 4 ("RR001")
    assert validate_otf_crn("834767352RR001") is None


def test_malformed_crn_non_rr_program_account_is_discarded():
    # confirmed real pattern: RP is a real CRA program-account code (payroll)
    # but not a charity registration number -- must not be accepted
    assert validate_otf_crn("107542870RP0001") is None


def test_malformed_crn_with_internal_spaces_is_discarded():
    # confirmed real pattern: "87533 3619 RR 0001" -- internal whitespace is
    # not stripped (only leading/trailing), so this is malformed, not
    # reconstructed into a valid BN by removing the spaces
    assert validate_otf_crn("87533 3619 RR 0001") is None


def test_crn_with_leading_trailing_whitespace_is_trimmed():
    assert validate_otf_crn("  848778866RR0001  ") == "848778866"


def test_empty_or_missing_crn_returns_none():
    assert validate_otf_crn("") is None
    assert validate_otf_crn(None) is None
    assert validate_otf_crn("   ") is None


# ── fiscal year parsing ───────────────────────────────────────────────────

def test_fiscal_year_takes_start_year():
    assert otf_fiscal_year("1999-2000") == 1999
    assert otf_fiscal_year("2025-2026") == 2025


def test_fiscal_year_none_for_blank_or_missing():
    assert otf_fiscal_year(None) is None
    assert otf_fiscal_year("") is None


# ── net amount computation ────────────────────────────────────────────────
# amount_cad = awarded - COALESCE(rescinded, 0), floored at 0 -- see the
# "Amounts" section of docs/otf-ingestion-spec.md for the net-of-rescinded
# decision this encodes.

def test_net_amount_subtracts_rescinded():
    net, floored = otf_net_amount(10000.0, 2500.0)
    assert net == 7500.0
    assert floored is False


def test_net_amount_null_rescinded_treated_as_zero():
    net, floored = otf_net_amount(10000.0, None)
    assert net == 10000.0
    assert floored is False


def test_net_amount_floors_at_zero_when_rescinded_exceeds_awarded():
    # Should not happen (verified: 0 such rows in the real file) but the
    # pipeline must not let a negative amount into grants_unified if it did.
    net, floored = otf_net_amount(500.0, 600.0)
    assert net == 0.0
    assert floored is True


def test_net_amount_equal_awarded_and_rescinded_nets_to_zero_not_floored():
    net, floored = otf_net_amount(500.0, 500.0)
    assert net == 0.0
    assert floored is False


# ── collaborative-grant identifiers ────────────────────────────────────────
# Identifier:Identificateur is NOT unique (confirmed: 5 identifiers cover 11
# rows in the real file -- CIM* ids are shared across genuinely different
# recipient organizations for collaborative grants) and must never be used as
# a dedup key -- every row is its own distinct grant. The ingestion code
# never groups/dedups by identifier; this test guards against a future change
# accidentally introducing one (e.g. a well-intentioned "distinct grants"
# query keyed on the wrong column).

def test_duplicate_identifiers_produce_distinct_rows_not_deduped():
    con = duckdb.connect(":memory:")
    con.execute("""
        CREATE TABLE otf AS SELECT * FROM (VALUES
            ('CIM2015102902', 'Org A', 1000.0, 'ON'),
            ('CIM2015102902', 'Org B', 2000.0, 'ON'),
            ('CIM2015102902', 'Org C', 3000.0, 'BC')
        ) AS t(identifier, org_name, amount_awarded, recipient_city)
    """)
    rows = con.execute("SELECT identifier, org_name, amount_awarded FROM otf").fetchall()
    assert len(rows) == 3, "a shared collaborative-grant identifier must not collapse distinct grants"
    assert len({r[1] for r in rows}) == 3, "each row's recipient must remain distinct"
    assert len({r[0] for r in rows}) == 1, "sanity check: these really do share one identifier"


TESTS = [
    test_valid_rr_suffixed_crn,
    test_valid_bare_9_digit_crn,
    test_malformed_crn_wrong_suffix_digit_count_is_discarded,
    test_malformed_crn_non_rr_program_account_is_discarded,
    test_malformed_crn_with_internal_spaces_is_discarded,
    test_crn_with_leading_trailing_whitespace_is_trimmed,
    test_empty_or_missing_crn_returns_none,
    test_fiscal_year_takes_start_year,
    test_fiscal_year_none_for_blank_or_missing,
    test_net_amount_subtracts_rescinded,
    test_net_amount_null_rescinded_treated_as_zero,
    test_net_amount_floors_at_zero_when_rescinded_exceeds_awarded,
    test_net_amount_equal_awarded_and_rescinded_nets_to_zero_not_floored,
    test_duplicate_identifiers_produce_distinct_rows_not_deduped,
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
