"""
Canadian Nonprofit Data — Entity Graph Builder
Links federal Grants & Contributions (grants.csv), the CRA T3010 charity
registry, and Canada Council for the Arts grants (data/) into one entity
graph, so the same organization is recognized as funder and/or recipient
across all three sources regardless of which name variant it appears
under. See docs/entity-resolution-methodology.md for the approach.

Run with: python analysis/build_entity_graph.py
"""

import os
import re
from collections import defaultdict
from datetime import datetime

import duckdb
from rapidfuzz import fuzz, process
from unidecode import unidecode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRANTS_CSV = os.path.join(ROOT, "grants.csv")
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(ROOT, "nonprofit_network.duckdb")

FUZZY_ACCEPT = 90   # auto-accept threshold (token_sort_ratio, 0-100)
FUZZY_REVIEW = 80   # below this, treat as unmatched

# Partial dept-code -> name lookup, taken from docs/data-publishing-problems.md.
# Departments not listed here just keep their ref_number code as canonical_name.
DEPT_NAMES = {
    "pch": "Canadian Heritage", "ec": "Environment and Climate Change Canada",
    "swc-cfc": "Status of Women Canada", "wd-deo": "Western Economic Diversification Canada",
    "dfatd-maecd": "Global Affairs Canada", "dfo-mpo": "Fisheries and Oceans Canada",
    "nrc-cnrc": "National Research Council Canada", "cic": "Immigration, Refugees and Citizenship Canada",
    "nrcan-rncan": "Natural Resources Canada", "iaac-aeic": "Impact Assessment Agency of Canada",
    "esdc-edsc": "Employment and Social Development Canada", "tc": "Transport Canada",
    "sshrc-crsh": "Social Sciences and Humanities Research Council",
    "isc-sac": "Indigenous Services Canada",
    "aandc-aadnc": "Crown-Indigenous Relations and Northern Affairs Canada",
    "phac-aspc": "Public Health Agency of Canada", "cannor": "Canadian Northern Economic Development Agency",
    "ps-sp": "Public Safety Canada", "cihr-irsc": "Canadian Institutes of Health Research",
    "ic": "Innovation, Science and Economic Development Canada",
    "nserc-crsng": "Natural Sciences and Engineering Research Council",
    "aafc-aac": "Agriculture and Agri-Food Canada", "ced-dec": "Canada Economic Development for Quebec Regions",
    "acoa-apeca": "Atlantic Canada Opportunities Agency", "infc": "Housing, Infrastructure and Communities",
    "csa-asc": "Canadian Space Agency", "cwa-aec": "Canada Water Agency",
}

LEGAL_SUFFIXES = {
    "INC", "INCORPORATED", "LTD", "LIMITED", "LTEE", "CORP", "CORPORATION",
    "FOUNDATION", "FONDATION", "SOCIETY", "SOCIETE", "ASSOCIATION", "ASSOC",
    "ORGANIZATION", "ORGANISATION", "TRUST", "CHARITABLE", "CHARITY", "CHARITE",
    "OF", "THE", "DE", "DU", "LA", "LE", "LES", "AND", "ET",
}

BN9_RE = re.compile(r"^\d{9}$")
BN15_RE = re.compile(r"^\d{9}[A-Z]{2}\d{4}$")

GRANTS_NFP_TYPES = {"N", "A", "S"}  # NFP/charity, Indigenous, academic


# ── normalization helpers ────────────────────────────────────────────────────

def normalize_bn(raw):
    """Reduce a CRA business number (9 or 15 char, with or without spaces) to
    its 9-digit root, since one organization can hold multiple program
    accounts (RR0001, RR0002, BC0001, ...). Returns None if not a plausible BN."""
    if not raw:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    compact = s.replace(" ", "").replace("-", "")
    if BN15_RE.match(compact) or BN9_RE.match(compact):
        return compact[:9]
    parts = s.split()
    if parts and BN9_RE.match(parts[0]):
        return parts[0]
    return None


def normalize_name(raw):
    if not raw:
        return ""
    s = unidecode(str(raw)).upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    tokens = [t for t in s.split() if t not in LEGAL_SUFFIXES]
    return " ".join(tokens) if tokens else " ".join(s.split())


def block_key(province, norm_name):
    prov = (province or "").strip().upper()[:2]
    prefix = norm_name[:4] if norm_name else ""
    return f"{prov}|{prefix}"


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


