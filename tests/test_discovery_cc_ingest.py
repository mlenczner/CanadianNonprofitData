"""Regression tests for ingest/cc.py against a small local CSV fixture built
from a REAL Corporations Canada "Other active corporations" export's column
shape (verified 2026-07-16 snapshot -- see discovery/config.py's CC_* and
ingest/cc.py's module docstring for the real coverage numbers this was
confirmed against). Covers: NFP Act legal-form filtering (excluding boards of
trade / cooperatives / special acts also present in the source file), BN
normalization, the French-name-as-trade-name handling, and blank-field
edge cases confirmed to occur in the real data."""
import csv

from discovery.ingest.cc import load_cc_records

FIELDS = [
    "Corporation number", "Business number (BN)", "Corporate name - form 1",
    "Corporate name - form 2", "Governing legislation", "Status", "Anniversary date",
    "Year of last annual filing", "Date of last annual meeting", "Street", "Street 2",
    "City/town", "Province/territory", "Country", "Postal code",
    "Minimum number of directors", "Maximum number of directors",
]


def _row(corp_num, bn="", name="Org", name_fr="", legislation="Canada Not-for-profit Corporations Act",
         city="Ottawa", province="ON", postal="K1A0A1", street="1 Main St"):
    return {
        "Corporation number": corp_num, "Business number (BN)": bn,
        "Corporate name - form 1": name, "Corporate name - form 2": name_fr,
        "Governing legislation": legislation, "Status": "Active", "Anniversary date": "2020-01-01",
        "Year of last annual filing": "2026", "Date of last annual meeting": "2025-06-01",
        "Street": street, "Street 2": "", "City/town": city, "Province/territory": province,
        "Country": "CA", "Postal code": postal,
        "Minimum number of directors": "3", "Maximum number of directors": "10",
    }


def _write_fixture(tmp_path, rows):
    path = tmp_path / "corporations-active-non-cbca-en.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return str(path)


def test_filters_to_nfp_act_only_excluding_boards_of_trade_and_coops(tmp_path):
    csv_path = _write_fixture(tmp_path, [
        _row("1", name="Real NFP Org"),  # NFP Act -- included
        _row("2", name="Chamber of Commerce", legislation="Boards of Trade Act - Part II"),
        _row("3", name="A Co-op", legislation="Canada Cooperatives Act"),
        _row("4", name="Special Org", legislation="Special Act of Parliament"),
    ])
    records = load_cc_records(csv_path)
    assert [r.legal_name for r in records] == ["Real NFP Org"]


def test_bn_is_normalized_to_9_digit_root(tmp_path):
    csv_path = _write_fixture(tmp_path, [_row("1", bn="895647055")])
    records = load_cc_records(csv_path)
    assert records[0].bn == "895647055"


def test_blank_bn_yields_none_not_empty_string(tmp_path):
    csv_path = _write_fixture(tmp_path, [_row("1", bn="")])
    records = load_cc_records(csv_path)
    assert records[0].bn is None


def test_french_name_form_2_becomes_a_trade_name(tmp_path):
    csv_path = _write_fixture(tmp_path, [
        _row("1", name="Focus Humanitarian Assistance Canada", name_fr="Focus Assistance Humanitaire Canada"),
    ])
    records = load_cc_records(csv_path)
    assert records[0].legal_name == "Focus Humanitarian Assistance Canada"
    assert records[0].trade_names == ("Focus Assistance Humanitaire Canada",)


def test_blank_form_2_yields_empty_trade_names_not_a_blank_entry(tmp_path):
    csv_path = _write_fixture(tmp_path, [_row("1", name_fr="")])
    records = load_cc_records(csv_path)
    assert records[0].trade_names == ()


def test_jurisdiction_comes_from_province_territory_column(tmp_path):
    csv_path = _write_fixture(tmp_path, [_row("1", province="qc")])
    records = load_cc_records(csv_path)
    assert records[0].jurisdiction == "QC"


def test_discovery_source_is_corporations_canada(tmp_path):
    csv_path = _write_fixture(tmp_path, [_row("1")])
    records = load_cc_records(csv_path)
    assert records[0].discovery_source == "corporations_canada"


def test_blank_legal_name_excludes_the_record(tmp_path):
    csv_path = _write_fixture(tmp_path, [_row("1", name="")])
    records = load_cc_records(csv_path)
    assert records == []


def test_postal_code_normalized_same_as_other_sources(tmp_path):
    csv_path = _write_fixture(tmp_path, [_row("1", postal="k1a 0a1")])
    records = load_cc_records(csv_path)
    assert records[0].postal_code == "K1A0A1"
