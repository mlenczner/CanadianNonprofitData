"""Regression test for _note_parse_failure() in analysis/build_entity_graph.py --
the counter that gives to_float()/fiscal_year_from_date() parse failures the
same visibility every other silent-drop risk in that file already has (T3010
ignore_errors rejects, OTF CRN discards, rescinded-floor count). Only a
non-blank, unparseable value should count as a failure -- a genuinely absent
field isn't a parse failure, it's just missing data."""
from collections import defaultdict

from analysis.build_entity_graph import _note_parse_failure


def test_non_blank_unparseable_value_increments_counter():
    counters = defaultdict(int)
    _note_parse_failure(counters, "federal_gc.agreement_value", "not-a-number")
    assert counters["federal_gc.agreement_value"] == 1


def test_none_does_not_increment_counter():
    counters = defaultdict(int)
    _note_parse_failure(counters, "federal_gc.agreement_value", None)
    assert counters["federal_gc.agreement_value"] == 0


def test_blank_string_does_not_increment_counter():
    counters = defaultdict(int)
    _note_parse_failure(counters, "federal_gc.agreement_value", "   ")
    assert counters["federal_gc.agreement_value"] == 0


def test_multiple_failures_accumulate_per_key():
    counters = defaultdict(int)
    _note_parse_failure(counters, "otf.amount_awarded", "garbage")
    _note_parse_failure(counters, "otf.amount_awarded", "also garbage")
    _note_parse_failure(counters, "otf.fiscal_year_raw", "n/a")
    assert counters["otf.amount_awarded"] == 2
    assert counters["otf.fiscal_year_raw"] == 1