# ── entity resolution state ─────────────────────────────────────────────────

class Resolver:
    """Holds the growing entity table plus lookup indexes, and resolves a
    (name, bn, province) triple to an entity_id via exact BN match, fuzzy
    name match (blocked by province+name-prefix), or a new residual entity."""

    def __init__(self):
        self.next_id = 1
        self.entities = []  # (entity_id, bn_root, canonical_name, city, province, entity_kind)
        self.bn_to_entity = {}
        self.dept_to_entity = {}
        self.residual_to_entity = {}
        self.fuzzy_index = defaultdict(list)  # block_key -> [(norm_name, entity_id)]
        self.links = []  # (entity_id, source_dataset, raw_name, raw_bn, match_method, match_score)
        self.stats = defaultdict(int)

    def new_id(self):
        eid = self.next_id
        self.next_id += 1
        return eid

    def add_charity(self, bn_root, legal_name, city, province):
        if bn_root in self.bn_to_entity:
            return self.bn_to_entity[bn_root]
        eid = self.new_id()
        self.bn_to_entity[bn_root] = eid
        self.entities.append((eid, bn_root, legal_name, city, province, "charity"))
        norm = normalize_name(legal_name)
        self.fuzzy_index[block_key(province, norm)].append((norm, eid))
        return eid

    def add_dept(self, code):
        code = code.strip().lower()
        if code in self.dept_to_entity:
            return self.dept_to_entity[code]
        eid = self.new_id()
        self.dept_to_entity[code] = eid
        name = DEPT_NAMES.get(code, code.upper())
        self.entities.append((eid, None, name, None, None, "federal_dept"))
        return eid

    def add_funder_org(self, name):
        eid = self.new_id()
        self.entities.append((eid, None, name, None, None, "funder_org"))
        return eid

    def resolve(self, source_dataset, name, bn_raw, province, allow_fuzzy):
        root = normalize_bn(bn_raw)
        if root and root in self.bn_to_entity:
            eid = self.bn_to_entity[root]
            self.stats["exact_bn"] += 1
            self.links.append((eid, source_dataset, name, bn_raw, "exact_bn", 100.0))
            return eid

        norm = normalize_name(name)
        if allow_fuzzy and norm:
            key = block_key(province, norm)
            candidates = self.fuzzy_index.get(key, [])
            if candidates:
                choices = [c[0] for c in candidates]
                match = process.extractOne(
                    norm, choices, scorer=fuzz.token_sort_ratio, score_cutoff=FUZZY_REVIEW
                )
                if match:
                    _, score, idx = match
                    if score >= FUZZY_ACCEPT:
                        eid = candidates[idx][1]
                        self.stats["fuzzy_accept"] += 1
                        self.links.append((eid, source_dataset, name, bn_raw, "fuzzy_accept", score))
                        return eid

        # residual: dedupe unmatched records by normalized name + province
        rkey = (norm, (province or "").strip().upper()[:2])
        if rkey in self.residual_to_entity:
            eid = self.residual_to_entity[rkey]
        else:
            eid = self.new_id()
            self.residual_to_entity[rkey] = eid
            self.entities.append((eid, root, name, None, province, "other_org"))
        self.stats["unmatched_new"] += 1
        self.links.append((eid, source_dataset, name, bn_raw, "unmatched_new", None))
        return eid


# ── pipeline stages ──────────────────────────────────────────────────────────

