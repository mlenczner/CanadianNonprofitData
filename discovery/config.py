"""Thresholds, region filter, and snapshot paths for the Canadian non-charity
nonprofit discovery pipeline (see montreal-discovery-spec.md for the full
design). Values here are starting points per the spec -- calibrate against a
labeled sample before trusting them in production, same caveat the spec
itself makes.

Started as a Montreal-only pilot; the Montreal-island constants below are
kept for that narrower mode (region="montreal"), but the default is now
region="quebec" -- the whole province, via CRA's own Province field rather
than a municipality allowlist (see QUEBEC_PROVINCE_CODE).
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "nonprofit_network.duckdb")

# ── region definition ────────────────────────────────────────────────────────
# "Montreal" is ambiguous (island vs. agglomeration vs. CMM) -- the spec flags
# this explicitly as a risk and asks for it to be pinned down here. Pinned to
# the Island of Montreal: the city of Montreal itself plus the 15 demerged
# municipalities that share the island but never re-merged after 2006. This is
# the narrowest, least ambiguous reading of "Montreal" and the safest default
# for a pilot -- widen to the agglomeration or CMM list here if that turns out
# to be what's wanted once real REQ/CRA data is in hand.
MONTREAL_ISLAND_MUNICIPALITIES = {
    "MONTREAL", "MONTRÉAL",
    "BAIE-D'URFE", "BAIE-D'URFÉ",
    "BEACONSFIELD",
    "COTE-SAINT-LUC", "CÔTE-SAINT-LUC",
    "DOLLARD-DES-ORMEAUX",
    "DORVAL",
    "HAMPSTEAD",
    "KIRKLAND",
    "L'ILE-DORVAL", "L'ÎLE-DORVAL",
    "MONTREAL-EST", "MONTRÉAL-EST",
    "MONTREAL-OUEST", "MONTRÉAL-OUEST",
    "MONT-ROYAL", "TOWN OF MOUNT ROYAL",
    "POINTE-CLAIRE",
    "SAINTE-ANNE-DE-BELLEVUE",
    "SENNEVILLE",
    "WESTMOUNT",
}

# Fallback blocking key when postal/FSA don't produce a candidate -- Montreal
# proper's FSAs start with H1-H4 and H8-H9; H5/H6/H7 lean into off-island
# suburbs (Laval, North/South Shore) so are deliberately excluded here. Only
# used as a last-resort widening, never as the primary region filter -- the
# city-name allowlist above is.
MONTREAL_FSA_PREFIXES = {f"H{n}" for n in [1, 2, 3, 4, 8, 9]}

# Quebec-wide region: CRA's own Province field on a charity's mailing address.
# Confirmed against raw_t3010_ident -- "QC" is the only Quebec value present
# (no "PQ"/"Quebec" variants to also match), so a straight equality check is
# reliable here in a way a municipality allowlist never was.
QUEBEC_PROVINCE_CODE = "QC"

# ── REQ (Données Québec "Registre des entreprises") ──────────────────────────
# Verified against a real REQ export (2026-07-01 snapshot) and its official
# "Guide d'utilisation" (IN-537, 2025-11) -- see docs/montreal-discovery-spec.md's
# Deviations note for what changed from the original placeholder assumption.
# The real dataset is NOT one denormalized CSV: it's several relational files
# joined by NEQ. Only the two used by ingest/req.py are listed here (the
# ZIP also ships Etablissements.csv, FusionScissions.csv,
# ContinuationsTransformations.csv -- not needed for this pilot).
REQ_ENTREPRISE_FILE = "Entreprise.csv"  # one row per company
REQ_NOM_FILE = "Nom.csv"                # one row per name (legal name + trade names + name history)

# Entreprise.csv's COD_FORME_JURI is a CODE, not text -- resolved via
# DomaineValeur.csv (TYP_DOM_VAL='FORM_JURI'). Confirmed: 'APE' is the only
# code mapping to "Personne morale sans but lucratif", matching the spec's
# scope exactly (spec says this one legal form, not also 'ASS' = Association,
# a distinct, looser legal form the spec doesn't mention).
REQ_NPO_FORME_JURI_CODE = "APE"

# COD_STAT_IMMAT (DomaineValeur.csv TYP_DOM_VAL='STAT_IMMAT'): 'IM'
# ("Immatriculée") is the only currently-active status; AI/NI/RD/RO/RX all
# mean not currently registered (intent-to-incorporate, never registered, or
# struck off in one of three ways). The pilot's question is "does this legal
# nonprofit exist" in the present tense, so only 'IM' is included -- a
# decision, not spec-mandated (unlike CRA's active+revoked inclusion, which
# the spec does call for, since a revoked *charity* still answers "is/was
# this a charity"; a struck-off REQ entity doesn't currently exist as one).
REQ_ACTIVE_STAT_IMMAT_CODE = "IM"

# Nom.csv's TYP_NOM_ASSUJ has no DomaineValeur.csv entries (empty for that
# TYP_DOM_VAL) -- these codes were inferred from real sample rows instead:
# 'N' = the legal/constitutive name (normally one per NEQ); 'A' = "autre nom"
# (a trade name/AKA, can be several, can be a translation e.g. an English
# name alongside the French legal one); 'M' = an auto-assigned numbered-
# company placeholder name (e.g. "9000-0019 QUÉBEC INC.") -- not meaningful
# for an NPO's actual name, excluded. STAT_NOM ('V'=en vigueur/current,
# 'A'=antérieur/former, 'F'=future, 'R'=réservé) is filtered to 'V' so a name
# change's old name doesn't get treated as the current legal name.
REQ_NAME_TYPE_LEGAL = "N"
REQ_NAME_TYPE_TRADE = "A"
REQ_NAME_STATUS_CURRENT = "V"

# ── Corporations Canada (federal not-for-profit registry, national) ─────────
# Phase 3 ("scale beyond Quebec") data source -- national, unlike REQ (Quebec-
# only). Bulk CSV from the Federal Corporations open-data dataset
# (https://open.canada.ca/data/en/dataset/0032ce54-c5dd-4b66-99a0-320a7b5e99f2),
# downloaded to data/ (not bundled/auto-downloaded by this repo, same
# convention as REQ). Confirmed against a real 2026-07-16 snapshot (50,665
# total rows): this file bundles not-for-profit corporations together with
# cooperatives, boards of trade, and a handful of special-act corporations in
# one file -- "Governing legislation" is a plain readable string (no
# code+lookup-table indirection REQ's COD_FORME_JURI needed), and filtering to
# it narrows to 49,431 NFP Act rows, mirroring REQ's narrow scope (one legal
# form, not every category the source file contains).
CC_ACTIVE_NON_CBCA_FILE = "corporations-active-non-cbca-en.csv"
CC_NFP_ACT_GOVERNING_LEGISLATION = "Canada Not-for-profit Corporations Act"

# ── matching thresholds (starting points, per spec -- calibrate, don't trust blind) ──
AUTO_MATCH_SCORE = 90
NEEDS_REVIEW_FLOOR = 75  # [NEEDS_REVIEW_FLOOR, AUTO_MATCH_SCORE) -> needs_review; below -> no match

# ── social-purpose signal ────────────────────────────────────────────────────
# Federal programs labeled "explicitly social" -- a definitional task the spec
# keeps here deliberately. Starting list only; this is the same kind of
# judgment call analysis/classify_l2.py already makes when assigning Candid
# PCS subject codes to grant descriptions -- before hand-maintaining a second,
# unrelated list here, check whether evidence/l2_classifications.duckdb's PCS
# codes can answer "is this program social" directly. Not done yet because the
# L2 pilot only covers a 1,000-text sample so far (docs/l2-pilot-report.md),
# not the full grant-description corpus -- revisit once/if that scales up.
SOCIAL_PROGRAM_NAME_KEYWORDS = {
    "homelessness", "housing", "social development", "youth", "settlement",
    "immigrant", "refugee", "community", "poverty", "family violence",
    "employment insurance", "social innovation", "accessibility", "seniors",
}
