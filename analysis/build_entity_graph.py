"""
Canadian Nonprofit Data — Entity Graph Builder
Links federal Grants & Contributions (grants.csv), the CRA T3010 charity
registry, Canada Council for the Arts grants, and Ontario Trillium
Foundation grants (all under data/) into one entity graph, so the same
organization is recognized as funder and/or recipient across all sources
regardless of which name variant it appears under. See
docs/entity-resolution-methodology.md for the approach.

Run with: python analysis/build_entity_graph.py
"""

import csv
import glob
import html
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import NamedTuple, Optional

import duckdb
from rapidfuzz import fuzz, process
from unidecode import unidecode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRANTS_CSV = os.path.join(ROOT, "grants.csv")
DATA_DIR = os.path.join(ROOT, "data")
OTF_CSV = os.path.join(DATA_DIR, "otf_grants.csv")
DB_PATH = os.path.join(ROOT, "nonprofit_network.duckdb")
OUTPUT_DIR = os.path.join(ROOT, "analysis", "output")
BN_MERGE_OVERRIDES_PATH = os.path.join(DATA_DIR, "bn_merge_overrides.csv")
BN_NEAR_MISS_REVIEW_PATH = os.path.join(OUTPUT_DIR, "bn_near_miss_review.csv")
BRANCH_VARIANT_REVIEW_PATH = os.path.join(OUTPUT_DIR, "branch_variant_review.csv")

FUZZY_ACCEPT = 90   # auto-accept threshold (token_sort_ratio, 0-100)
FUZZY_REVIEW = 80   # below this, treat as unmatched

LEGAL_SUFFIXES = {
    "INC", "INCORPORATED", "LTD", "LIMITED", "LTEE", "CORP", "CORPORATION",
    "FOUNDATION", "FONDATION", "SOCIETY", "SOCIETE", "ASSOCIATION", "ASSOC",
    "ORGANIZATION", "ORGANISATION", "TRUST", "CHARITABLE", "CHARITY", "CHARITE",
    "OF", "THE", "DE", "DU", "LA", "LE", "LES", "AND", "ET",
}

BN9_RE = re.compile(r"^\d{9}$")
# Program-account suffix restricted to the CRA-documented codes actually seen
# in this data (RR = registered charity, RT = GST/HST, RP = payroll, RC =
# corporate income tax) -- normalize_bn() used to accept *any* 2-letter code
# here, which is looser than the ingestion spec calls for and could silently
# accept a malformed non-BN string that happened to match the shape.
BN_PROGRAM_ACCOUNT_RE = re.compile(r"^\d{9}(?:RR|RT|RP|RC)\d{4}$")
BN_ARC_CRA_PREFIX_RE = re.compile(r"^(?:ARC|CRA)\s*")
YEAR_RE = re.compile(r"^(?:18|19|20)\d{2}$")  # plausible incorporation/founding year

# OTF's Charitable Registration Number column is deliberately validated
# stricter than normalize_bn() above (which accepts any 2-letter program-
# account code, e.g. RP, and strips internal spaces before checking) --
# docs/otf-ingestion-spec.md calls for only a bare 9-digit root or an
# RR-suffixed 15-char BN, checked after trimming (not de-spacing) the field,
# so free-text entries like "10754 2870 RP0001" (a payroll account code, not
# a charity registration) or "834767352RR001" (3-digit suffix) are discarded
# rather than accidentally accepted by the looser general-purpose rule.
OTF_CRN_RE = re.compile(r"^\d{9}(RR\d{4})?$")

GRANTS_NFP_TYPES = {"N", "A", "S"}  # NFP/charity, Indigenous, academic


# ── normalization helpers ────────────────────────────────────────────────────

def normalize_bn(raw):
    """Reduce a CRA business number, in any of the raw formats entity_links
    sources actually use ("132162041RT0001", "ARC 132162041 RT 0001", bare
    9-digit, spaced/hyphenated/period-punctuated variants), to its 9-digit
    root, since one organization can hold multiple program accounts
    (RR0001, RR0002, ...). Normalization only: uppercases, strips a leading
    ARC/CRA token and all whitespace/periods/hyphens, then accepts a bare
    9-digit root or a 9-digit root plus an RR/RT/RP/RC program-account
    suffix (BN_PROGRAM_ACCOUNT_RE) -- anything else (wrong digit count,
    garbage, a postal code, an unrecognized program code) is rejected as
    unparseable rather than repaired or guessed at. In particular this does
    NOT try to detect or fix a corrupted-but-plausible root (e.g. a leading
    zero inserted by some upstream system that treated the BN as a number)
    -- see bn_reject_shape() for reject classification and
    data/bn_merge_overrides.csv for the human-reviewed fix path for BNs that
    turn out to be the same org under a source typo."""
    if not raw:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    s = BN_ARC_CRA_PREFIX_RE.sub("", s)
    compact = re.sub(r"[\s.\-]", "", s)
    if not compact:
        return None
    if BN_PROGRAM_ACCOUNT_RE.match(compact) or BN9_RE.match(compact):
        return compact[:9]
    return None


def bn_reject_shape(raw):
    """Coarse shape descriptor for a raw BN that normalize_bn() rejected,
    used only to bucket reject counts in the build report (e.g. "8 digits",
    "10 digits", "non-numeric") so new/unexpected raw BN formats surface as
    a labeled count instead of disappearing into one opaque total. Not used
    for any matching decision."""
    s = str(raw).strip().upper()
    s = BN_ARC_CRA_PREFIX_RE.sub("", s)
    compact = re.sub(r"[\s.\-]", "", s)
    if not compact:
        return "blank"
    if compact.isdigit():
        return f"{len(compact)} digits"
    return "non-numeric"


def clean_html(raw):
    """Strip HTML tags and decode HTML entities from a raw name, repeatedly
    (some sources double-encode, e.g. "&amp;eacute;"), then collapse
    whitespace. A handful of source records carry literal markup --
    entity 101428's raw name is
    "<span lang='fr' xml:lang='fr'>F&eacute;d&eacute;ration acadienne de la
    Nouvelle-&Eacute;cosse</span>" -- and since normalize_name()/
    display_name() never decoded this, the same organization split into
    up to 4 separate entities (tag-wrapped, entity-encoded-no-tags, and two
    clean variants). Applied as the first step of both functions below so
    every entity-minting call site is covered by this one change."""
    if not raw:
        return raw
    s = re.sub(r"<[^>]+>", " ", str(raw))
    prev = None
    while prev != s:
        prev = s
        s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_name(raw):
    if not raw:
        return ""
    s = clean_html(raw)
    if "|" in s:
        # grants.csv recipient names are often bilingual, "English Name|Nom
        # français" -- match on the English half only so both language
        # variants of the same org collapse to the same normalized name
        # instead of creating separate entities.
        s = s.split("|", 1)[0]
    s = unidecode(s).upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    tokens = [t for t in s.split() if t not in LEGAL_SUFFIXES]
    return " ".join(tokens) if tokens else " ".join(s.split())


def display_name(raw):
    """Strip the bilingual pipe format ("English Name|Nom français") down to
    its English half for storage as canonical_name, mirroring what
    normalize_name() already does for the match key -- otherwise an entity
    resolved from a pipe-formatted source record displays the raw
    "Name|Nom" string verbatim instead of a clean name (AGENTS.md issue #3).
    Unlike normalize_name(), this doesn't touch case, punctuation, or legal
    suffixes -- canonical_name is a display value, not a match key."""
    if not raw:
        return raw
    s = clean_html(raw)
    return s.split("|", 1)[0].strip() if "|" in s else s


def name_variants(raw_name):
    """A charity's legal name, plus (if it contains a bilingual "/"
    separator) each half on its own -- so a fuzzy candidate is discoverable
    under whichever language a donee-schedule record happens to name it in,
    not just the full concatenated bilingual string. Confirmed necessary:
    POP Montreal International Music Festival's CRA legal name is
    ".../FESTIVAL INTERNATIONAL DE MUSIQUE POP MONTREAL" -- a donee record
    naming it in English only ("POP Montreal") scores 25-65 token_sort_ratio
    against the full bilingual string, well under FUZZY_ACCEPT, regardless of
    blocking. Mirrors discovery/match.py's _name_variants(), which found the
    same fix necessary for its own (separate) CRA-name fuzzy matcher."""
    variants = [raw_name]
    if raw_name and "/" in raw_name:
        variants.extend(h.strip() for h in raw_name.split("/") if h.strip())
    return variants



# federal_gc (recipient_type='P') and Canada Council (Recipient Type=
# 'Individual') both carry an explicit field marking a recipient as a
# person rather than an organization, checked directly at their ingestion
# sites below. T3010's non-qualified-donee schedule has no such field --
# just a name -- but individual donees there have a distinct raw shape:
# "Lastname  Firstname" (two words separated by 2+ spaces, no comma; the
# comma an upstream CRA export step apparently drops, confirmed against
# real rows -- e.g. "McConnell  Joanne" here vs. "McConnell, Joanne" in the
# same person's Canada Council record), sometimes with a trailing
# "(nickname)" parenthetical ("Reid  Margaret (Peggy)"). Sampled all 21,462
# distinct raw names across data/t3010/non_qualified_donees_*.csv: 5,382
# match this shape; manually reviewing all of them found organization
# names never take it (real orgs consistently have 3+ words, a legal
# suffix, or normal single-space/punctuated names) except one confirmed
# false positive ("KCA   Niisaachewan(Dalles)", a community name, not a
# person) -- accepted given the alternative is what this filter exists to
# fix. Deliberately does NOT match single-space two-word names ("Red
# Cross"), which are indistinguishable from real short org names.
INDIVIDUAL_DONEE_NAME_RE = re.compile(r"^[A-Za-zÀ-ÿ'\-]+  +[A-Za-zÀ-ÿ'\-]+(\s*\([^)]*\))?$")


def looks_like_individual_donee_name(name):
    return bool(name) and bool(INDIVIDUAL_DONEE_NAME_RE.match(name.strip()))


# federal_gc's recipient_type is missing entirely (blank/NULL) for 325,252
# of ~1.3M raw rows -- recipient_type='P' (checked directly at the
# ingestion site) only catches individuals the owning department bothered
# to code as such, and a real gap remains among the blank ones: e.g.
# "McConnell, Erin (McMaster University)" and "McConnell, Jennifer S" (both
# real academic-grant recipients) carry no recipient_type at all. These take
# a "Lastname, Firstname [Middle] [(Affiliation)]" shape -- verified by
# sampling all 147,215 distinct blank-recipient_type names: 28,330 match
# after two guards, both confirmed necessary by real counterexamples found
# during that sampling: (1) the token after the comma must start with a
# capital letter, or names like "AdMare, centre d'artistes..." and
# "Rouyn-Noranda, ville et village en sante inc." (real Quebec orgs whose
# name takes "Name, lowercase description" form) false-positive -- this
# does mean a handful of real individuals whose name happens to be entered
# lowercase ("banquy, xavier") are NOT caught, an accepted residual gap, not
# chased further; (2) the token after the comma must not be a corporate
# suffix (Inc/Ltd/Corp/LLC/...), or "Sumalytics, Inc." false-positives as a
# person named "Sumalytics Inc". Deliberately NOT applied when
# recipient_type carries an explicit non-blank value (even non-'P' ones):
# a spot check of the same shape under explicit N/F/G/O/S/A/I codes found a
# real false positive there too ("L'Avenue, justice alternative", typed
# 'N') sitting alongside real misclassified individuals ("Furdyk, Michael",
# typed 'N') -- an explicit department declaration is a stronger, if
# imperfect, organization signal than a blank field, so overriding it here
# isn't worth the added false-positive risk; that bucket is a known,
# undressed residual gap.
INDIVIDUAL_RECIPIENT_COMMA_RE = re.compile(
    r"^[A-Za-zÀ-ÿ'\-]+,\s*[A-ZÀ-Þ][A-Za-zÀ-ÿ'\-\.]*(\s+[A-ZÀ-Þ][A-Za-zÀ-ÿ'\-\.]*)?(\s*\([^)]*\))?$"
)
CORP_NAME_SUFFIXES = {
    "INC", "LTD", "LTEE", "LTÉE", "CORP", "LLC", "LLP", "CO",
    "LIMITED", "INCORPORATED", "ULC", "SA", "SARL", "SENC",
}


