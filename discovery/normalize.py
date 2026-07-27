"""Name + address normalization for the discovery module.

Reuses analysis.build_entity_graph's normalize_name()/display_name()/BN
helpers rather than reimplementing them -- that module is safely importable
(main() is guarded; tests/ already does `from analysis.build_entity_graph
import ...`), and having two independent normalizers would let the same org
name collapse to different match keys depending which pipeline touched it.

The one deliberate divergence: DISCOVERY_LEGAL_SUFFIXES adds "ENR" (Quebec's
common registered-sole-proprietorship suffix, e.g. "Jean Tremblay Enr.") on
top of the repo's existing LEGAL_SUFFIXES set, since the spec calls it out
and REQ data will contain it far more often than grants.csv/T3010 ever did.
"""
import re
import sys
import os
from functools import lru_cache

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unidecode import unidecode

from analysis.build_entity_graph import (  # noqa: E402,F401 -- normalize_bn re-exported; discovery/ingest/cra.py imports it from here, not from analysis.build_entity_graph directly
    normalize_bn,
    LEGAL_SUFFIXES,
)

DISCOVERY_LEGAL_SUFFIXES = LEGAL_SUFFIXES | {"ENR"}

_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]")
_WS_RE = re.compile(r"\s+")


def normalize_org_name(raw):
    """Same tokenization as analysis.build_entity_graph.normalize_name()
    (bilingual pipe split, unidecode, uppercase, strip punctuation) but
    against DISCOVERY_LEGAL_SUFFIXES instead of the base LEGAL_SUFFIXES --
    normalize_name() isn't parameterized by suffix set, so this mirrors its
    body rather than wrapping it. Keep the two in sync if that logic changes."""
    if not raw:
        return ""
    s = str(raw)
    if "|" in s:
        s = s.split("|", 1)[0]
    s = unidecode(s).upper()
    s = _NON_ALNUM_RE.sub(" ", s)
    tokens = [t for t in s.split() if t not in DISCOVERY_LEGAL_SUFFIXES]
    return " ".join(tokens) if tokens else " ".join(s.split())


# ── fuzzy-match-only normalization (discovery/match.py) ──────────────────────
# Deliberately NOT folded into normalize_org_name() above: that function is
# used for other purposes (see its own docstring) and this repo's tests pin
# it to match analysis.build_entity_graph.normalize_name() exactly (minus the
# ENR suffix) -- changing its stopword set would ripple into the main entity
# graph's already-validated fuzzy thresholds. This is a separate, more
# aggressive normalization used ONLY when computing a match score.

# Pure connector words with zero distinguishing value -- same rationale as
# the DE/DU/LA/LE/LES/ET/OF/THE already in LEGAL_SUFFIXES, filling in ones
# that set doesn't cover (confirmed via real token-frequency counts below).
MATCHING_CONNECTOR_WORDS = {"DES", "POUR", "EN", "AU", "AUX"}

# Generic institutional/category words, empirically identified as appearing
# in >=1% of a combined 22,694-name sample (16,237 Montreal REQ NPOs + 6,457
# Montreal CRA charities) -- common enough across genuinely different
# organizations that they add no distinguishing signal to a fuzzy name score,
# the same problem LEGAL_SUFFIXES already solves for "INC"/"FOUNDATION"/etc,
# just domain-specific Quebec-nonprofit vocabulary that set doesn't cover.
# Confirmed necessary on real data: "CENTRE DE LA PETITE ENFANCE ORIGAMI" vs
# "...DE MCGILL" scored 84/100 under token_sort_ratio (an improvement over
# the old token_set_ratio, but still wrong) purely from sharing CENTRE +
# PETITE + ENFANCE -- all three needed to be stripped before the real
# distinguishing word (ORIGAMI vs MCGILL) could dominate the score.
MATCHING_CATEGORY_WORDS = {
    "MONTREAL", "QUEBEC", "CANADA", "CENTRE", "EGLISE", "CLUB", "SAINT", "ST",
    "COMMUNAUTAIRE", "MAISON", "PETITE", "ENFANCE", "ECOLE", "CONGREGATION",
    "INSTITUT", "QUEBECOISE", "PAROISSE", "THEATRE", "INTERNATIONAL", "CHURCH",
    "FABRIQUE", "EVANGELIQUE", "SANTE", "GROUPE", "FEDERATION", "DEVELOPPEMENT",
    "COMMUNAUTE", "SYNDICAT", "FONDS", "ACTION",
}

MATCHING_STOPWORDS = MATCHING_CONNECTOR_WORDS | MATCHING_CATEGORY_WORDS


@lru_cache(maxsize=200_000)
def normalize_for_scoring(raw):
    """A more aggressive normalization used ONLY for fuzzy match scoring,
    never for display or entity keys. Layers two things on top of
    normalize_org_name(): (1) drops single-character tokens --
    normalize_org_name's punctuation-to-space step turns a French elision
    like "L'Institut" into two tokens ("L", "INSTITUT"), leaving a spurious
    single-letter "word" that inflated similarity scores for otherwise-
    unrelated names sharing nothing but a leftover "L"/"D"/"S" (confirmed:
    these three letters alone showed up as if they were shared tokens in
    1-11% of a 22,694-name sample); (2) strips MATCHING_STOPWORDS.

    Cached: score_names() calls this fresh on both sides of every discovery-
    record x candidate comparison inside a matching block, so the same
    handful of thousand distinct names get re-normalized tens of millions of
    times over a province-wide run otherwise -- confirmed as the actual
    bottleneck (city-level blocks like Montreal's ~20k discovery records x
    ~4k CRA charities) when scaling the pilot from Montreal-only to all of
    Quebec."""
    base = normalize_org_name(raw)
    tokens = [t for t in base.split() if len(t) > 1 and t not in MATCHING_STOPWORDS]
    return " ".join(tokens) if tokens else base


def normalize_postal(raw):
    """6-char, no-space, uppercase Canadian postal code, or None."""
    if not raw:
        return None
    s = re.sub(r"\s+", "", str(raw)).upper()
    return s if re.match(r"^[A-Z]\d[A-Z]\d[A-Z]\d$", s) else None


def fsa(postal):
    """First 3 chars of a normalized postal code (forward sortation area)."""
    return postal[:3] if postal else None