def load_raw(con):
    print("Loading raw sources into DuckDB ...")
    con.execute(f"CREATE OR REPLACE TABLE raw_grants AS SELECT * FROM read_csv('{GRANTS_CSV}', all_varchar=true)")
    n = con.execute("SELECT COUNT(*) FROM raw_grants").fetchone()[0]
    print(f"  raw_grants: {n:,} rows")

    con.execute(
        f"CREATE OR REPLACE TABLE raw_t3010_ident AS "
        f"SELECT * FROM read_csv('{DATA_DIR}/t3010_identification.csv', all_varchar=true)"
    )
    n = con.execute("SELECT COUNT(*) FROM raw_t3010_ident").fetchone()[0]
    print(f"  raw_t3010_ident: {n:,} rows")

    con.execute(
        f"CREATE OR REPLACE TABLE raw_t3010_qd AS "
        f"SELECT * FROM read_csv('{DATA_DIR}/t3010_qualified_donees.csv', all_varchar=true)"
    )
    n = con.execute("SELECT COUNT(*) FROM raw_t3010_qd").fetchone()[0]
    print(f"  raw_t3010_qd: {n:,} rows")

    con.execute(
        f"CREATE OR REPLACE TABLE raw_t3010_nqd AS "
        f"SELECT * FROM read_csv('{DATA_DIR}/t3010_non_qualified_donees.csv', all_varchar=true)"
    )
    n = con.execute("SELECT COUNT(*) FROM raw_t3010_nqd").fetchone()[0]
    print(f"  raw_t3010_nqd: {n:,} rows")

    con.execute(
        f"CREATE OR REPLACE TABLE raw_t3010_fin AS "
        f"SELECT * FROM read_csv('{DATA_DIR}/t3010_financials.csv', all_varchar=true)"
    )
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