def looks_like_individual_name_comma(name):
    if not name:
        return False
    m = INDIVIDUAL_RECIPIENT_COMMA_RE.match(name.strip())
    if not m:
        return False
    first_token = m.group(0).split(",", 1)[1].strip().split()[0]
    return first_token.upper().rstrip(".") not in CORP_NAME_SUFFIXES


def block_key(province, norm_name):
    prov = (province or "").strip().upper()[:2]
    prefix = norm_name[:4] if norm_name else ""
    return f"{prov}|{prefix}"


def name_prefix(norm_name):
    return norm_name[:4] if norm_name else ""


# T3010 donee-name fields aren't reliably truncated at one fixed length (some
# filing years/pathways allow well over 100 chars), but a large cluster of
# names (34,395 distinct raw_names in the current build) hits exactly this
# length -- confirmed, e.g., across multiple independent filings for the same
# Saskatoon school division, each cut off mid-word right after a digit
# ("...No. 13 T", truncated before "TRUST FUND"). A name landing exactly on
# this cliff is treated as a truncation risk; anything shorter or longer is not.
RAW_NAME_TRUNCATION_LEN = 60


def _fuse_digit_letter_tokens(tokens, raw_len=None):
    """Join a standalone single-letter token onto an immediately-preceding
    pure-digit token, so 'CIRCUIT 1 B' (produced from a raw '1-B' or '1 B'
    once normalize_name turns the hyphen/extra space into a plain space)
    collapses to the same 'CIRCUIT 1B' that a source writing '1B' fused
    already yields. Leaves an unrelated standalone letter alone (e.g. the
    possessive 'S' in 'JEHOVAH S') since the preceding token isn't pure-digit,
    and never touches an already-fused multi-char token like '11B'.

    Exception: if this is the trailing token of a raw name truncated at
    exactly RAW_NAME_TRUNCATION_LEN characters, it's far more likely to be
    the first letter of a cut-off word (e.g. '13' + 'T' from '...No. 13
    TRUST FUND') than a genuine branch/circuit suffix -- fusing it would
    produce a digit token ('13T') that can never match the untruncated
    name's bare '13'. Dropped instead of fused, since a single truncated
    letter carries no reliable information either way."""
    out = []
    n = len(tokens)
    for i, tok in enumerate(tokens):
        is_truncated_tail = i == n - 1 and raw_len == RAW_NAME_TRUNCATION_LEN
        if len(tok) == 1 and tok.isalpha() and out and out[-1].isdigit():
            if not is_truncated_tail:
                out[-1] = out[-1] + tok
            # else: drop the letter -- neither fused nor kept as its own token
        else:
            out.append(tok)
    return out


def digit_tokens(norm_name, raw_len=None):
    """Whitespace tokens containing a digit (e.g. '5A', '60', '1992') from an
    already normalize_name()-processed string, after fusing split digit+
    single-letter suffixes ('1 B' -> '1B'). Gates fuzzy matches: two org names
    differing only in a branch/circuit/chapter number (Alberta Circuit '5A'
    vs '7A' of Jehovah's Witnesses) must not fuzzy-match no matter how high
    token_sort_ratio scores the rest of the name. Deliberately splits on
    whitespace rather than a \\d+ regex, since a regex would collapse '5A' to
    '5' and fail to distinguish it from '5B' or '7A'. Year-like tokens are
    kept here and handled by digit_tokens_match() at comparison time.
    `raw_len` (the pre-normalization raw name length, if known) is passed
    through to _fuse_digit_letter_tokens() to detect the truncation case."""
    fused = _fuse_digit_letter_tokens(norm_name.split(), raw_len=raw_len)
    return frozenset(t for t in fused if any(ch.isdigit() for ch in t))


def digit_tokens_match(a, b):
    """Gate test for whether two digit-token sets represent the same branch/
    circuit/chapter identity. Non-year tokens must match exactly. A year-like
    token (1800-2099) is treated as an incidental incorporation/founding year
    embedded in a legal name (e.g. 'Soup Kitchen Association 2013') and
    ignored when only one side carries one; when BOTH sides carry a
    (differing) year it's kept as a differentiator, so two same-named orgs
    distinguished only by year aren't merged."""
    a_years = frozenset(t for t in a if YEAR_RE.match(t))
    b_years = frozenset(t for t in b if YEAR_RE.match(t))
    if (a - a_years) != (b - b_years):
        return False
    if a_years and b_years:
        return a_years == b_years
    return True


def fiscal_year_from_date(date_str, month_cutover=4):
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(str(date_str).strip(), fmt)
            return d.year if d.month >= month_cutover else d.year - 1
        except Exception:
            continue
    return None


def to_float(raw):
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace(",", "").replace("$", "").strip())
    except Exception:
        return None


def _note_parse_failure(counters, key, raw):
    """Increment counters[key] when raw was non-blank but the caller's
    to_float()/fiscal_year_from_date() call on it came back None -- a genuine
    parse failure, not merely an absent field. Every other silent-drop risk
    in this file is counted and printed (T3010 ignore_errors rejects, OTF CRN
    discards, rescinded-floor count); to_float()/fiscal_year_from_date() were
    the one place that wasn't, despite feeding the same dollar-value
    calculations. Call only from build_entities_and_grants()'s per-source
    loops, after checking the parsed result is None -- not from to_float()/
    fiscal_year_from_date() themselves, since those are also called from
    org_page.py at render time, a different context this pipeline-build
    report shouldn't count towards."""
    if raw is not None and str(raw).strip() != "":
        counters[key] += 1


