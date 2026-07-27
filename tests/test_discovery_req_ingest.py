"""Regression tests for ingest/req.py against a small local CSV fixture built
from a REAL REQ export's structure (verified 2026-07-01 snapshot + the
official "Guide d'utilisation" IN-537 -- see discovery/config.py's REQ_*
comments). Covers: NPO legal-form + active-status filtering, Montreal region
filtering, legal-name/trade-name/former-name/numbered-company-name handling,
the LIGN2-vs-LIGN3 city-line shift, province-suffix stripping, and the
fail-loud behavior when the expected files aren't present."""
import csv

import pytest

from discovery.ingest.req import load_req_records, ReqDataError

ENTREPRISE_FIELDS = ["NEQ", "COD_FORME_JURI", "COD_STAT_IMMAT",
                     "ADR_DOMCL_LIGN1_ADR", "ADR_DOMCL_LIGN2_ADR", "ADR_DOMCL_LIGN3_ADR", "ADR_DOMCL_LIGN4_ADR"]
NOM_FIELDS = ["NEQ", "NOM_ASSUJ", "TYP_NOM_ASSUJ", "STAT_NOM"]


def _write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _make_fixture(tmp_path, entreprise_rows, nom_rows):
    data_dir = tmp_path / "req_export"
    data_dir.mkdir()
    _write_csv(data_dir / "Entreprise.csv", ENTREPRISE_FIELDS, entreprise_rows)
    _write_csv(data_dir / "Nom.csv", NOM_FIELDS, nom_rows)
    return str(data_dir)


def _ent(neq, forme="APE", stat="IM", lign1="1 Rue A", lign2="Montreal (Québec)", lign3="", lign4="H2X1Y4"):
    return {"NEQ": neq, "COD_FORME_JURI": forme, "COD_STAT_IMMAT": stat,
            "ADR_DOMCL_LIGN1_ADR": lign1, "ADR_DOMCL_LIGN2_ADR": lign2,
            "ADR_DOMCL_LIGN3_ADR": lign3, "ADR_DOMCL_LIGN4_ADR": lign4}


def _nom(neq, name, typ="N", stat="V"):
    return {"NEQ": neq, "NOM_ASSUJ": name, "TYP_NOM_ASSUJ": typ, "STAT_NOM": stat}


def test_filters_to_npo_legal_form_active_status_and_montreal_region(tmp_path):
    data_dir = _make_fixture(
        tmp_path,
        entreprise_rows=[
            _ent("1111111111"),  # APE + IM + Montreal -- should match
            _ent("2222222222", forme="CIE"),  # wrong legal form -- excluded
            _ent("3333333333", stat="RD"),  # struck off -- excluded
            _ent("4444444444", lign2="Quebec City (Québec)", lign4="G1A1A1"),  # not Montreal -- excluded
        ],
        nom_rows=[
            _nom("1111111111", "Org Montreal NPO"),
            _nom("2222222222", "Regular Corp"),
            _nom("3333333333", "Former NPO"),
            _nom("4444444444", "Org Quebec City NPO"),
        ],
    )
    records = load_req_records(data_dir)
    assert [r.source_id for r in records] == ["1111111111"]
    assert records[0].legal_name == "Org Montreal NPO"


def test_collects_trade_names_and_ignores_numbered_company_placeholder(tmp_path):
    data_dir = _make_fixture(
        tmp_path,
        entreprise_rows=[_ent("1111111111")],
        nom_rows=[
            _nom("1111111111", "Org Montreal NPO", typ="N"),
            _nom("1111111111", "Operating Name A", typ="A"),
            _nom("1111111111", "Operating Name B", typ="A"),
            _nom("1111111111", "9000-0019 QUÉBEC INC.", typ="M"),  # numbered-company placeholder -- excluded
        ],
    )
    records = load_req_records(data_dir)
    assert len(records) == 1
    assert records[0].legal_name == "Org Montreal NPO"
    assert set(records[0].trade_names) == {"Operating Name A", "Operating Name B"}


def test_excludes_former_name_in_favour_of_current(tmp_path):
    data_dir = _make_fixture(
        tmp_path,
        entreprise_rows=[_ent("1111111111")],
        nom_rows=[
            _nom("1111111111", "Old Name Inc.", typ="N", stat="A"),  # former -- must not win
            _nom("1111111111", "New Current Name", typ="N", stat="V"),
        ],
    )
    records = load_req_records(data_dir)
    assert records[0].legal_name == "New Current Name"


def test_no_current_legal_name_excludes_the_record(tmp_path):
    data_dir = _make_fixture(
        tmp_path,
        entreprise_rows=[_ent("1111111111")],
        nom_rows=[_nom("1111111111", "Old Name Inc.", typ="N", stat="A")],  # only a former name on file
    )
    records = load_req_records(data_dir)
    assert records == []


def test_city_shifts_to_lign3_when_lign2_is_a_secondary_address_line(tmp_path):
    # Real observed shape: LIGN1=street, LIGN2="3E ÉTAGE, TOUR OUEST" (a floor
    # descriptor, not a city), LIGN3=the actual city, LIGN4=postal code.
    data_dir = _make_fixture(
        tmp_path,
        entreprise_rows=[_ent("1111111111", lign2="3E ÉTAGE, TOUR OUEST", lign3="Longueuil (Québec)")],
        nom_rows=[_nom("1111111111", "Org With Suite Line")],
    )
    records = load_req_records(data_dir, region_filter=False)
    assert records[0].city == "Longueuil"


def test_province_suffix_stripped_in_both_parenthetical_and_bare_forms(tmp_path):
    data_dir = _make_fixture(
        tmp_path,
        entreprise_rows=[
            _ent("1111111111", lign2="Montreal (Québec)"),
            _ent("2222222222", lign2="WESTMOUNT QC", lign4="H3Y1P1"),
        ],
        nom_rows=[_nom("1111111111", "Org A"), _nom("2222222222", "Org B")],
    )
    records = load_req_records(data_dir)
    cities = {r.source_id: r.city for r in records}
    assert cities["1111111111"] == "Montreal"
    assert cities["2222222222"] == "WESTMOUNT"


def test_region_filter_false_returns_records_outside_montreal_too(tmp_path):
    data_dir = _make_fixture(
        tmp_path,
        entreprise_rows=[_ent("4444444444", lign2="Quebec City (Québec)", lign4="G1A1A1")],
        nom_rows=[_nom("4444444444", "Org Quebec City NPO")],
    )
    assert load_req_records(data_dir, region_filter=True) == []
    records = load_req_records(data_dir, region_filter=False)
    assert len(records) == 1
    assert records[0].city == "Quebec City"


def test_montreal_matched_via_fsa_fallback_when_city_name_not_in_allowlist(tmp_path):
    # City string doesn't match the allowlist verbatim, but the postal code's
    # FSA (H3-) is in the Montreal-island fallback set.
    data_dir = _make_fixture(
        tmp_path,
        entreprise_rows=[_ent("1111111111", lign2="Some Unlisted Neighbourhood Name", lign4="H3A1E4")],
        nom_rows=[_nom("1111111111", "Org Matched By FSA")],
    )
    records = load_req_records(data_dir)
    assert [r.source_id for r in records] == ["1111111111"]


def test_missing_files_fail_loudly(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(ReqDataError):
        load_req_records(str(empty_dir))
