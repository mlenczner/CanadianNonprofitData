"""
Regression tests for a correctness bug in analysis/build_entity_graph.py:
HTML markup and entities in raw source names defeated entity resolution.

22+ entities (56 confirmed against a real rebuild) had literal HTML in
canonical_name, e.g. "<span lang='fr' xml:lang='fr'>F&eacute;d&eacute;ration
acadienne de la Nouvelle-&Eacute;cosse</span>" -- because normalize_name()/
display_name() never decoded this, "Fédération acadienne de la
Nouvelle-Écosse" existed as at least 4 separate entities (span-wrapped,
HTML-entity-encoded with no tags, and two clean variants). Fixed by
clean_html() (strip tags, decode entities repeatedly since some sources
double-encode, collapse whitespace), called as the first step of both
normalize_name() (match key) and display_name() (canonical_name/display
value).

Run with:
    .venv/bin/python -m pytest tests/test_name_hygiene.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.build_entity_graph import clean_html, display_name, normalize_name


# ── clean_html() ──────────────────────────────────────────────────────────

def test_clean_html_strips_span_wrapped_name():
    raw = "<span lang='fr' xml:lang='fr'>F&eacute;d&eacute;ration acadienne de la Nouvelle-&Eacute;cosse</span>"
    assert clean_html(raw) == "Fédération acadienne de la Nouvelle-Écosse"


def test_clean_html_decodes_entities_with_no_tags():
    raw = "F&eacute;d&eacute;ration acadienne de la Nouvelle-&Eacute;cosse"
    assert clean_html(raw) == "Fédération acadienne de la Nouvelle-Écosse"


def test_clean_html_decodes_double_encoded_entities():
    # "&amp;eacute;" decodes to "&eacute;" on the first pass, "é" on the second.
    raw = "F&amp;eacute;d&amp;eacute;ration"
    assert clean_html(raw) == "Fédération"


def test_clean_html_strips_tags_with_no_entities():
    raw = "<b>Canadian Red Cross</b>"
    assert clean_html(raw) == "Canadian Red Cross"


def test_clean_html_collapses_whitespace_left_by_tag_removal():
    raw = "Some<br/>Org<br/>Name"
    assert clean_html(raw) == "Some Org Name"


def test_clean_html_is_noop_on_a_clean_name():
    assert clean_html("Canadian Red Cross Society") == "Canadian Red Cross Society"


def test_clean_html_handles_none_and_empty():
    assert clean_html(None) is None
    assert clean_html("") == ""


# ── normalize_name() / display_name() wiring ────────────────────────────────

def test_normalize_name_collapses_span_wrapped_and_clean_variants():
    span_wrapped = "<span lang='fr' xml:lang='fr'>F&eacute;d&eacute;ration acadienne de la Nouvelle-&Eacute;cosse</span>"
    entity_encoded = "F&eacute;d&eacute;ration acadienne de la Nouvelle-&Eacute;cosse"
    clean_mixed = "Fédération acadienne de la Nouvelle-Écosse"
    clean_upper = "FÉDÉRATION ACADIENNE DE LA NOUVELLE-ÉCOSSE"
    keys = {normalize_name(n) for n in (span_wrapped, entity_encoded, clean_mixed, clean_upper)}
    assert len(keys) == 1, f"expected all four variants to normalize to one key, got {keys}"


def test_display_name_strips_tags_and_decodes_entities_but_keeps_case_and_accents():
    raw = "<span lang='fr' xml:lang='fr'>F&eacute;d&eacute;ration acadienne de la Nouvelle-&Eacute;cosse</span>"
    assert display_name(raw) == "Fédération acadienne de la Nouvelle-Écosse"


def test_display_name_still_splits_bilingual_pipe_after_html_cleanup():
    raw = "<b>English Name</b>|Nom fran&ccedil;ais"
    assert display_name(raw) == "English Name"


def test_normalize_name_still_strips_legal_suffixes_after_html_cleanup():
    raw = "<span>Canadian Red Cross Society</span>"
    assert "SOCIETY" not in normalize_name(raw).split()
