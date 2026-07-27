"""
Regression tests for BN (CRA business number) normalization in
analysis/build_entity_graph.py, part of the BN-hygiene spec
(docs/webapp-fixes-and-official-links-spec.md follow-up).

Raw BNs in entity_links arrive in many formats: "132162041RT0001", "ARC
132162041 RT 0001", bare 9-digit, period/hyphen-punctuated, etc. Before this
fix, normalize_bn() had two gaps:

1. It never stripped a leading "ARC"/"CRA" token or embedded periods, so
   "ARC 132162041 RT 0001" and "132.162.041" both failed to parse and were
   silently treated as no-BN, even though they're unambiguous.
2. Its 15-char pattern (BN15_RE) accepted *any* 2-letter program-account
   code, not just the CRA-documented RR/RT/RP/RC codes, and a permissive
   parts[0]-prefix fallback let some malformed strings through -- looser
   than the ingestion spec calls for.

Fixed with a rewritten normalize_bn(): uppercase, strip whitespace/periods/
hyphens, strip a leading ARC/CRA token, strip a trailing RR|RT|RP|RC+4-digit
suffix, and reject anything that doesn't yield exactly a 9-digit root.
Normalization only -- it does not "fix" leading zeros or guess digits (see
bn_reject_shape() and the near-miss review queue for how a source typo like
a mangled BN is instead surfaced for human review, not silently repaired).

Run with:
    .venv/bin/python -m pytest tests/test_bn_normalization.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.build_entity_graph import bn_reject_shape, normalize_bn


# ── accepted formats ────────────────────────────────────────────────────────

def test_bare_9_digit_accepted():
    assert normalize_bn("132162041") == "132162041"


def test_rr_suffixed_15_char_accepted():
    assert normalize_bn("132162041RR0001") == "132162041"


def test_rt_program_code_accepted():
    assert normalize_bn("132162041RT0001") == "132162041"


def test_rp_program_code_accepted():
    assert normalize_bn("132162041RP0001") == "132162041"


def test_rc_program_code_accepted():
    assert normalize_bn("132162041RC0001") == "132162041"


def test_arc_prefix_stripped():
    assert normalize_bn("ARC 132162041 RT 0001") == "132162041"


def test_cra_prefix_stripped():
    assert normalize_bn("CRA132162041RR0001") == "132162041"


def test_periods_stripped():
    assert normalize_bn("132.162.041") == "132162041"


def test_hyphens_stripped():
    assert normalize_bn("132-162-041") == "132162041"


def test_leading_trailing_whitespace_stripped():
    assert normalize_bn("  132162041RR0001  ") == "132162041"


def test_lowercase_accepted():
    assert normalize_bn("132162041rr0001") == "132162041"


# ── rejected formats (treated as no-BN, never repaired/guessed) ────────────

def test_na_rejected():
    assert normalize_bn("N/A") is None


def test_postal_code_rejected():
    assert normalize_bn("M5V 2T3") is None


def test_8_digit_rejected():
    assert normalize_bn("12345678") is None


def test_10_digit_rejected():
    assert normalize_bn("1234567890") is None


def test_3_digit_program_suffix_rejected():
    # "834767352RR001" -- only a 3-digit account suffix, not the required 4
    # (a payroll/other account code fragment, not a real charity BN).
    assert normalize_bn("834767352RR001") is None


def test_non_standard_program_code_no_longer_silently_accepted():
    # Previously normalize_bn() accepted *any* 2-letter program code
    # (BN15_RE = r"^\d{9}[A-Z]{2}\d{4}$"); the ingestion spec restricts this
    # to the CRA-documented RR/RT/RP/RC codes only, so a made-up or
    # unrelated 2-letter code is now correctly rejected rather than
    # silently accepted as if it were a real BN.
    assert normalize_bn("132162041BC0001") is None


def test_blank_rejected():
    assert normalize_bn("") is None
    assert normalize_bn("   ") is None
    assert normalize_bn(None) is None


def test_does_not_repair_or_guess_a_leading_zero():
    # A 9-digit string is already a plausible root even if it's a corrupted
    # version of some other org's real BN (see the near-miss review queue)
    # -- normalize_bn() must not try to detect or "fix" this, only parse.
    assert normalize_bn("011921981") == "011921981"


# ── bn_reject_shape() classification (used to bucket reject counts) ───────

def test_bn_reject_shape_buckets_by_digit_count():
    assert bn_reject_shape("12345678") == "8 digits"
    assert bn_reject_shape("1234567890") == "10 digits"


def test_bn_reject_shape_buckets_non_numeric_separately():
    assert bn_reject_shape("N/A") != bn_reject_shape("12345678")
    assert bn_reject_shape("M5V 2T3") == "non-numeric"