def build_entities_and_grants(con):
    r = Resolver()

    print("\nSeeding entities from T3010 charity registry ...")
    for bn, legal_name, city, province in con.execute(
        'SELECT BN, "Legal Name", City, Province FROM raw_t3010_ident'
    ).fetchall():
        root = normalize_bn(bn)
        if root:
            r.add_charity(root, legal_name, city, province)
    print(f"  {len(r.bn_to_entity):,} charity entities seeded")

    print("Seeding federal department entities from grants.csv ref_number prefixes ...")
    for (code,) in con.execute(
        "SELECT DISTINCT split_part(ref_number, '-', 1) AS code FROM raw_grants WHERE ref_number LIKE '%-%'"
    ).fetchall():
        if code:
            r.add_dept(code)
    print(f"  {len(r.dept_to_entity):,} federal department entities seeded")

    cc_eid = r.add_funder_org("Canada Council for the Arts")

    grants_unified = []  # (source_dataset, funder_entity_id, recipient_entity_id, amount_cad,
                          #  fiscal_year, program_name, description)

    # ── source 1: federal Grants & Contributions ────────────────────────────
    print("\nProcessing federal G&C records (grants.csv) ...")
    cols = [
        "ref_number", "recipient_legal_name", "recipient_business_number", "recipient_type",
        "recipient_province", "recipient_country", "agreement_value", "agreement_start_date",
        "prog_name_en", "description_en",
    ]
    cur = con.execute(f"SELECT {', '.join(cols)} FROM raw_grants")
    processed = 0
    while True:
        batch = cur.fetchmany(50_000)
        if not batch:
            break
        for (ref, name, bn, rtype, province, country, value, start_date, prog, desc) in batch:
            processed += 1
            if processed % 200_000 == 0:
                print(f"  ... {processed:,} rows")
            if not ref or "-" not in ref:
                continue
            dept_code = ref.split("-")[0].strip().lower()
            funder_eid = r.dept_to_entity.get(dept_code)
            if funder_eid is None:
                continue
            allow_fuzzy = (rtype in GRANTS_NFP_TYPES) and (country or "").strip().upper() == "CA"
            recipient_eid = r.resolve("federal_gc", name, bn, province, allow_fuzzy)
            grants_unified.append((
                "federal_gc", funder_eid, recipient_eid, to_float(value),
                fiscal_year_from_date(start_date), prog, desc,
            ))
    print(f"  {processed:,} federal G&C records processed")

    # ── source 2: Canada Council for the Arts ───────────────────────────────
    print("\nProcessing Canada Council grants ...")
    processed = 0
    for (name, rtype, bn, amount, year, province, program) in con.execute(
        "SELECT recipient_name, recipient_type, business_number, amount, cc_year, province, program FROM cc"
    ).fetchall():
        processed += 1
        allow_fuzzy = rtype == "Organization"
        recipient_eid = r.resolve("canada_council", name, bn, province, allow_fuzzy)
        fy = None
        if year and year[:4].isdigit():
            fy = int(year[:4])
        grants_unified.append((
            "canada_council", cc_eid, recipient_eid, to_float(amount), fy, program, None,
        ))
    print(f"  {processed:,} Canada Council records processed")

    # ── source 3: T3010 qualified donees (charity -> charity/qualified-donee gifts) ──
    print("\nProcessing T3010 qualified donee gifts ...")
    processed = 0
    for (bn, fpe, donee_bn, donee_name, city, province, total_gifts) in con.execute(
        'SELECT BN, FPE, "Donee BN", "Donee Name", City, Province, "Total Gifts" FROM raw_t3010_qd'
    ).fetchall():
        processed += 1
        funder_root = normalize_bn(bn)
        funder_eid = r.bn_to_entity.get(funder_root)
        if funder_eid is None:
            continue  # filer not found in identification extract (shouldn't happen)
        recipient_eid = r.resolve("t3010_qualified_donee", donee_name, donee_bn, province, allow_fuzzy=True)
        grants_unified.append((
            "t3010_qualified_donee", funder_eid, recipient_eid, to_float(total_gifts),
            fiscal_year_from_date(fpe, month_cutover=1), "Qualified donee gift", None,
        ))
    print(f"  {processed:,} qualified donee records processed")

    # ── source 4: T3010 grants to non-qualified donees ──────────────────────
    print("\nProcessing T3010 non-qualified donee grants ...")
    processed = 0
    for (bn, fpe, recipient_name, cash, noncash) in con.execute(
        'SELECT BN, FPE, "Recipient Name", "Cash amount", "Non-cash amount" FROM raw_t3010_nqd'
    ).fetchall():
        processed += 1
        funder_root = normalize_bn(bn)
        funder_eid = r.bn_to_entity.get(funder_root)
        if funder_eid is None:
            continue
        recipient_eid = r.resolve("t3010_non_qualified_donee", recipient_name, None, None, allow_fuzzy=True)
        amount = (to_float(cash) or 0) + (to_float(noncash) or 0)
        grants_unified.append((
            "t3010_non_qualified_donee", funder_eid, recipient_eid, amount,
            fiscal_year_from_date(fpe, month_cutover=1), "Non-qualified donee grant", None,
        ))
    print(f"  {processed:,} non-qualified donee records processed")

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

    con.execute("DROP TABLE IF EXISTS entity_links")
    con.execute("""
        CREATE TABLE entity_links (
            entity_id INTEGER, source_dataset VARCHAR, raw_name VARCHAR,
            raw_bn VARCHAR, match_method VARCHAR, match_score DOUBLE
        )
    """)
    con.executemany("INSERT INTO entity_links VALUES (?,?,?,?,?,?)", r.links)

    con.execute("DROP TABLE IF EXISTS grants_unified")
    con.execute("""
        CREATE TABLE grants_unified (
            grant_id INTEGER, source_dataset VARCHAR, funder_entity_id INTEGER,
            recipient_entity_id INTEGER, amount_cad DOUBLE, fiscal_year INTEGER,
            program_name VARCHAR, description VARCHAR
        )
    """)
    con.executemany(
        "INSERT INTO grants_unified VALUES (?,?,?,?,?,?,?,?)",
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
    print("\nBuilding entity_financials (T3010 line codes 4700/4950/5100/5050/4540/4570) ...")
    con.execute("""
        CREATE OR REPLACE TABLE entity_financials AS
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
        FROM raw_t3010_fin f
        JOIN entities e ON e.bn_root = substr(regexp_replace(f.BN, '[^0-9A-Za-z]', ''), 1, 9)
    """)
    n = con.execute("SELECT COUNT(*) FROM entity_financials").fetchone()[0]
    print(f"  entity_financials: {n:,} rows")


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

    print("\n── 20 random fuzzy_accept matches (for manual QA) ────────")
    for eid, src, raw_name, score in con.execute("""
        SELECT l.entity_id, l.source_dataset, l.raw_name, l.match_score
        FROM entity_links l WHERE l.match_method = 'fuzzy_accept'
        USING SAMPLE 20
    """).fetchall():
        canon = con.execute("SELECT canonical_name FROM entities WHERE entity_id = ?", [eid]).fetchone()[0]
        print(f"  [{score:>5.1f}] {raw_name[:35]:<35} -> {canon[:35]:<35} ({src})")

    print(f"\n{'='*60}\nDONE — database at {DB_PATH}\n{'='*60}")


def main():
    con = duckdb.connect(DB_PATH)
    load_raw(con)
    build_entities_and_grants(con)
    build_role_summary(con)
    build_entity_financials(con)
    print_report(con)
    con.close()


if __name__ == "__main__":
    main()