def validate_otf_crn(raw):
    """Strip whitespace and accept only a bare 9-digit root or an RR-suffixed
    15-char BN (OTF_CRN_RE); returns the 9-digit bn_root, or None for anything
    else (blank, absent, or malformed/free-text -- see OTF_CRN_RE above).
    Deliberately does not reuse normalize_bn(), which is looser than the
    ingestion spec calls for here."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if OTF_CRN_RE.match(s):
        return s[:9]
    return None


def otf_fiscal_year(raw):
    """OTF's 'Fiscal Year' column is a string like '1999-2000' -- take the
    start year, matching the canada_council convention (cc_year[:4]) elsewhere
    in this file."""
    if raw and raw[:4].isdigit():
        return int(raw[:4])
    return None


def otf_net_amount(awarded, rescinded):
    """amount_cad = awarded - COALESCE(rescinded, 0), floored at 0 -- reflects
    money that actually flowed net of anything rescinded/recovered, same
    reasoning as the amendment-dedup fix (AGENTS.md issue #3): grants_unified
    records flows, not announcements. Returns (net_amount, was_floored) so the
    caller can count/print the rare case where rescinded exceeds awarded
    rather than letting a negative amount into grants_unified silently."""
    net = awarded - (rescinded or 0)
    if net < 0:
        return 0.0, True
    return net, False


# ── entity resolution state ─────────────────────────────────────────────────

class EntityRow(NamedTuple):
    entity_id: int
    bn_root: Optional[str]
    canonical_name: str
    city: Optional[str]
    province: Optional[str]
    entity_kind: str


class FuzzyCandidate(NamedTuple):
    norm_name: str
    digit_tokens: frozenset
    entity_id: int


class EntityLink(NamedTuple):
    entity_id: int
    source_dataset: str
    raw_name: str
    raw_bn: Optional[str]
    match_method: str
    match_score: Optional[float]


class GateReject(NamedTuple):
    raw_name: str
    rejected_canonical_name: str
    score: float
    source_dataset: str


class Resolver:
    """Holds the growing entity table plus lookup indexes, and resolves a
    (name, bn, province) triple to an entity_id via exact BN match, fuzzy
    name match (blocked by province+name-prefix), or a new residual entity."""

    def __init__(self):
        self.next_id = 1
        self.entities = []  # [EntityRow, ...]
        self.bn_to_entity = {}
        self.dept_to_entity = {}
        self.residual_to_entity = {}
        self.fuzzy_index = defaultdict(list)  # block_key -> [FuzzyCandidate, ...]
        self.fuzzy_index_by_prefix = defaultdict(list)  # name_prefix -> [FuzzyCandidate, ...], all provinces
        self.links = []  # [EntityLink, ...]
        self.gate_rejects = []  # [GateReject, ...] — candidates that scored >= FUZZY_ACCEPT but
                                 # were split apart by the digit-token gate; sampled for QA in print_report
        self.stats = defaultdict(int)
        self.bn_reject_counts = defaultdict(int)  # bn_reject_shape() -> count, printed in the build report

    def new_id(self):
        eid = self.next_id
        self.next_id += 1
        return eid

    def note_bn_reject(self, raw):
        """Record a non-blank raw BN that normalize_bn() couldn't parse to a
        9-digit root, bucketed by bn_reject_shape() so new/unexpected raw BN
        formats surface in the build report as a labeled count instead of
        disappearing into one opaque total. Blank/absent values (most BN
        fields, most of the time) are not "rejects" and are silently
        skipped, same convention as _note_parse_failure()."""
        if raw is None or str(raw).strip() == "":
            return
        self.bn_reject_counts[bn_reject_shape(raw)] += 1

    def add_charity(self, bn_root, legal_name, city, province):
        if bn_root in self.bn_to_entity:
            return self.bn_to_entity[bn_root]
        eid = self.new_id()
        self.bn_to_entity[bn_root] = eid
        self.entities.append(EntityRow(eid, bn_root, display_name(legal_name), city, province, "charity"))
        # Index every name variant (full name, plus each half of a bilingual
        # "English/French" name) as its own fuzzy candidate -- all pointing
        # at the same entity_id -- so a donee record naming this charity in
        # just one language is still discoverable. See name_variants().
        for variant in name_variants(legal_name):
            norm = normalize_name(variant)
            if not norm:
                continue
            raw_len = len(variant) if variant else None
            candidate = FuzzyCandidate(norm, digit_tokens(norm, raw_len=raw_len), eid)
            self.fuzzy_index[block_key(province, norm)].append(candidate)
            self.fuzzy_index_by_prefix[name_prefix(norm)].append(candidate)
        return eid

    def add_dept(self, code, name):
        code = code.strip().lower()
        if code in self.dept_to_entity:
            return self.dept_to_entity[code]
        eid = self.new_id()
        self.dept_to_entity[code] = eid
        self.entities.append(EntityRow(eid, None, display_name(name), None, None, "federal_dept"))
        return eid

    def add_funder_org(self, name):
        eid = self.new_id()
        self.entities.append(EntityRow(eid, None, display_name(name), None, None, "funder_org"))
        return eid

    def resolve(self, source_dataset, name, bn_raw, province, allow_fuzzy):
        root = normalize_bn(bn_raw)
        if root is None:
            self.note_bn_reject(bn_raw)
        if root and root in self.bn_to_entity:
            eid = self.bn_to_entity[root]
            self.stats["exact_bn"] += 1
            self.links.append(EntityLink(eid, source_dataset, name, bn_raw, "exact_bn", 100.0))
            return eid

        norm = normalize_name(name)
        if allow_fuzzy and norm:
            # T3010 donee-name fields routinely leave province blank (t3010_
            # non_qualified_donee has no province/city column in the CRA
            # source at all; t3010_qualified_donee's is frequently empty even
            # when a donee name is present) -- confirmed case: POP Montreal
            # International Music Festival, a real registered charity blocked
            # under "QC|POP ", was invisible to 3 of 4 donee-schedule records
            # naming it because those rows' blank province produced block key
            # "|POP " instead, an entirely different bucket. When the incoming
            # record has no province to block on, fall back to a province-
            # agnostic index (name-prefix only, all provinces) rather than
            # silently missing every charity whose real province we just don't
            # know from this record. Records that DO carry a province keep the
            # original tighter (province, prefix) blocking unchanged.
            if (province or "").strip():
                candidates = self.fuzzy_index.get(block_key(province, norm), [])
            else:
                candidates = self.fuzzy_index_by_prefix.get(name_prefix(norm), [])
            if candidates:
                q_nums = digit_tokens(norm, raw_len=len(name) if name else None)
                all_choices = [c.norm_name for c in candidates]

                # Would this record have fuzzy-matched before the digit-token
                # gate? Log it for QA sampling in print_report — a high
                # pre-gate score split apart by a differing branch/circuit/
                # chapter number is the gate doing its job; splitting a
                # genuine near-duplicate instead is the failure mode to watch for.
                pre_gate = process.extractOne(
                    norm, all_choices, scorer=fuzz.token_sort_ratio, score_cutoff=FUZZY_ACCEPT
                )
                if pre_gate:
                    _, pre_score, pre_idx = pre_gate
                    if not digit_tokens_match(candidates[pre_idx].digit_tokens, q_nums):
                        rejected_eid = candidates[pre_idx].entity_id
                        self.stats["digit_gate_reject"] += 1
                        self.gate_rejects.append(GateReject(
                            name, self.entities[rejected_eid - 1].canonical_name, pre_score, source_dataset
                        ))

                gated = [c for c in candidates if digit_tokens_match(c.digit_tokens, q_nums)]
                if gated:
                    choices = [c.norm_name for c in gated]
                    match = process.extractOne(
                        norm, choices, scorer=fuzz.token_sort_ratio, score_cutoff=FUZZY_REVIEW
                    )
                    if match:
                        _, score, idx = match
                        if score >= FUZZY_ACCEPT:
                            eid = gated[idx].entity_id
                            self.stats["fuzzy_accept"] += 1
                            self.links.append(EntityLink(eid, source_dataset, name, bn_raw, "fuzzy_accept", score))
                            return eid

        # residual: dedupe unmatched records by normalized name + province,
        # but never silently merge two different BNs into one entity just
        # because they share a normalized name + province (e.g. several
        # differently-BN'd "Port Authority"-style orgs). A BN found here is
        # registered in bn_to_entity so later records with the same BN
        # exact-match instead of falling through to name-based residual
        # dedup again and potentially creating a separate entity.
        rkey = (norm, (province or "").strip().upper()[:2])
        existing_eid = self.residual_to_entity.get(rkey)
        existing_bn = self.entities[existing_eid - 1].bn_root if existing_eid is not None else None

        if existing_eid is None:
            eid = self.new_id()
            self.residual_to_entity[rkey] = eid
            self.entities.append(EntityRow(eid, root, display_name(name), None, province, "other_org"))
            if root:
                self.bn_to_entity[root] = eid
        elif root and existing_bn and existing_bn != root:
            # Collision: same normalized name+province, but a different real
            # BN -- resolve/create a BN-specific entity rather than merging.
            bn_rkey = (rkey, root)
            eid = self.residual_to_entity.get(bn_rkey)
            if eid is None:
                eid = self.new_id()
                self.residual_to_entity[bn_rkey] = eid
                self.entities.append(EntityRow(eid, root, display_name(name), None, province, "other_org"))
                self.bn_to_entity[root] = eid
        else:
            eid = existing_eid
            if root and not existing_bn:
                # Backfill: attach this record's BN to the existing residual
                # entity and index it for future exact-BN matches.
                self.entities[eid - 1] = self.entities[eid - 1]._replace(bn_root=root)
                self.bn_to_entity[root] = eid
        self.stats["unmatched_new"] += 1
        self.links.append(EntityLink(eid, source_dataset, name, bn_raw, "unmatched_new", None))
        return eid


# ── pipeline stages ──────────────────────────────────────────────────────────

def _latest_amendment_sql(source_table):
    """SQL selecting only the latest-amendment row per (owner_org, ref_number)
    from a grants-shaped table. Amendment rows restate an agreement's current
    value rather than adding to it -- summing every row for a given agreement
    double/triple-counts the same dollars. Keeps the latest state per
    agreement (current amendment_number, treating missing/blank as 0 =
    original); amendment history stays fully queryable in the un-deduped
    source table, so nothing here is destructive.

    ref_number is NOT globally unique on its own: 24,851 refs collide across
    departments (e.g. GC-2016-Q4-00001 is six different grants -- different
    recipients, different values, different departments -- all at amendment
    0). Deduping by ref_number alone collapses 61,075 rows of genuinely
    distinct agreements ($41.3B) down to one arbitrary row each. The dedup
    key is (TRIM(owner_org), TRIM(ref_number)) -- verified zero multi-recipient
    groups remain at max amendment within that key. TRIM matters on both
    sides: at least one ref has a trailing space.

    Approximation, not a guarantee: docs/data-publishing-problems.md notes
    some departments publish deltas/negative amendments instead of restated
    totals, in which case "latest amendment" isn't strictly "current total"
    -- see docs/entity-resolution-methodology.md for the caveat."""
    return f"""
        SELECT * FROM {source_table}
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY TRIM(owner_org), TRIM(ref_number)
            ORDER BY COALESCE(TRY_CAST(NULLIF(TRIM(amendment_number), '') AS INTEGER), 0) DESC
        ) = 1
    """


def _dedup_t3010_qd_sql():
    """raw_t3010_qd contains genuine full-row duplicate lines within the same
    source file -- confirmed against a real rebuild: CanadaHelps' 2024
    qualified-donee schedule alone has 4,499 duplicate donee-gift lines out
    of 35,989 (e.g. its $3,224,402 gift to the Canadian Red Cross Society
    appears on two different line numbers, '#' 31480 and 35979, with every
    other field byte-identical) -- a chunk of one filer's schedule appended
    twice within the same CRA-published CSV, not an ingestion bug on our
    side. Confirmed across all 12 years: 22,351 duplicate groups / 62,067
    duplicate rows on a full-row-except-'#' key. A looser key (BN+FPE+donee+
    amount only) found more "duplicates" (23,649/65,228) but some of those
    differ in Gifts in Kind / Political Activity fields, so they're not true
    dupes -- this is the safe key. Keeps the lowest '#' per group
    deterministically. raw_t3010_qd itself is untouched -- this produces a
    derived table only, same non-destructive convention as
    _latest_amendment_sql()."""
    return """
        SELECT * FROM raw_t3010_qd
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY BN, FPE, "Form ID", "Donee BN", "Donee Name", Associated,
                         City, Province, "Total Gifts", "Gifts in Kind",
                         "Political Activity Gift", "Political Activity Amount",
                         filename, source_year
            ORDER BY TRY_CAST("#" AS INTEGER)
        ) = 1
    """


def _bn_full_update_sql():
    """entities.bn_full: the complete 15-char BN (with RR/RC/etc. program-
    account suffix), not just the 9-digit bn_root -- needed to deep-link to
    the CRA's own charity listing (which is keyed on the full BN, not the
    root). Sourced from raw_t3010_ident.BN, the latest source_year row per
    bn_root wins (ties/multiple filings resolved by "most recent filing
    wins", same convention build_entity_financials already uses for its own
    bn_full column). Factored into its own function, same testability
    convention as _dedup_t3010_qd_sql()/_latest_amendment_sql() -- the
    UPDATE...FROM pattern needs a real entities+raw_t3010_ident pair of
    tables to exercise, which a small fixture DB can provide without the
    rest of build_entities_and_grants()."""
    return """
        UPDATE entities SET bn_full = latest_ident.bn_full
        FROM (
            SELECT substr(regexp_replace(BN, '[^0-9A-Za-z]', '', 'g'), 1, 9) AS bn_root, BN AS bn_full
            FROM raw_t3010_ident
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY substr(regexp_replace(BN, '[^0-9A-Za-z]', '', 'g'), 1, 9)
                ORDER BY source_year DESC
            ) = 1
        ) AS latest_ident
        WHERE entities.bn_root = latest_ident.bn_root
    """


# Shared by _search_name_update_sql() (entities.search_name, populated at
# build time) and, in spirit, webapp.py's _fold_query() (folds an incoming
# search query the exact same way at request time) -- webapp.py doesn't
# import this constant directly (it has no other dependency on
# build_entity_graph.py), but both expressions must stay byte-identical or
# accent-insensitive search silently breaks; tests/test_bn_full_and_search_
# name.py asserts the two independently-written expressions actually agree
# at runtime rather than just trusting the docstrings match.
SEARCH_NAME_FOLD_EXPR = "lower(strip_accents(regexp_replace(trim({col}), '\\s+', ' ', 'g')))"


def _search_name_update_sql():
    """entities.search_name: canonical_name -> lowercased, accent-stripped,
    whitespace-collapsed -- lets live search match "ecole" against "École"
    and vice versa. Uses DuckDB's native strip_accents() rather than a
    second Python-side normalizer, so the query-time folding (webapp.py's
    _fold_query()) and this column use the exact same folding logic with
    nothing to keep in sync."""
    return f"UPDATE entities SET search_name = {SEARCH_NAME_FOLD_EXPR.format(col='canonical_name')}"


def _load_t3010_table(con, glob_pattern, table_name):
    """Load a T3010 CSV glob into `table_name` (unioned by column name across
    years, since form columns changed shape over time). Before that union
    load — which uses ignore_errors=true and drops malformed rows silently —
    scan each matching file on its own with store_rejects=true and print a
    per-file count of rows dropped, since union_by_name is not supported
    together with store_rejects in DuckDB."""
    con.execute("DROP TABLE IF EXISTS _t3010_reject_errors")
    con.execute("DROP TABLE IF EXISTS _t3010_reject_scans")
    files = sorted(glob.glob(glob_pattern))
    for f in files:
        con.execute(
            f"SELECT * FROM read_csv('{f}', all_varchar=true, ignore_errors=true, "
            f"store_rejects=true, rejects_table='_t3010_reject_errors', rejects_scan='_t3010_reject_scans')"
        ).fetchall()
    rejects = con.execute(
        "SELECT s.file_path, count(*) FROM _t3010_reject_errors e "
        "JOIN _t3010_reject_scans s USING (scan_id) GROUP BY s.file_path ORDER BY s.file_path"
    ).fetchall()
    total = sum(n for _, n in rejects)
    if total:
        print(f"  {table_name}: {total:,} rows rejected by ignore_errors across {len(rejects)} file(s):")
        for file_path, n in rejects:
            print(f"    {os.path.basename(file_path)}: {n:,} rejected")
    else:
        print(f"  {table_name}: 0 rows rejected across {len(files)} file(s)")

    con.execute(
        f"CREATE OR REPLACE TABLE {table_name} AS "
        f"SELECT *, CAST(regexp_extract(filename, '(\\d{{4}})\\.csv$', 1) AS INTEGER) AS source_year "
        f"FROM read_csv('{glob_pattern}', all_varchar=true, union_by_name=true, filename=true, ignore_errors=true)"
    )


def load_raw(con):
    print("Loading raw sources into DuckDB ...")
    con.execute(f"CREATE OR REPLACE TABLE raw_grants AS SELECT * FROM read_csv('{GRANTS_CSV}', all_varchar=true)")
    n = con.execute("SELECT COUNT(*) FROM raw_grants").fetchone()[0]
    print(f"  raw_grants: {n:,} rows")

    # Amendment rows restate an agreement's value rather than adding to it --
    # dedupe to the latest amendment per (owner_org, ref_number) before
    # anything reads grants values, so grants_unified never double/triple-
    # counts dollars. ref_number alone is NOT a safe key: refs collide across
    # departments (see _latest_amendment_sql). raw_grants itself stays
    # untouched (full amendment history queryable).
    con.execute(f"CREATE OR REPLACE TABLE raw_grants_latest AS {_latest_amendment_sql('raw_grants')}")
    n_latest = con.execute("SELECT COUNT(*) FROM raw_grants_latest").fetchone()[0]
    print(f"  raw_grants_latest: {n_latest:,} rows (latest amendment per (dept, ref); "
          f"{n - n_latest:,} superseded amendment rows excluded from grants_unified)")

    # T3010 (2013-2024): one file per kind per year, unioned by column name
    # since a handful of columns were added/removed across form versions
    # (e.g. 5045/5840-5843 only exist from 2023 onward). `source_year` is
    # parsed from our own local filename, not from any in-file column.
    t3010_dir = os.path.join(DATA_DIR, "t3010")

    _load_t3010_table(con, f"{t3010_dir}/identification_*.csv", "raw_t3010_ident")
    n = con.execute("SELECT COUNT(*) FROM raw_t3010_ident").fetchone()[0]
    years = con.execute("SELECT COUNT(DISTINCT source_year) FROM raw_t3010_ident").fetchone()[0]
    print(f"  raw_t3010_ident: {n:,} rows across {years} years")

    _load_t3010_table(con, f"{t3010_dir}/qualified_donees_*.csv", "raw_t3010_qd")
    n = con.execute("SELECT COUNT(*) FROM raw_t3010_qd").fetchone()[0]
    print(f"  raw_t3010_qd: {n:,} rows")

    # raw_t3010_qd carries genuine full-row duplicate lines within the same
    # source file (see _dedup_t3010_qd_sql()) -- dedupe before anything reads
    # gift values, same non-destructive pattern as raw_grants_latest above.
    con.execute(f"CREATE OR REPLACE TABLE raw_t3010_qd_dedup AS {_dedup_t3010_qd_sql()}")
    n_dedup = con.execute("SELECT COUNT(*) FROM raw_t3010_qd_dedup").fetchone()[0]
    total_before = con.execute('SELECT SUM(TRY_CAST("Total Gifts" AS DOUBLE)) FROM raw_t3010_qd').fetchone()[0] or 0
    total_after = con.execute('SELECT SUM(TRY_CAST("Total Gifts" AS DOUBLE)) FROM raw_t3010_qd_dedup').fetchone()[0] or 0
    print(f"  raw_t3010_qd_dedup: {n_dedup:,} rows "
          f"({n - n_dedup:,} duplicate lines removed, "
          f"${total_before - total_after:,.0f} in duplicate reported gifts excluded)")
    top_affected = con.execute("""
        SELECT dup.BN, dup.n_dup_rows, dup.dup_dollars
        FROM (
            SELECT BN, COUNT(*) - COUNT(*) FILTER (WHERE keep) AS n_dup_rows,
                   SUM(TRY_CAST("Total Gifts" AS DOUBLE)) FILTER (WHERE NOT keep) AS dup_dollars
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY BN, FPE, "Form ID", "Donee BN", "Donee Name", Associated,
                                 City, Province, "Total Gifts", "Gifts in Kind",
                                 "Political Activity Gift", "Political Activity Amount",
                                 filename, source_year
                    ORDER BY TRY_CAST("#" AS INTEGER)
                ) = 1 AS keep
                FROM raw_t3010_qd
            )
            GROUP BY BN
        ) dup
        WHERE dup.n_dup_rows > 0
        ORDER BY dup.dup_dollars DESC
        LIMIT 20
    """).fetchall()
    print("  top 20 filers by duplicate-gift dollars removed:")
    for bn, n_dup, dollars in top_affected:
        print(f"    BN {bn}: {n_dup:,} duplicate rows, ${dollars or 0:,.0f}")

    _load_t3010_table(con, f"{t3010_dir}/non_qualified_donees_*.csv", "raw_t3010_nqd")
    n = con.execute("SELECT COUNT(*) FROM raw_t3010_nqd").fetchone()[0]
    print(f"  raw_t3010_nqd: {n:,} rows (only populated from 2023 onward)")

    _load_t3010_table(con, f"{t3010_dir}/financials_*.csv", "raw_t3010_fin")
    n = con.execute("SELECT COUNT(*) FROM raw_t3010_fin").fetchone()[0]
    print(f"  raw_t3010_fin: {n:,} rows")

    con.execute(
        f"CREATE OR REPLACE TABLE raw_cc AS "
        f"SELECT * FROM read_csv('{DATA_DIR}/canada_council_grants.csv', all_varchar=true)"
    )
    cols = [r[0] for r in con.execute("DESCRIBE raw_cc").fetchall()]
    new_names = [
        "cc_year", "cc_year_fr", "recipient_name", "alias", "recipient_type", "recipient_type_fr",
        "business_number", "amount", "amount_fr", "currency", "approval_date", "city", "province",
        "province_fr", "postal_code", "census_area", "census_area_fr", "federal_riding",
        "federal_riding_code", "type_of_support", "type_of_support_fr", "program", "program_fr",
        "component_code", "component", "component_fr", "type_of_funding", "type_of_funding_fr",
        "field_of_practice", "field_of_practice_fr", "last_modified", "data_source", "data_source_fr",
    ]
    assert len(cols) == len(new_names), f"Canada Council column count changed: {len(cols)} vs expected {len(new_names)}"
    rename_sql = ", ".join(f'"{c}" AS {n}' for c, n in zip(cols, new_names))
    con.execute(f"CREATE OR REPLACE TABLE cc AS SELECT {rename_sql} FROM raw_cc")
    n = con.execute("SELECT COUNT(*) FROM cc").fetchone()[0]
    print(f"  cc: {n:,} rows")

    # OTF's headers are bilingual with embedded colons (e.g. "Fiscal
    # Year:Année fiscal") -- read all_varchar and rename positionally, same
    # pattern as raw_cc above. sniff_csv handles this file with defaults (no
    # ignore_errors needed); if any row were rejected here, read_csv would
    # raise rather than silently drop it, unlike the T3010 ignore_errors=true
    # loads above -- see AGENTS.md open issue #1 for why that's deliberate.
    con.execute(
        f"CREATE OR REPLACE TABLE raw_otf AS "
        f"SELECT * FROM read_csv('{OTF_CSV}', all_varchar=true)"
    )
    cols = [r[0] for r in con.execute("DESCRIBE raw_otf").fetchall()]
    new_names = [
        "funding_org", "country_served", "province_served", "fiscal_year_raw", "program_name",
        "geo_area_served", "cross_catchment", "identifier", "org_name", "submission_date",
        "approval_date", "amount_applied_for", "amount_awarded", "planned_duration_months",
        "description_en", "description_fr", "program_area", "budget_fund", "incorporation_number",
        "charitable_registration_number", "recipient_city", "recipient_postal_code", "co_application",
        "population_served", "age_group", "grant_result", "rescinded_flag", "rescinded_initiated_by",
        "amount_rescinded", "grant_status", "statut_subvention", "last_modified",
    ]
    assert len(cols) == len(new_names), f"OTF column count changed: {len(cols)} vs expected {len(new_names)}"
    rename_sql = ", ".join(f'"{c}" AS {n}' for c, n in zip(cols, new_names))
    con.execute(f"CREATE OR REPLACE TABLE otf AS SELECT {rename_sql} FROM raw_otf")
    n = con.execute("SELECT COUNT(*) FROM otf").fetchone()[0]
    print(f"  otf: {n:,} rows")


def build_entities_and_grants(con):
    r = Resolver()
    parse_failures = defaultdict(int)  # "source.field" -> count of non-blank values to_float()/fiscal_year_from_date() couldn't parse

    print("\nSeeding entities from T3010 charity registry (2013-2024, latest year per BN wins) ...")
    latest_by_root = {}  # bn_root -> (source_year, legal_name, city, province)
    for bn, legal_name, city, province, source_year in con.execute(
        'SELECT BN, "Legal Name", City, Province, source_year FROM raw_t3010_ident'
    ).fetchall():
        root = normalize_bn(bn)
        if not root:
            r.note_bn_reject(bn)
            continue
        prev = latest_by_root.get(root)
        if prev is None or source_year > prev[0]:
            latest_by_root[root] = (source_year, legal_name, city, province)
    for root, (source_year, legal_name, city, province) in latest_by_root.items():
        r.add_charity(root, legal_name, city, province)
    print(f"  {len(r.bn_to_entity):,} charity entities seeded (including charities deregistered "
          f"before 2024 that only appear in earlier years)")

    # owner_org (51 distinct codes, each mapping to exactly one bilingual
    # owner_org_title in grants.csv, verified) replaces the ref_number prefix
    # this used to seed from (AGENTS.md issue #3/#4): that prefix is an
    # unrelated, undocumented numeric code -- not a department slug -- so it
    # produced 120 mostly-meaningless entities (e.g. "199", "014") instead of
    # one clean entity per real department, and DEPT_NAMES (a ~25-entry
    # hand-maintained lookup keyed on slugs like "pch" that essentially never
    # matched those numeric prefixes) is gone; owner_org_title supplies every
    # name directly. This also fixes the funder-attribution risk flagged in
    # issue #3: a grant's department is now keyed on its own owner_org column
    # instead of a ref_number prefix that can collide across departments.
    print("Seeding federal department entities from grants.csv owner_org codes ...")
    for (code, title) in con.execute(
        "SELECT DISTINCT TRIM(owner_org) AS code, ANY_VALUE(owner_org_title) AS title "
        "FROM raw_grants WHERE owner_org IS NOT NULL AND TRIM(owner_org) != '' GROUP BY 1"
    ).fetchall():
        if code:
            r.add_dept(code, title or code.upper())
    print(f"  {len(r.dept_to_entity):,} federal department entities seeded")

    cc_eid = r.add_funder_org("Canada Council for the Arts")

    grants_unified = []  # (source_dataset, funder_entity_id, recipient_entity_id, amount_cad,
                          #  fiscal_year, program_name, description, source_ref)

    # ── source 1: federal Grants & Contributions ────────────────────────────
    # source_ref = TRIM(owner_org) + "|" + TRIM(ref_number), the exact key
    # _latest_amendment_sql() dedupes raw_grants_latest on -- populated here,
    # when the source row is still at hand, so org_page.py's receipt drawer
    # can look it up directly instead of an ambiguous runtime join on
    # recipient name + amount + fiscal year (AGENTS.md issue #4). The other
    # four sources have no comparably unique per-row key in their raw schedules
    # (T3010 donee schedules) or explicitly-documented-non-unique one (OTF's
    # "Identifier") without inventing one, so their source_ref stays NULL.
    print("\nProcessing federal G&C records (grants.csv) ...")
    cols = [
        "owner_org", "ref_number", "recipient_legal_name", "recipient_business_number",
        "recipient_type", "recipient_province", "recipient_country", "agreement_value",
        "agreement_start_date", "prog_name_en", "description_en",
    ]
    cur = con.execute(f"SELECT {', '.join(cols)} FROM raw_grants_latest")
    processed = 0
    individual_skipped = 0
    while True:
        batch = cur.fetchmany(50_000)
        if not batch:
            break
        for (owner_org, ref, name, bn, rtype, province, country, value, start_date, prog, desc) in batch:
            processed += 1
            if processed % 200_000 == 0:
                print(f"  ... {processed:,} rows")
            if not ref or "-" not in ref:
                continue
            if rtype == "P" or (not rtype and looks_like_individual_name_comma(name)):
                # 'P' = Individual/Sole proprietor. A blank rtype with a
                # "Lastname, Firstname" shape is also an individual -- see
                # looks_like_individual_name_comma()'s docstring/comment for
                # why this isn't applied when rtype carries any other value.
                individual_skipped += 1
                continue
            funder_eid = r.dept_to_entity.get((owner_org or "").strip().lower())
            if funder_eid is None:
                continue
            allow_fuzzy = (rtype in GRANTS_NFP_TYPES) and (country or "").strip().upper() == "CA"
            recipient_eid = r.resolve("federal_gc", name, bn, province, allow_fuzzy)
            source_ref = f"{owner_org.strip()}|{ref.strip()}" if owner_org else None
            amount = to_float(value)
            if amount is None:
                _note_parse_failure(parse_failures, "federal_gc.agreement_value", value)
            fy = fiscal_year_from_date(start_date)
            if fy is None:
                _note_parse_failure(parse_failures, "federal_gc.agreement_start_date", start_date)
            grants_unified.append((
                "federal_gc", funder_eid, recipient_eid, amount, fy, prog, desc, source_ref,
            ))
    print(f"  {processed:,} federal G&C records processed "
          f"({individual_skipped:,} individual/sole-proprietor recipients skipped)")

    # ── source 2: Canada Council for the Arts ───────────────────────────────
    print("\nProcessing Canada Council grants ...")
    processed = 0
    individual_skipped = 0
    for (name, rtype, bn, amount_raw, year, province, program) in con.execute(
        "SELECT recipient_name, recipient_type, business_number, amount, cc_year, province, program FROM cc"
    ).fetchall():
        processed += 1
        # Recipient Type is one of Organization / Group / Individual (verified
        # against the real CSV); only Individual is a person -- Group covers
        # informal collectives (e.g. an artist collective) that aren't
        # individual grant recipients even though they're not incorporated.
        if rtype == "Individual":
            individual_skipped += 1
            continue
        allow_fuzzy = rtype == "Organization"
        recipient_eid = r.resolve("canada_council", name, bn, province, allow_fuzzy)
        amount = to_float(amount_raw)
        if amount is None:
            _note_parse_failure(parse_failures, "canada_council.amount", amount_raw)
        fy = None
        if year and year[:4].isdigit():
            fy = int(year[:4])
        else:
            _note_parse_failure(parse_failures, "canada_council.cc_year", year)
        grants_unified.append((
            "canada_council", cc_eid, recipient_eid, amount, fy, program, None, None,
        ))
    print(f"  {processed:,} Canada Council records processed "
          f"({individual_skipped:,} individual recipients skipped)")

    # ── source 3: T3010 qualified donees (charity -> charity/qualified-donee gifts) ──
    # Reads raw_t3010_qd_dedup, not raw_t3010_qd directly -- the raw source
    # carries genuine full-row duplicate lines within the same file (see
    # _dedup_t3010_qd_sql()); reading the undeduped table here would
    # double-count real dollars (confirmed: CanadaHelps -> Canadian Red
    # Cross Society $3,224,402 appeared as two grants_unified rows before
    # this fix).
    print("\nProcessing T3010 qualified donee gifts ...")
    processed = 0
    for (bn, fpe, donee_bn, donee_name, city, province, total_gifts) in con.execute(
        'SELECT BN, FPE, "Donee BN", "Donee Name", City, Province, "Total Gifts" FROM raw_t3010_qd_dedup'
    ).fetchall():
        processed += 1
        funder_root = normalize_bn(bn)
        if funder_root is None:
            r.note_bn_reject(bn)
        funder_eid = r.bn_to_entity.get(funder_root)
        if funder_eid is None:
            continue  # filer not found in identification extract (shouldn't happen)
        recipient_eid = r.resolve("t3010_qualified_donee", donee_name, donee_bn, province, allow_fuzzy=True)
        amount = to_float(total_gifts)
        if amount is None:
            _note_parse_failure(parse_failures, "t3010_qualified_donee.total_gifts", total_gifts)
        fy = fiscal_year_from_date(fpe, month_cutover=1)
        if fy is None:
            _note_parse_failure(parse_failures, "t3010_qualified_donee.fpe", fpe)
        grants_unified.append((
            "t3010_qualified_donee", funder_eid, recipient_eid, amount,
            fy, "Qualified donee gift", None, None,
        ))
    print(f"  {processed:,} qualified donee records processed")

    # ── source 4: T3010 grants to non-qualified donees ──────────────────────
    print("\nProcessing T3010 non-qualified donee grants ...")
    processed = 0
    individual_skipped = 0
    for (bn, fpe, recipient_name, cash, noncash) in con.execute(
        'SELECT BN, FPE, "Recipient Name", "Cash amount", "Non-cash amount" FROM raw_t3010_nqd'
    ).fetchall():
        processed += 1
        funder_root = normalize_bn(bn)
        if funder_root is None:
            r.note_bn_reject(bn)
        funder_eid = r.bn_to_entity.get(funder_root)
        if funder_eid is None:
            continue
        if looks_like_individual_donee_name(recipient_name):
            individual_skipped += 1
            continue
        recipient_eid = r.resolve("t3010_non_qualified_donee", recipient_name, None, None, allow_fuzzy=True)
        cash_amt = to_float(cash)
        if cash_amt is None:
            _note_parse_failure(parse_failures, "t3010_non_qualified_donee.cash_amount", cash)
        noncash_amt = to_float(noncash)
        if noncash_amt is None:
            _note_parse_failure(parse_failures, "t3010_non_qualified_donee.noncash_amount", noncash)
        amount = (cash_amt or 0) + (noncash_amt or 0)
        fy = fiscal_year_from_date(fpe, month_cutover=1)
        if fy is None:
            _note_parse_failure(parse_failures, "t3010_non_qualified_donee.fpe", fpe)
        grants_unified.append((
            "t3010_non_qualified_donee", funder_eid, recipient_eid, amount,
            fy, "Non-qualified donee grant", None, None,
        ))
    print(f"  {processed:,} non-qualified donee records processed "
          f"({individual_skipped:,} individual-shaped recipient names skipped)")

    # ── source 5: Ontario Trillium Foundation grants ────────────────────────
    # Funding Org is the same single value on all 32,842 rows ("Ontario
    # Trillium Foundation") -- resolve it once rather than per row. It must
    # land on the entity already seeded by an earlier source (verified:
    # entity 253071, bn_root 108091091, an `other_org` residual -- OTF itself
    # isn't a CRA-registered charity, so it was never seeded from T3010) via
    # exact_bn, not create a new entity; assert that here rather than only in
    # verification, so a regression fails loudly at build time.
    print("\nProcessing OTF grants ...")
    otf_funder_eid = r.resolve("otf", "Ontario Trillium Foundation", "108091091", "ON", allow_fuzzy=False)
    assert r.links[-1].match_method == "exact_bn", (
        f"OTF funder should exact-BN-match the existing Ontario Trillium Foundation "
        f"entity, got match_method={r.links[-1].match_method!r}"
    )

    processed = 0
    crn_discarded = 0
    rescinded_flag_null_amount = 0
    floored_negative = 0
    for (org_name, crn_raw, awarded_raw, rescinded_raw, rescinded_flag, program, desc, fy_raw) in con.execute(
        "SELECT org_name, charitable_registration_number, amount_awarded, amount_rescinded, "
        "rescinded_flag, program_name, description_en, fiscal_year_raw FROM otf"
    ).fetchall():
        processed += 1
        awarded_parsed = to_float(awarded_raw)
        if awarded_parsed is None:
            _note_parse_failure(parse_failures, "otf.amount_awarded", awarded_raw)
        awarded = awarded_parsed or 0.0
        rescinded = to_float(rescinded_raw)
        if rescinded_flag == "Yes" and rescinded is None:
            # "Recovered, amount unrecorded" -- a known unknown (see
            # docs/otf-ingestion-spec.md and entity-resolution-methodology.md),
            # treated as 0 rescinded rather than dropping the row.
            rescinded_flag_null_amount += 1
        net_amount, was_floored = otf_net_amount(awarded, rescinded)
        if was_floored:
            floored_negative += 1
            print(f"    WARNING: rescinded > awarded for {org_name!r} "
                  f"(awarded={awarded}, rescinded={rescinded}); floored to 0")

        bn_root = validate_otf_crn(crn_raw)
        if crn_raw and str(crn_raw).strip() and bn_root is None:
            crn_discarded += 1

        # allow_fuzzy=True unconditionally: unlike federal_gc/canada_council,
        # OTF has no recipient-type field to gate on, and the ingestion spec
        # expects the ~40% that don't carry a valid CRN (municipalities,
        # school boards, First Nations) to mostly land as unmatched_new
        # other_org rows via this same fuzzy attempt -- that's correct
        # behavior, not a failure to fix.
        recipient_eid = r.resolve("otf", org_name, bn_root, "ON", allow_fuzzy=True)
        fy = otf_fiscal_year(fy_raw)
        if fy is None:
            _note_parse_failure(parse_failures, "otf.fiscal_year_raw", fy_raw)
        grants_unified.append((
            "otf", otf_funder_eid, recipient_eid, net_amount, fy, program, desc, None,
        ))
    print(f"  {processed:,} OTF records processed")
    print(f"  {crn_discarded:,} charitable registration numbers discarded "
          f"(present but not matching ^\\d{{9}}(RR\\d{{4}})?$)")
    print(f"  {rescinded_flag_null_amount:,} rows with rescinded flag = Yes but "
          f"amount_rescinded NULL (treated as 0 -- recovered, amount unrecorded)")
    print(f"  {floored_negative:,} rows had rescinded > awarded, floored to 0")

    if parse_failures:
        print("\nAmount/date parse failures (non-blank field, unparseable -- became NULL in grants_unified):")
        for key, n in sorted(parse_failures.items(), key=lambda kv: -kv[1]):
            print(f"  {key}: {n:,}")
    else:
        print("\nAmount/date parse failures: 0 across all sources")

    if r.bn_reject_counts:
        print("\nBN parse-reject counts by pattern (non-blank raw BN that didn't normalize to a 9-digit root):")
        for key, n in sorted(r.bn_reject_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {key}: {n:,}")
    else:
        print("\nBN parse-reject counts: 0 across all sources")

    print(f"\nEntity resolution summary: {dict(r.stats)}")
    print(f"Total entities: {len(r.entities):,}")

    con.execute("DROP TABLE IF EXISTS entities")
    con.execute("""
        CREATE TABLE entities (
            entity_id INTEGER, bn_root VARCHAR, canonical_name VARCHAR,
            city VARCHAR, province VARCHAR, entity_kind VARCHAR
        )
    """)
    con.executemany("INSERT INTO entities VALUES (?,?,?,?,?,?)", r.entities)

    # bn_full: the complete 15-char BN (with RR/RC/etc. program-account
    # suffix), not just the 9-digit bn_root -- needed to deep-link to the
    # CRA's own charity listing (which is keyed on the full BN, not the
    # root). Sourced from raw_t3010_ident.BN, the latest source_year row per
    # bn_root, same "most recent filing wins" convention build_entity_financials
    # already uses for its own bn_full column (on entity_financials, not
    # entities -- that one only covers entities with a financials filing;
    # this covers every entity with any identification-schedule row).
    con.execute("ALTER TABLE entities ADD COLUMN bn_full VARCHAR")
    con.execute(_bn_full_update_sql())
    n_bn_full = con.execute("SELECT COUNT(*) FROM entities WHERE bn_full IS NOT NULL").fetchone()[0]
    print(f"  entities.bn_full populated for {n_bn_full:,} entities")

    # search_name: canonical_name (already HTML-clean as of the fix above) ->
    # lowercased, accent-stripped, whitespace-collapsed -- lets live search
    # match "ecole" against "École" and vice versa. Uses DuckDB's native
    # strip_accents() rather than a second Python-side normalizer, so the
    # query-time folding (webapp.py's _fold_query()) and this column use the
    # exact same folding logic with nothing to keep in sync.
    con.execute("ALTER TABLE entities ADD COLUMN search_name VARCHAR")
    con.execute(_search_name_update_sql())

    con.execute("DROP TABLE IF EXISTS entity_links")
    con.execute("""
        CREATE TABLE entity_links (
            entity_id INTEGER, source_dataset VARCHAR, raw_name VARCHAR,
            raw_bn VARCHAR, match_method VARCHAR, match_score DOUBLE
        )
    """)
    con.executemany("INSERT INTO entity_links VALUES (?,?,?,?,?,?)", r.links)

    # QA-only: candidates the digit-token gate split apart despite scoring
    # >= FUZZY_ACCEPT pre-gate. Not part of the documented schema (underscore
    # prefix, like _t3010_reject_errors) — sampled in print_report to check
    # whether the gate is only catching true branch-number splits or also
    # separating genuine same-org near-misses.
    con.execute("DROP TABLE IF EXISTS _fuzzy_gate_rejects")
    con.execute("""
        CREATE TABLE _fuzzy_gate_rejects (
            raw_name VARCHAR, rejected_canonical_name VARCHAR,
            score DOUBLE, source_dataset VARCHAR
        )
    """)
    con.executemany("INSERT INTO _fuzzy_gate_rejects VALUES (?,?,?,?)", r.gate_rejects)
    print(f"_fuzzy_gate_rejects: {len(r.gate_rejects):,} rows")

    con.execute("DROP TABLE IF EXISTS grants_unified")
    con.execute("""
        CREATE TABLE grants_unified (
            grant_id INTEGER, source_dataset VARCHAR, funder_entity_id INTEGER,
            recipient_entity_id INTEGER, amount_cad DOUBLE, fiscal_year INTEGER,
            program_name VARCHAR, description VARCHAR, source_ref VARCHAR
        )
    """)
    con.executemany(
        "INSERT INTO grants_unified VALUES (?,?,?,?,?,?,?,?,?)",
        [(i + 1,) + row for i, row in enumerate(grants_unified)],
    )
    print(f"grants_unified: {len(grants_unified):,} rows")


def build_role_summary(con):
    print("\nBuilding entity_role_summary ...")
    con.execute("""
        CREATE OR REPLACE TABLE entity_role_summary AS
        WITH given AS (
            SELECT funder_entity_id AS entity_id, SUM(amount_cad) AS total_given, COUNT(*) AS n_given
            FROM grants_unified WHERE funder_entity_id IS NOT NULL GROUP BY 1
        ), received AS (
            SELECT recipient_entity_id AS entity_id, SUM(amount_cad) AS total_received, COUNT(*) AS n_received
            FROM grants_unified WHERE recipient_entity_id IS NOT NULL GROUP BY 1
        )
        SELECT
            e.entity_id, e.canonical_name, e.entity_kind,
            COALESCE(g.total_given, 0) AS total_given,
            COALESCE(r.total_received, 0) AS total_received,
            COALESCE(g.n_given, 0) AS n_grants_given,
            COALESCE(r.n_received, 0) AS n_grants_received,
            CASE WHEN COALESCE(g.total_given,0) + COALESCE(r.total_received,0) = 0 THEN NULL
                 ELSE COALESCE(g.total_given,0) / (COALESCE(g.total_given,0) + COALESCE(r.total_received,0))
            END AS given_share,
            CASE
                WHEN COALESCE(g.total_given,0) + COALESCE(r.total_received,0) = 0 THEN 'no_flows'
                WHEN COALESCE(g.total_given,0) / (COALESCE(g.total_given,0) + COALESCE(r.total_received,0)) >= 0.9 THEN 'primarily_funder'
                WHEN COALESCE(g.total_given,0) / (COALESCE(g.total_given,0) + COALESCE(r.total_received,0)) <= 0.1 THEN 'primarily_recipient'
                ELSE 'dual_role'
            END AS role
        FROM entities e
        LEFT JOIN given g ON g.entity_id = e.entity_id
        LEFT JOIN received r ON r.entity_id = e.entity_id
    """)
    n = con.execute("SELECT COUNT(*) FROM entity_role_summary").fetchone()[0]
    print(f"  entity_role_summary: {n:,} rows")


def build_entity_financials(con):
    # raw_t3010_fin spans 2013-2024 (one row per BN per filing year). We keep
    # only the latest source_year per bn_root before joining to entities, so
    # entity_financials stays one row per entity instead of up to 12.
    print("\nBuilding entity_financials (T3010 line codes 4700/4950/5100/5050/4540/4570, latest fiscal year per entity) ...")
    con.execute("""
        CREATE OR REPLACE TABLE entity_financials AS
        WITH fin_with_root AS (
            SELECT *, substr(regexp_replace(BN, '[^0-9A-Za-z]', ''), 1, 9) AS bn_root
            FROM raw_t3010_fin
        ),
        latest_fin AS (
            SELECT *
            FROM fin_with_root
            QUALIFY ROW_NUMBER() OVER (PARTITION BY bn_root ORDER BY source_year DESC) = 1
        )
        SELECT
            e.entity_id,
            f.BN AS bn_full,
            TRY_CAST(f.FPE AS DATE) AS fiscal_period_end,
            TRY_CAST(f."4700" AS DOUBLE) AS total_revenue,
            TRY_CAST(f."4950" AS DOUBLE) AS total_expenditures,
            TRY_CAST(f."5100" AS DOUBLE) AS total_expenditures_incl_disbursements,
            TRY_CAST(f."5050" AS DOUBLE) AS total_gifts_to_qualified_donees,
            TRY_CAST(f."4540" AS DOUBLE) AS revenue_from_federal_gov,
            TRY_CAST(f."4570" AS DOUBLE) AS revenue_from_any_cdn_gov
        FROM latest_fin f
        JOIN entities e ON e.bn_root = f.bn_root
    """)
    n = con.execute("SELECT COUNT(*) FROM entity_financials").fetchone()[0]
    print(f"  entity_financials: {n:,} rows")


def build_entity_financials_by_year(con):
    # Same source as entity_financials, but keeps every fiscal year instead
    # of collapsing to the latest -- needed for the org-page funding timeline
    # to compare declared T3010 revenue against identified grants_unified
    # money on a year-by-year basis. Deduped per (bn_root, fiscal_year) --
    # not per (bn_root, source_year), since a late-filed or refiled return
    # could put two source_year rows on the same FPE-derived fiscal year.
    # EXTRACT(YEAR FROM FPE) is used instead of fiscal_year_from_date(...,
    # month_cutover=1) (the convention t3010_qualified_donee/
    # t3010_non_qualified_donee already use for their own FPE-derived fiscal
    # year): with month_cutover=1, that function's `d.month >= month_cutover`
    # branch is always true, so it's provably just d.year -- EXTRACT(YEAR...)
    # is the same computation without a Python round-trip, and it's what
    # entity_financials already uses to parse this same FPE column.
    print("\nBuilding entity_financials_by_year (T3010 line codes 4700/4570/4510, every fiscal year per entity) ...")
    con.execute("""
        CREATE OR REPLACE TABLE entity_financials_by_year AS
        WITH fin_with_root AS (
            SELECT *, substr(regexp_replace(BN, '[^0-9A-Za-z]', ''), 1, 9) AS bn_root
            FROM raw_t3010_fin
        ),
        deduped AS (
            SELECT *
            FROM fin_with_root
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY bn_root, EXTRACT(YEAR FROM TRY_CAST(FPE AS DATE))
                ORDER BY source_year DESC
            ) = 1
        )
        SELECT
            e.entity_id,
            TRY_CAST(f.FPE AS DATE) AS fiscal_period_end,
            EXTRACT(YEAR FROM TRY_CAST(f.FPE AS DATE))::INTEGER AS fiscal_year,
            TRY_CAST(f."4700" AS DOUBLE) AS total_revenue,
            TRY_CAST(f."4570" AS DOUBLE) AS gov_revenue,
            TRY_CAST(f."4510" AS DOUBLE) AS foundation_revenue
        FROM deduped f
        JOIN entities e ON e.bn_root = f.bn_root
    """)
    n = con.execute("SELECT COUNT(*) FROM entity_financials_by_year").fetchone()[0]
    print(f"  entity_financials_by_year: {n:,} rows")


# ── BN near-miss review & confirmed-merge mechanism ─────────────────────────
# A raw BN can be corrupted at the source (a transcription typo, or a
# leading zero gained/lost when some upstream system treated a BN as a
# number) -- normalize_bn() parses BNs, it never repairs or guesses at them
# (see its docstring), so a corrupted BN mints its own separate entity
# rather than silently merging into the real one. build_bn_near_miss_review()
# surfaces candidate pairs for a human to eyeball -- it never auto-merges, a
# wrong BN merge silently combines two different real organizations' money,
# which is worse than leaving them split. _apply_entity_merges()/
# apply_bn_merge_overrides() are the separate, human-gated mechanism that
# actually performs a confirmed merge, reading data/bn_merge_overrides.csv
# (also reused by build_branch_variant_review()'s confirmed merges).

def _bn_substitution_candidates(root):
    """Every 9-digit string within one digit substitution of `root` -- the
    plain-typo corruption shape (e.g. bn_root 119219814 vs 119218814, one
    digit off)."""
    for i in range(len(root)):
        for d in "0123456789":
            if d != root[i]:
                yield root[:i] + d + root[i + 1:]


def _bn_shift_candidates(root):
    """The two 9-digit strings produced by inserting a single digit at the
    front (losing the true trailing digit to truncation) or dropping the
    leading digit (gaining a placeholder trailing digit) -- models a BN
    gaining/losing a leading zero when treated as a number somewhere
    upstream. Confirmed real: Canadian Red Cross bn_root 119219814 filed
    elsewhere as 011921981 ('0' + 119219814[:8]). Deliberately NOT a general
    insertion/deletion at any position -- that would also flag unrelated
    near-duplicate BNs from two different real organizations far too often;
    restricted to the boundary-shift shape actually observed."""
    for d in "0123456789":
        yield d + root[:8]
        yield root[1:] + d


def _best_name_variant_score(name_a, name_b):
    """Best rapidfuzz token_sort_ratio across every combination of
    name_variants() (the bilingual "Name A/Nom B" splitter add_charity()
    already uses to index fuzzy candidates) of both names, each run through
    normalize_name(). Scoring the two raw names directly would wrongly
    reject a real near-miss pair whenever one side's canonical_name carries
    a bilingual suffix the other lacks -- confirmed real case verified
    against the live DB: the actual Canadian Red Cross charity's
    canonical_name is "THE CANADIAN RED CROSS SOCIETY / LA SOCIETE
    CANADIENNE DE LA CROIX-ROUGE"; a corrupted-BN duplicate named simply
    "THE CANADIAN RED CROSS SOCIETY" scores only ~61 against the full
    bilingual string as one string, but 100 against its English-only
    variant."""
    variants_a = [normalize_name(v) for v in name_variants(name_a or "")]
    variants_b = [normalize_name(v) for v in name_variants(name_b or "")]
    best = 0.0
    for va in variants_a:
        for vb in variants_b:
            if not va or not vb:
                continue
            score = fuzz.token_sort_ratio(va, vb)
            if score > best:
                best = score
    return best


def build_bn_near_miss_review(con, out_path=None):
    """Writes analysis/output/bn_near_miss_review.csv: pairs of entities
    whose bn_roots are one substitution or boundary-shift apart AND whose
    names score >= FUZZY_ACCEPT under _best_name_variant_score() -- almost
    certainly the same organization under a source BN typo, but never
    auto-merged (see module note above). Must run after entities/
    entity_role_summary exist. Returns the number of pairs written."""
    out_path = out_path or BN_NEAR_MISS_REVIEW_PATH
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    rows = con.execute("""
        SELECT e.entity_id, e.bn_root, e.canonical_name, e.province,
               COALESCE(rs.total_given, 0) + COALESCE(rs.total_received, 0) AS total_flow
        FROM entities e
        LEFT JOIN entity_role_summary rs USING (entity_id)
        WHERE e.bn_root IS NOT NULL
    """).fetchall()
    by_root = {row[1]: row for row in rows}  # bn_root -> row (1:1: bn_root is unique on entities)

    seen_pairs = set()
    near_misses = []
    for root, row in by_root.items():
        cand_types = {}
        for cand in _bn_substitution_candidates(root):
            cand_types.setdefault(cand, "substitution")
        for cand in _bn_shift_candidates(root):
            cand_types.setdefault(cand, "insertion_deletion")
        for cand, edit_type in cand_types.items():
            if cand == root or cand not in by_root:
                continue
            other = by_root[cand]
            pair_key = tuple(sorted((row[0], other[0])))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            name_score = _best_name_variant_score(row[2], other[2])
            if name_score < FUZZY_ACCEPT:
                continue
            a, b = (row, other) if row[0] < other[0] else (other, row)
            near_misses.append((
                a[0], b[0], a[2], b[2], a[1], b[1], a[3], b[3], a[4], b[4],
                edit_type, round(name_score, 1),
            ))

    near_misses.sort(key=lambda rec: -(rec[8] + rec[9]))  # biggest combined dollar impact first

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "entity_id_a", "entity_id_b", "name_a", "name_b", "bn_root_a", "bn_root_b",
            "province_a", "province_b", "total_flow_a", "total_flow_b",
            "edit_type", "name_similarity_score",
        ])
        writer.writerows(near_misses)

    return len(near_misses)


def _apply_entity_merges(con, pairs, label=None):
    """Applies confirmed entity merges: pairs of (keep_entity_id,
    merge_entity_id). Remaps grants_unified's funder/recipient entity_id and
    entity_links' entity_id from merge_entity_id -> keep_entity_id, then
    deletes the merged-away row from entities. Must run after entities/
    entity_links/grants_unified are all populated and before
    build_role_summary()/build_entity_financials*() so downstream aggregates
    reflect the merged state without a second full rebuild. Non-destructive
    of the underlying money: every grant/link row stays, just re-pointed at
    the surviving entity_id; only the now-redundant entities row is removed.

    label: if given, prints a merge-count + top-20-by-dollar-impact report
    before applying (the dollar lookup reads grants_unified while the
    merged-away entity_id is still live there, so it must run pre-merge --
    same reporting convention as _dedup_t3010_qd_sql()'s "top 20 filers by
    duplicate-gift dollars removed"). Callers exercised only via unit tests
    (fixture DBs, no build report expected) pass no label and get silent
    behavior, same as before this parameter existed.

    Returns the number of merges applied."""
    pairs = list(pairs)
    if not pairs:
        if label:
            print(f"  {label}: 0 merges")
        return 0
    if label:
        impacts = []
        for keep_id, merge_id in pairs:
            flow = con.execute(
                "SELECT COALESCE(SUM(amount_cad), 0) FROM grants_unified "
                "WHERE funder_entity_id = ? OR recipient_entity_id = ?",
                [merge_id, merge_id],
            ).fetchone()[0]
            name_row = con.execute("SELECT canonical_name FROM entities WHERE entity_id = ?", [merge_id]).fetchone()
            impacts.append((merge_id, keep_id, name_row[0] if name_row else None, flow or 0))
        impacts.sort(key=lambda rec: -rec[3])
        print(f"  {label}: {len(pairs):,} merges applied, top 20 by dollar amount moved:")
        for merge_id, keep_id, name, flow in impacts[:20]:
            print(f"    entity {merge_id} ({name}) -> entity {keep_id}: ${flow:,.0f}")

    con.executemany("UPDATE grants_unified SET funder_entity_id = ? WHERE funder_entity_id = ?", pairs)
    con.executemany("UPDATE grants_unified SET recipient_entity_id = ? WHERE recipient_entity_id = ?", pairs)
    con.executemany("UPDATE entity_links SET entity_id = ? WHERE entity_id = ?", pairs)
    con.executemany("DELETE FROM entities WHERE entity_id = ?", [(merge_id,) for _, merge_id in pairs])
    return len(pairs)


def apply_bn_merge_overrides(con, overrides_path=None):
    """Applies human-confirmed entity merges from data/bn_merge_overrides.csv
    (columns: entity_id_keep, entity_id_merge, note) -- the human-reviewed
    fix path for a near-miss BN pair (build_bn_near_miss_review()) or a
    branch-suffix name variant (build_branch_variant_review()) that a human
    has actually eyeballed and confirmed is the same organization. A no-op
    (returns 0) if the file doesn't exist -- this override file is optional,
    and most rebuilds won't have any confirmed entries yet. entity_ids are
    only stable within a given build (Resolver assigns them in processing
    order), so overrides must be re-verified against a fresh rebuild's
    entity_ids before being trusted, not assumed to carry over."""
    overrides_path = overrides_path or BN_MERGE_OVERRIDES_PATH
    if not os.path.exists(overrides_path):
        return 0
    pairs = []
    with open(overrides_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            keep = (row.get("entity_id_keep") or "").strip()
            merge = (row.get("entity_id_merge") or "").strip()
            if not keep or not merge:
                continue
            pairs.append((int(keep), int(merge)))
    return _apply_entity_merges(con, pairs, label="Confirmed BN-merge overrides (data/bn_merge_overrides.csv)")


# ── NULL-province residual merge ────────────────────────────────────────────

NULL_PROVINCE_MERGE_MIN_TOKENS = 3
NULL_PROVINCE_MERGE_MIN_NAME_LENGTH = 15


def build_null_province_residual_merges(con):
    """Bridges a residual-dedup gap: Resolver.resolve()'s residual branch
    dedupes on (normalize_name(name), province), so two records of the same
    real organization -- one with a real province on file, one with a blank
    one -- mint two separate other_org entities even when their names fold
    identical (confirmed real: "Fédération acadienne de la Nouvelle-Écosse",
    one entity bn_root 132162041/province NS, the other neither -- both had
    byte-identical clean_html()+normalize_name() keys post-rebuild; AGENTS.md
    open issue #6). Not fixable inside Resolver.resolve() itself, since the
    two records can arrive in either order and there's no second pass there
    -- this is a Python post-process over the already-built entities table.

    Merges a fully-blank (bn_root IS NULL AND province IS NULL) other_org
    entity into its normalize_name()-identical other_org twin, ONLY when:
      - that twin carries a bn_root or a province, and there is exactly one
        such twin (an ambiguous match against multiple distinct twins is
        left alone, not guessed at);
      - the name isn't generic-looking (fewer than
        NULL_PROVINCE_MERGE_MIN_TOKENS normalize_name() tokens AND shorter
        than NULL_PROVINCE_MERGE_MIN_NAME_LENGTH chars), to avoid merging
        two unrelated small orgs that just happen to share a short name.
    Never merges two BN-bearing entities (that's build_bn_near_miss_review()'s
    territory) and never merges into a charity entity (a charity's identity
    is already BN-anchored during resolve() itself; a residual other_org
    coincidentally sharing its exact name -- e.g. a non-NFP recipient type
    that was never even fuzzy-matched against charities -- should not
    silently absorb into it here).

    Must run after entities/entity_links/grants_unified are populated and
    before build_role_summary()/build_entity_financials*(). Returns the
    number of merges applied."""
    blank_rows = con.execute("""
        SELECT entity_id, canonical_name FROM entities
        WHERE entity_kind = 'other_org' AND bn_root IS NULL AND province IS NULL
    """).fetchall()
    twin_rows = con.execute("""
        SELECT entity_id, canonical_name FROM entities
        WHERE entity_kind = 'other_org' AND (bn_root IS NOT NULL OR province IS NOT NULL)
    """).fetchall()

    twins_by_norm = defaultdict(list)
    for eid, name in twin_rows:
        twins_by_norm[normalize_name(name)].append(eid)

    pairs = []
    for eid, name in blank_rows:
        norm = normalize_name(name)
        if not norm:
            continue
        tokens = norm.split()
        if len(tokens) < NULL_PROVINCE_MERGE_MIN_TOKENS and len(name or "") < NULL_PROVINCE_MERGE_MIN_NAME_LENGTH:
            continue
        candidates = twins_by_norm.get(norm, [])
        if len(candidates) != 1:
            continue  # no twin, or ambiguous (multiple BN/province-bearing entities share this name)
        pairs.append((candidates[0], eid))

    return _apply_entity_merges(con, pairs, label="NULL-province residual merges")


# ── branch-suffix name variant review queue ─────────────────────────────────

BRANCH_SUFFIX_PATTERNS = [
    re.compile(r"^(.+)\s+[–-]\s+(.+)$"),      # "Name – Place" / "Name - Place" (space-delimited only,
                                               # so a compound word like "Meals-on-Wheels" isn't split)
    re.compile(r"^(.+)\s+\(([^()]+)\)\s*$"),  # "Name (Place)"
]


def build_branch_variant_review(con, out_path=None):
    """Writes analysis/output/branch_variant_review.csv: BN-less other_org
    entities whose name has a trailing " – <place>" / " - <place>" /
    "(<place>)" shape, where the base (everything before that suffix) is
    normalize_name()-identical to a BN-bearing entity's name (e.g. "Canadian
    Red Cross – Ottawa" against "THE CANADIAN RED CROSS SOCIETY"). This may
    be a genuinely separate regional entry, or the same org's local office --
    not distinguishable from name shape alone, so this is a review queue
    only; never an automatic merge. Confirmed merges route through
    data/bn_merge_overrides.csv / apply_bn_merge_overrides(), the same
    mechanism build_bn_near_miss_review() uses. Must run after entities/
    entity_role_summary exist. Returns the number of candidates written."""
    out_path = out_path or BRANCH_VARIANT_REVIEW_PATH
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    bn_rows = con.execute("""
        SELECT e.entity_id, e.canonical_name, e.bn_root, e.province,
               COALESCE(rs.total_given, 0) + COALESCE(rs.total_received, 0) AS total_flow
        FROM entities e
        LEFT JOIN entity_role_summary rs USING (entity_id)
        WHERE e.bn_root IS NOT NULL
    """).fetchall()
    bn_by_norm = defaultdict(list)
    for eid, name, bn_root, province, flow in bn_rows:
        bn_by_norm[normalize_name(name)].append((eid, name, bn_root, province, flow))

    bnless_rows = con.execute("""
        SELECT e.entity_id, e.canonical_name,
               COALESCE(rs.total_given, 0) + COALESCE(rs.total_received, 0) AS total_flow
        FROM entities e
        LEFT JOIN entity_role_summary rs USING (entity_id)
        WHERE e.bn_root IS NULL AND e.entity_kind = 'other_org'
    """).fetchall()

    out_rows = []
    for eid, name, flow in bnless_rows:
        for pattern in BRANCH_SUFFIX_PATTERNS:
            m = pattern.match((name or "").strip())
            if not m:
                continue
            base, place = m.group(1).strip(), m.group(2).strip()
            twins = bn_by_norm.get(normalize_name(base))
            if not twins:
                continue
            for bn_eid, bn_name, bn_root, province, bn_flow in twins:
                out_rows.append((eid, name, place, bn_eid, bn_name, bn_root, province, bn_flow, flow))
            break  # first matching suffix pattern wins -- don't double-count a name against both patterns

    out_rows.sort(key=lambda rec: -(rec[7] + rec[8]))  # biggest combined dollar impact first

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "bnless_entity_id", "bnless_name", "branch_suffix", "bn_entity_id", "bn_entity_name",
            "bn_root", "province", "bn_entity_flow", "bnless_entity_flow",
        ])
        writer.writerows(out_rows)

    return len(out_rows)


def print_report(con):
    print(f"\n{'='*60}\nENTITY GRAPH — SUMMARY REPORT\n{'='*60}")

    print("\n── match method breakdown by source ─────────────────────")
    for src, method, cnt in con.execute("""
        SELECT source_dataset, match_method, COUNT(*)
        FROM entity_links GROUP BY 1,2 ORDER BY 1, 3 DESC
    """).fetchall():
        print(f"  {src:<28} {method:<15} {cnt:>10,}")

    print("\n── entity_kind counts ────────────────────────────────────")
    for kind, cnt in con.execute("SELECT entity_kind, COUNT(*) FROM entities GROUP BY 1 ORDER BY 2 DESC").fetchall():
        print(f"  {kind:<15} {cnt:>10,}")

    print("\n── role classification (entities with any flow) ─────────")
    for role, cnt in con.execute("""
        SELECT role, COUNT(*) FROM entity_role_summary WHERE role != 'no_flows' GROUP BY 1 ORDER BY 2 DESC
    """).fetchall():
        print(f"  {role:<20} {cnt:>10,}")

    print("\n── sample dual_role entities (top 10 by total flow) ──────")
    for name, kind, given, received, share in con.execute("""
        SELECT canonical_name, entity_kind, total_given, total_received, given_share
        FROM entity_role_summary WHERE role = 'dual_role'
        ORDER BY total_given + total_received DESC LIMIT 10
    """).fetchall():
        print(f"  {name[:45]:<45} given=${given:,.0f}  received=${received:,.0f}  share={share:.2f}")

    print("\n── 20 random fuzzy_accept matches, score < 99 (for manual QA) ────")
    for raw_name, score, canon, src in con.execute("""
        SELECT l.raw_name, l.match_score, e.canonical_name, l.source_dataset
        FROM entity_links l JOIN entities e ON e.entity_id = l.entity_id
        WHERE l.match_method = 'fuzzy_accept' AND l.match_score < 99
        ORDER BY random() LIMIT 20
    """).fetchall():
        print(f"  [{score:>5.1f}] {raw_name[:38]:<38} -> {canon[:38]:<38} ({src})")

    print("\n── 20 random digit-token-gate rejects, score >= 90 pre-gate (for manual QA — "
          "true branch/circuit splits vs. wrongly split near-duplicates) ────")
    for raw_name, canon, score, src in con.execute("""
        SELECT raw_name, rejected_canonical_name, score, source_dataset
        FROM _fuzzy_gate_rejects ORDER BY random() LIMIT 20
    """).fetchall():
        print(f"  [{score:>5.1f}] {raw_name[:38]:<38} -> {canon[:38]:<38} ({src})")

    print(f"\n{'='*60}\nDONE — database at {DB_PATH}\n{'='*60}")


def main():
    import sys
    # --financials-by-year-only: adds/refreshes entity_financials_by_year
    # against an already-built DB, without re-running entity resolution.
    # Safe because build_entity_financials (its direct model) only reads
    # raw_t3010_fin and entities, both already fully loaded by a prior full
    # run, and nothing downstream of build_entities_and_grants/
    # build_role_summary mutates either table.
    if "--financials-by-year-only" in sys.argv:
        con = duckdb.connect(DB_PATH)
        build_entity_financials_by_year(con)
        con.close()
        return

    # --apply-overrides-only: applies data/bn_merge_overrides.csv against an
    # already-built DB and refreshes the aggregates that depend on entities/
    # grants_unified (entity_role_summary, entity_financials*), without
    # re-running entity resolution. Safe for the same reason
    # --financials-by-year-only is: nothing downstream of
    # build_entities_and_grants() mutates entities/entity_links/
    # grants_unified except _apply_entity_merges() itself. The intended
    # workflow: run a full build, eyeball build_bn_near_miss_review()'s and
    # build_branch_variant_review()'s output CSVs against that build's real
    # entity_ids, add confirmed pairs to data/bn_merge_overrides.csv, then
    # apply them here rather than re-running the whole ~2GB grants.csv +
    # T3010 pipeline just to pick up a handful of manually-confirmed merges.
    if "--apply-overrides-only" in sys.argv:
        con = duckdb.connect(DB_PATH)
        apply_bn_merge_overrides(con)
        build_role_summary(con)
        # Refresh the review queues too: a newly-applied override may have
        # resolved some of the candidates they previously listed (they
        # shouldn't be re-offered once merged), and role_summary must be
        # rebuilt first for the same reason it's ordered first in the main
        # full-build path above -- see that comment for the confirmed bug
        # this ordering avoids.
        build_bn_near_miss_review(con)
        build_branch_variant_review(con)
        build_entity_financials(con)
        build_entity_financials_by_year(con)
        con.close()
        return

    con = duckdb.connect(DB_PATH)
    load_raw(con)
    build_entities_and_grants(con)

    # BN-hygiene entity merges/review queues (docs/webapp-fixes-and-official-
    # links-spec.md follow-up items 1-4): safe, conservative merges are
    # applied automatically (NULL-province residual merges, then any
    # human-confirmed overrides); everything else that *looks* like the same
    # organization but isn't safe to auto-merge (a near-miss BN pair, a
    # branch-suffix name variant) is written to a review CSV instead. Must
    # run after entities/entity_links/grants_unified exist and before
    # build_role_summary()/build_entity_financials*(), so those downstream
    # aggregates reflect the merged state without a second full rebuild.
    print("\nApplying BN-hygiene entity merges ...")
    n_entities_before_merges = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    build_null_province_residual_merges(con)
    apply_bn_merge_overrides(con)
    n_entities_after_merges = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    print(f"  entities: {n_entities_before_merges:,} -> {n_entities_after_merges:,} "
          f"({n_entities_before_merges - n_entities_after_merges:,} merged away)")

    # build_role_summary() must run before the review queues below: both
    # read entity_role_summary for their reported dollar-flow columns, and
    # those flows need to reflect *this* run's post-merge grants_unified,
    # not a stale entity_role_summary left over from a previous build (or a
    # missing table on a first-ever build). Confirmed as a real bug during
    # verification: run 1's near-miss CSV reported $133,000 for an entity
    # whose true post-merge flow (read live from grants_unified moments
    # later, at override-application time) was $217,650,891 -- two full
    # orders of magnitude off, because entity_role_summary hadn't been
    # rebuilt yet in this run when the review CSV was written.
    build_role_summary(con)

    print("\nWriting BN-hygiene review queues ...")
    n_near_miss = build_bn_near_miss_review(con)
    print(f"  {BN_NEAR_MISS_REVIEW_PATH}: {n_near_miss:,} candidate pairs")
    n_branch_variant = build_branch_variant_review(con)
    print(f"  {BRANCH_VARIANT_REVIEW_PATH}: {n_branch_variant:,} candidates")

    build_entity_financials(con)
    build_entity_financials_by_year(con)
    print_report(con)
    con.close()


if __name__ == "__main__":
    main()
