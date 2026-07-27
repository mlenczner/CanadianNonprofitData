"""Tests for analysis/build_evidence_site.py. Fixture YAML + CSV written
in-test; never touches the real evidence/ files."""

import csv
import html.parser
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analysis import build_evidence_site as bes  # noqa: E402
from test_org_page import assert_parses_cleanly, assert_drawers_are_reachable  # noqa: E402

FIXTURE_YAML = """\
interventions:
  - id: fully_verified
    name: Fully Verified Thing
    aliases: [FVT, "le truc verifie"]
    mechanism: >
      Does a thing reliably, across several trials.
    evidence:
      - source: Big Registry
        finding: Rated highly effective across three independent trials.
        status: verified_2024-01-01  # bigregistry.org/programs/fvt
        note: Anchor evidence entry
      - source: Second Registry
        finding: Confirms stability of effect in a different population.
        status: verified_2024-02-02
    canadian_relevance: Delivered by several Canadian charities per federal grant text.

  - id: mixed_intervention
    name: Mixed Intervention
    aliases: [MI]
    mechanism: >
      Does a thing, with evidence quality mixed across sources.
    evidence:
      - source: Solid Registry
        finding: Rated effective in two RCTs.
        status: verified_2024-03-03  # solidregistry.gov/mi
      - source: Shaky Source
        finding: Some claim of population-level transfer null result, context-transfer example.
        status: from_model_knowledge  # still to verify
    canadian_relevance: Not yet identified in federal grants.csv text.

  - id: all_unverified
    name: All Unverified Thing
    aliases: [AUT]
    mechanism: >
      Might do a thing, nobody has checked yet.
    evidence:
      - source: Draft Source
        finding: Claimed to help, drafted from model knowledge only.
        status: from_model_knowledge  # collect and read
    canadian_relevance: Canadian evaluations exist; not yet read and verified.
"""

CSV_HEADER = [
    "category", "recipient_legal_name", "business_number", "city", "province",
    "n_grants", "total_cad", "first_year", "last_year", "example_funder",
    "receipt_description_snippet", "receipt_ref_number", "entity_id",
    "match_method", "entity_kind", "bn_root", "latest_revenue", "fiscal_period_end",
]


def make_csv_row(category, name, total_cad, ref_number="REF-0001", **overrides):
    row = {
        "category": category, "recipient_legal_name": name, "business_number": "",
        "city": "Toronto", "province": "ON", "n_grants": "2", "total_cad": str(total_cad),
        "first_year": "2018", "last_year": "2021", "example_funder": "Some Department",
        "receipt_description_snippet": f"Funding to support {name}'s program work.",
        "receipt_ref_number": ref_number, "entity_id": "", "match_method": "exact_bn",
        "entity_kind": "charity", "bn_root": "", "latest_revenue": "", "fiscal_period_end": "",
    }
    row.update(overrides)
    return row


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER)
        w.writeheader()
        for row in rows:
            w.writerow(row)


@pytest.fixture
def fixture_paths(tmp_path):
    yaml_path = tmp_path / "evidence-spine-seed.yaml"
    yaml_path.write_text(FIXTURE_YAML, encoding="utf-8")

    csv_path = tmp_path / "seed-classifications.csv"
    rows = [
        make_csv_row("fully_verified", "Existing Org Inc", 500000, "REF-0001"),
        make_csv_row("fully_verified", "Nonexistent Org Inc", 300000, "REF-0002"),
        make_csv_row("fully_verified", "Third Org", 200000, "REF-0003"),
        make_csv_row("fully_verified", "Fourth Org", 100000, "REF-0004"),
        make_csv_row("fully_verified", "Fifth Org", 50000, "REF-0005"),
    ]
    write_csv(csv_path, rows)

    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    (orgs_dir / "existing-org-inc.html").write_text("<html><body>dummy org page</body></html>", encoding="utf-8")

    out_dir = tmp_path / "out"
    return {"yaml": str(yaml_path), "csv": str(csv_path), "orgs": str(orgs_dir), "out": str(out_dir)}


class TagCollector(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


def parses_cleanly(page_html):
    assert_parses_cleanly(page_html)


# ── qualification rule ───────────────────────────────────────────────────────

def test_all_unverified_gets_no_page_and_appears_in_progress(fixture_paths):
    written = bes.build_site(fixture_paths["yaml"], fixture_paths["csv"], fixture_paths["out"], fixture_paths["orgs"])
    names = [os.path.basename(p) for p in written]
    assert "all_unverified.html" not in names
    assert "fully_verified.html" in names
    assert "mixed_intervention.html" in names

    index_html = open(os.path.join(fixture_paths["out"], "index.html"), encoding="utf-8").read()
    assert "All Unverified Thing" in index_html
    # Must be in the "in progress" list, not rendered as a published card link.
    assert "all_unverified.html" not in index_html.rstrip('">')  # no href to a page that doesn't exist
    assert 'href="all_unverified.html"' not in index_html


# ── status badges ────────────────────────────────────────────────────────────

def test_mixed_page_shows_unverified_badge_only_on_unverified_entry(fixture_paths):
    bes.build_site(fixture_paths["yaml"], fixture_paths["csv"], fixture_paths["out"], fixture_paths["orgs"])
    page = open(os.path.join(fixture_paths["out"], "mixed_intervention.html"), encoding="utf-8").read()
    parses_cleanly(page)
    assert "UNVERIFIED" in page
    assert "Solid Registry" in page and "Shaky Source" in page
    # The verified entry's own card must not carry the UNVERIFIED badge text.
    solid_card = re.search(r"<div class='ev-card[^']*'><h3>Solid Registry</h3>.*?</div>", page, re.S)
    assert solid_card is not None
    assert "UNVERIFIED" not in solid_card.group(0)


def test_editorial_status_renders_as_prominent_note_not_badge(fixture_paths):
    # emergency_shelter-style category-level entry: use all_unverified's sibling
    # by adding an editorial-only intervention via a fresh fixture inline.
    yaml_text = FIXTURE_YAML + """
  - id: editorial_only
    name: Editorial Only Thing
    aliases: []
    mechanism: A category, not a branded model.
    evidence:
      - source: (category-level)
        finding: This is a service category, not an evaluated model.
        status: editorial_position
    canadian_relevance: n/a
"""
    yaml_path = os.path.join(os.path.dirname(fixture_paths["yaml"]), "with-editorial.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(yaml_text)
    bes.build_site(yaml_path, fixture_paths["csv"], fixture_paths["out"], fixture_paths["orgs"])
    page = open(os.path.join(fixture_paths["out"], "editorial_only.html"), encoding="utf-8").read()
    parses_cleanly(page)
    assert "ev-card editorial" in page
    assert "This is a service category, not an evaluated model." in page
    assert "UNVERIFIED" not in page


# ── org receipts ─────────────────────────────────────────────────────────────

def test_org_receipt_drawer_contains_snippet_and_ref_number(fixture_paths):
    bes.build_site(fixture_paths["yaml"], fixture_paths["csv"], fixture_paths["out"], fixture_paths["orgs"])
    page = open(os.path.join(fixture_paths["out"], "fully_verified.html"), encoding="utf-8").read()
    assert "Funding to support Existing Org Inc" in page
    assert "REF-0001" in page


def test_org_link_only_rendered_when_target_file_exists(fixture_paths):
    bes.build_site(fixture_paths["yaml"], fixture_paths["csv"], fixture_paths["out"], fixture_paths["orgs"])
    page = open(os.path.join(fixture_paths["out"], "fully_verified.html"), encoding="utf-8").read()
    assert "../orgs/existing-org-inc.html" in page
    assert "../orgs/nonexistent-org-inc.html" not in page


# ── scale cap ────────────────────────────────────────────────────────────────

def test_scale_cap_60_rows_shows_50_and_rollup_note(tmp_path):
    yaml_path = tmp_path / "evidence-spine-seed.yaml"
    yaml_path.write_text(FIXTURE_YAML, encoding="utf-8")
    csv_path = tmp_path / "seed-classifications.csv"
    rows = [make_csv_row("fully_verified", f"Org Number {i}", 1000 * (60 - i), f"REF-{i:04d}") for i in range(60)]
    write_csv(csv_path, rows)
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    out_dir = tmp_path / "out"

    bes.build_site(str(yaml_path), str(csv_path), str(out_dir), str(orgs_dir))
    page = open(out_dir / "fully_verified.html", encoding="utf-8").read()
    assert page.count("<td class='num'>") // 2 == bes.ORG_SCALE_CAP  # 2 num cells per row (grants, total)
    assert "Showing 50 of 60 organizations" in page


# ── draft treatment ──────────────────────────────────────────────────────────

def test_draft_treatment_present_in_every_generated_file(fixture_paths):
    written = bes.build_site(fixture_paths["yaml"], fixture_paths["csv"], fixture_paths["out"], fixture_paths["orgs"])
    assert len(written) >= 3
    for path in written:
        page = open(path, encoding="utf-8").read()
        assert "[DRAFT]" in page
        assert bes.DRAFT_BANNER_TEXT in page
        assert bes.DRAFT_FULL_TEXT in page
        assert "draft-watermark" in page


# ── structural checks (reused from test_org_page.py) ─────────────────────────

def test_all_pages_parse_and_drawers_are_reachable(fixture_paths):
    written = bes.build_site(fixture_paths["yaml"], fixture_paths["csv"], fixture_paths["out"], fixture_paths["orgs"])
    for path in written:
        page = open(path, encoding="utf-8").read()
        assert_parses_cleanly(page)
        assert_drawers_are_reachable(page)


def test_no_unreplaced_template_tokens(fixture_paths):
    written = bes.build_site(fixture_paths["yaml"], fixture_paths["csv"], fixture_paths["out"], fixture_paths["orgs"])
    for path in written:
        page = open(path, encoding="utf-8").read()
        assert "{title}" not in page and "{css}" not in page and "{banner}" not in page
        assert "{full_text}" not in page and "{generated}" not in page and "{js}" not in page


# ── status parsing helpers ───────────────────────────────────────────────────

def test_parse_status_verified_with_date():
    parsed = bes.parse_status("verified_2024-05-06")
    assert parsed == {"kind": "verified", "date": "2024-05-06", "raw_suffix": "2024-05-06"}


def test_parse_status_verified_without_parseable_date():
    parsed = bes.parse_status("verified_tonight")
    assert parsed["kind"] == "verified"
    assert parsed["date"] is None
    assert parsed["raw_suffix"] == "tonight"


def test_parse_status_unverified_and_editorial():
    assert bes.parse_status("from_model_knowledge") == {"kind": "unverified"}
    assert bes.parse_status("editorial_position") == {"kind": "editorial"}


def test_parse_status_unknown_raises():
    with pytest.raises(ValueError):
        bes.parse_status("something_else")


# ── real-file integration ────────────────────────────────────────────────────

@pytest.mark.skipif(not os.path.exists(bes.YAML_PATH), reason="real evidence files not present")
def test_integration_builds_real_site(tmp_path):
    out_dir = tmp_path / "out"
    written = bes.build_site(out_dir=str(out_dir))
    assert len(written) >= 2
    index = open(out_dir / "index.html", encoding="utf-8").read()
    assert_parses_cleanly(index)
