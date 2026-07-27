"""
Part B of docs/crowding-out-and-flow-through-spec.md -- flow-through mapping:
how much government money passes through regranting charities to named
final recipients, traced as far as the data allows.

Run with: .venv/bin/python analysis/flow_through.py

Builds flow_through_chains (one row per intermediary->donee edge, with
hop-1 context) in nonprofit_network.duckdb, prints headline aggregates, and
writes docs/flow-through-report.md.

Honest denominator: this counts "org X received $A government money and
separately re-granted $B" -- co-occurrence in overlapping fiscal years, not
a trace of any specific dollar. Same "claim and receipt" discipline already
used on org pages (see AGENTS.md).

Scope: hop 1 is federal G&C + OTF + Canada Council (HOP1_SOURCES), matching
the broadened "government funding" scope decided for Part A
(analysis/crowding_out.py) for consistency. The spec's own prototype
numbers ($11.19B / 1,414 charities / $5.23B) were federal-only AND used a
"latest filing only" intermediary rule rather than the year-overlap rule
this script implements per the spec's Design section -- both differences
are stated explicitly in the report rather than silently reconciled.
"""

import os
from datetime import datetime

import duckdb
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "nonprofit_network.duckdb")

DRAFT_DISCLAIMER_MD = (
    "> **DRAFT — research prototype.** This is an unreleased working draft produced for "
    "research purposes only. Figures are derived from public data using experimental "
    "methods, contain known data-quality limitations, and have not been reviewed for "
    "publication. Do not cite, circulate, or rely on any figure or claim in this document.\n"
)

HOP1_SOURCES = ("federal_gc", "otf", "canada_council")
INTERMEDIARY_THRESHOLD = 100_000
MAX_HOP_DEPTH = 2  # regrant edges only; hop 1 (funder->intermediary) isn't itself a row
CANADAHELPS_BN_ROOT = "896568417"  # donation-processing platform -- see build_report()'s caveat

# Spec's documented prototype (federal-only, latest-filing-only rule) -- reconciliation reference.
PROTOTYPE_FEDERAL_ONLY_INTERMEDIARIES = 1414
PROTOTYPE_FEDERAL_ONLY_HOP1_TOTAL = 11.19e9
PROTOTYPE_FEDERAL_ONLY_REGRANTED = 5.23e9


def _source_list_sql(sources):
    return ", ".join(f"'{s}'" for s in sources)


def _hop1_yearly_sql(sources):
    return f"""
        SELECT e.bn_root, e.entity_id, g.source_dataset, g.fiscal_year,
               SUM(g.amount_cad) AS hop1_amount
        FROM grants_unified g
        JOIN entities e ON e.entity_id = g.recipient_entity_id
        WHERE g.source_dataset IN ({_source_list_sql(sources)})
          AND e.bn_root IS NOT NULL
        GROUP BY e.bn_root, e.entity_id, g.source_dataset, g.fiscal_year
    """


def _line5050_panel_sql():
    """Line 5050 (total gifts to qualified donees), deduped to the latest
    source_year per (bn_root, fiscal_year) -- same idiom as
    build_entity_graph.build_entity_financials_by_year."""
    return """
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
        SELECT bn_root,
               EXTRACT(YEAR FROM TRY_CAST(FPE AS DATE))::INTEGER AS fiscal_year,
               TRY_CAST("5050" AS DOUBLE) AS line_5050
        FROM deduped
    """


def flag_intermediaries(con, sources=HOP1_SOURCES, threshold=INTERMEDIARY_THRESHOLD):
    """(bn_root, entity_id, fiscal_year) flagged as an intermediary: line
    5050 > threshold in a T3010 fiscal year that overlaps (same calendar
    year as) a hop-1 receipt from `sources`. "Overlap" = same calendar year
    -- an approximation, same spirit as Part A's funder-FY-vs-charity-FPE
    caveat, documented in the report rather than guessed at more precisely."""
    query = f"""
        WITH hop1 AS ({_hop1_yearly_sql(sources)}),
        panel AS ({_line5050_panel_sql()})
        SELECT p.bn_root, h.entity_id, p.fiscal_year, p.line_5050,
               SUM(h.hop1_amount) AS hop1_amount,
               STRING_AGG(DISTINCT h.source_dataset, ',') AS hop1_source_datasets
        FROM panel p
        JOIN hop1 h ON h.bn_root = p.bn_root AND h.fiscal_year = p.fiscal_year
        WHERE p.line_5050 > {threshold}
        GROUP BY p.bn_root, h.entity_id, p.fiscal_year, p.line_5050
    """
    return con.execute(query).fetchdf()


def large_regranters_by_year(con, threshold=INTERMEDIARY_THRESHOLD):
    """(bn_root, fiscal_year) with line 5050 > threshold, with no
    requirement that the org also received government (hop-1) money of its
    own -- used to decide whether to continue a chain past the first hop.
    A second-hop regranter got its money from the first-hop intermediary,
    not from government directly, so it can't be required to clear
    flag_intermediaries()'s hop-1 join."""
    query = f"""
        SELECT bn_root, fiscal_year, line_5050
        FROM ({_line5050_panel_sql()})
        WHERE line_5050 > {threshold}
    """
    return con.execute(query).fetchdf()


def fetch_donee_gifts(con, bn_roots, fiscal_years=None):
    """Every T3010 Schedule 6 (qualified-donee) gift line for `bn_roots`,
    deduped to the latest source_year per (bn_root, fiscal_year, donee BN,
    donee name) on top of raw_t3010_qd_dedup's existing exact-duplicate-line
    dedup (see AGENTS.md issue #6) -- guards against a re-filed/amended
    year double-counting a regrant, the same amendment discipline used
    elsewhere in this project. Donee BN resolved to entities.bn_root using
    the same inline substr/regexp_replace shortcut build_entity_graph.py's
    own table-builders use (not the more careful Python normalize_bn(),
    which isn't callable from SQL).

    fiscal_years, if given, must be a list of (bn_root, fiscal_year) pairs
    aligned with the actual (org, year) combinations the caller needs --
    e.g. build_chains() only ever looks up one specific flagged year per
    org, not every year that org ever filed. Confirmed necessary at real
    scale: a large regranter can have a decade of filings, and without this
    the chain traversal was pulling (and holding in memory) 10x more gift
    rows than any traversal step could actually use, causing an OOM kill
    once the traversal's frontier grew past a few thousand orgs."""
    if not len(bn_roots):
        return pd.DataFrame(columns=[
            "bn_root", "fiscal_year", "donee_bn_root", "donee_name",
            "donee_raw_bn", "amount", "donee_entity_id",
        ])
    bn_list = ", ".join(f"'{b}'" for b in bn_roots)
    # bn_root filter pushed into the first CTE (not left for an outer WHERE
    # after the QUALIFY/ROW_NUMBER step) -- otherwise DuckDB windows over
    # all 3.7M raw_t3010_qd_dedup rows regardless of how few bn_roots are
    # actually requested. Confirmed necessary: an unfiltered-until-the-end
    # version of this query took minutes for ~1,900 intermediaries.
    year_filter_cte = ""
    year_join = ""
    if fiscal_years:
        values_sql = ", ".join(f"('{b}', {y})" for b, y in fiscal_years)
        year_filter_cte = f", year_keys(bn_root, fiscal_year) AS (VALUES {values_sql})"
        year_join = "JOIN year_keys yk ON yk.bn_root = d.bn_root AND yk.fiscal_year = d.fiscal_year"
    query = f"""
        WITH qd AS (
            SELECT *,
                   substr(regexp_replace(BN, '[^0-9A-Za-z]', ''), 1, 9) AS bn_root,
                   NULLIF(substr(regexp_replace(COALESCE("Donee BN", ''), '[^0-9A-Za-z]', ''), 1, 9), '') AS donee_bn_root,
                   EXTRACT(YEAR FROM TRY_CAST(FPE AS DATE))::INTEGER AS fiscal_year
            FROM raw_t3010_qd_dedup
            WHERE substr(regexp_replace(BN, '[^0-9A-Za-z]', ''), 1, 9) IN ({bn_list})
        ),
        deduped AS (
            SELECT * FROM qd
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY bn_root, fiscal_year, "Donee BN", "Donee Name"
                ORDER BY source_year DESC
            ) = 1
        ){year_filter_cte}
        SELECT
            d.bn_root, d.fiscal_year,
            d.donee_bn_root, d."Donee Name" AS donee_name,
            d."Donee BN" AS donee_raw_bn,
            TRY_CAST(d."Total Gifts" AS DOUBLE) AS amount,
            e.entity_id AS donee_entity_id
        FROM deduped d
        {year_join}
        LEFT JOIN entities e ON e.bn_root = d.donee_bn_root
        WHERE TRY_CAST(d."Total Gifts" AS DOUBLE) IS NOT NULL
    """
    return con.execute(query).fetchdf()


CHAIN_EDGE_SCHEMA = {
    "hop1_source_datasets": "VARCHAR", "hop1_amount": "DOUBLE", "hop1_fiscal_year": "INTEGER",
    "intermediary_bn_root": "VARCHAR", "intermediary_entity_id": "INTEGER", "hop_depth": "INTEGER",
    "donee_bn_root": "VARCHAR", "donee_entity_id": "INTEGER", "donee_raw_name": "VARCHAR",
    "donee_raw_bn": "VARCHAR", "hop2_amount": "DOUBLE", "hop2_fiscal_year": "INTEGER",
    "is_cycle": "BOOLEAN",
}
CHAIN_EDGE_COLUMNS = list(CHAIN_EDGE_SCHEMA)

BATCH_SIZE = 200


def _iter_chain_edge_batches(con, sources=HOP1_SOURCES, threshold=INTERMEDIARY_THRESHOLD,
                              max_hop_depth=MAX_HOP_DEPTH, batch_size=BATCH_SIZE):
    """Yields small DataFrames of intermediary->donee edges, batch by batch,
    rather than building the whole graph in memory at once -- see
    write_flow_through_chains()'s docstring for why. One row per edge, with
    hop-1 context carried from the root intermediary of the chain.
    Traversal is capped at `max_hop_depth` regrant hops (plus the hop-1
    government receipt, so max_hop_depth=2 is government -> intermediary ->
    donee -> sub-donee, 3 hops total, matching the spec's cap). A donee
    whose bn_root already appears earlier in its own chain is flagged as a
    cycle and not traversed further from.

    Only the root (depth-1) intermediary is required to have received
    hop-1 government money itself -- flag_intermediaries()'s hop-1 join.
    A depth-2 regranter got its money from the depth-1 intermediary, not
    from government directly, so continuing the chain past hop 1 only
    requires the donee to independently clear the large-regranter bar
    (large_regranters_by_year()), not a fresh hop-1 receipt of its own.

    Traversal is an iterative level-by-level BFS, not per-node recursion,
    and `fetch_donee_gifts` is called once per batch of `batch_size` orgs
    within each depth level, not once for the whole frontier -- confirmed
    necessary against the real DB: the depth-2 frontier (donees who
    independently clear the large-regranter bar, unrelated to any specific
    upstream chain) came out to ~7,400 distinct orgs, and both fetching all
    of their gift rows in one query *and* accumulating every edge from
    every batch in one Python list before returning pushed the process past
    the machine's memory limit (SIGKILL/exit 137, twice, at two different
    stages) -- batching the fetch AND yielding+discarding each batch's
    edges immediately (instead of returning one big DataFrame) bounds peak
    memory to one batch's worth of gifts and edges.

    The frontier is deduped to one (path, root) per (bn_root, fiscal_year)
    -- a visited-node BFS, not full-path enumeration. Without this, a hub
    reached by many different upstream intermediaries replicates its
    entire own donee list once per upstream parent: confirmed real against
    the full DB, CanadaHelps (a donation-processing platform effectively
    every Canadian charity routes some giving through, bn_root 896568417)
    is independently reached as a donee by 2,624 distinct depth-1
    intermediaries and is itself a large regranter every year 2013-2024,
    which multiplied out to an estimated ~46M spurious rows for that one
    org alone (~98M across the whole depth-2 frontier) before this dedup
    -- not a realistic table size, and not new information: the
    duplicate rows would have been byte-identical except for which
    upstream root's hop1_* context they carried. First-encountered root
    wins arbitrarily; a depth-2+ edge's hop1_* context is therefore
    illustrative of one contributing chain, not exhaustive of every path
    that reaches it -- consistent with the honest-denominator framing
    (money is fungible, no specific root's dollars are being traced to
    begin with, so exhaustively enumerating every root would have implied
    a precision the underlying data can't support anyway)."""
    intermediaries = flag_intermediaries(con, sources, threshold)
    if not len(intermediaries):
        return

    regranters = large_regranters_by_year(con, threshold)
    regranter_years_by_bn = {}
    for bn_root, fiscal_year in zip(regranters["bn_root"], regranters["fiscal_year"]):
        regranter_years_by_bn.setdefault(bn_root, []).append(fiscal_year)

    entity_id_by_bn = dict(zip(intermediaries["bn_root"], intermediaries["entity_id"]))

    # frontier: (bn_root, fiscal_year, ancestor-path, root-context) to expand at the current depth
    frontier = [
        (row.bn_root, row.fiscal_year, frozenset({row.bn_root}), row)
        for row in intermediaries.itertuples()
    ]

    depth = 1
    while frontier and depth <= max_hop_depth:
        frontier_by_bn = {}
        seen_bn_year = set()
        for bn_root, fiscal_year, path, root in frontier:
            key = (bn_root, fiscal_year)
            if key in seen_bn_year:
                continue
            seen_bn_year.add(key)
            frontier_by_bn.setdefault(bn_root, []).append((fiscal_year, path, root))

        missing = [b for b in frontier_by_bn if b not in entity_id_by_bn]
        if missing:
            lookup_list = ", ".join(f"'{b}'" for b in missing)
            lookup = con.execute(
                f"SELECT bn_root, entity_id FROM entities WHERE bn_root IN ({lookup_list})"
            ).fetchdf()
            entity_id_by_bn.update(dict(zip(lookup["bn_root"], lookup["entity_id"])))

        next_frontier = []
        batch_bn_roots = list(frontier_by_bn)
        for batch_start in range(0, len(batch_bn_roots), batch_size):
            batch = batch_bn_roots[batch_start:batch_start + batch_size]
            batch_years = [(bn, fy) for bn in batch for fy, _, _ in frontier_by_bn[bn]]
            gifts = fetch_donee_gifts(con, batch, fiscal_years=batch_years)
            # A plain dict-of-lists built in one itertuples() pass, not
            # DataFrame.groupby() -- confirmed necessary: groupby() over a
            # large result set split into many small (bn_root, fiscal_year)
            # groups spends most of its time materializing per-group
            # sub-DataFrames, which this traversal immediately re-iterates
            # row by row anyway.
            gifts_by_key = {}
            for g in gifts.itertuples():
                gifts_by_key.setdefault((g.bn_root, g.fiscal_year), []).append(g)

            batch_edges = []
            for bn_root in batch:
                for fiscal_year, path, root in frontier_by_bn[bn_root]:
                    grp = gifts_by_key.get((bn_root, fiscal_year))
                    if grp is None:
                        continue
                    next_frontier.extend(
                        _expand_edges(batch_edges, bn_root, fiscal_year, path, root, depth,
                                      grp, entity_id_by_bn, regranter_years_by_bn, max_hop_depth)
                    )
            if batch_edges:
                yield pd.DataFrame(batch_edges, columns=CHAIN_EDGE_COLUMNS)

        frontier = next_frontier
        depth += 1


def build_chains(con, sources=HOP1_SOURCES, threshold=INTERMEDIARY_THRESHOLD, max_hop_depth=MAX_HOP_DEPTH):
    """Materializes the full chain-edge result as one DataFrame -- fine for
    tests and small/moderate graphs. For the real full-scale run, use
    write_flow_through_chains() instead, which streams batches straight
    into the DuckDB table rather than holding them all in Python memory at
    once (see _iter_chain_edge_batches()'s docstring for why that matters)."""
    batches = list(_iter_chain_edge_batches(con, sources, threshold, max_hop_depth))
    if not batches:
        return pd.DataFrame(columns=CHAIN_EDGE_COLUMNS)
    return pd.concat(batches, ignore_index=True)


def _expand_edges(edges, bn_root, fiscal_year, path, root, depth, grp,
                   entity_id_by_bn, regranter_years_by_bn, max_hop_depth):
    """Appends one edge per gift in `grp` to `edges`, returns the
    next-frontier entries spawned by non-cycle, non-capped donees."""
    spawned = []
    for g in grp:
        donee_bn = g.donee_bn_root
        is_cycle = donee_bn is not None and donee_bn in path
        edges.append({
            "hop1_source_datasets": root.hop1_source_datasets,
            "hop1_amount": root.hop1_amount,
            "hop1_fiscal_year": root.fiscal_year,
            "intermediary_bn_root": bn_root,
            "intermediary_entity_id": entity_id_by_bn.get(bn_root),
            "hop_depth": depth,
            "donee_bn_root": donee_bn,
            "donee_entity_id": None if pd.isna(g.donee_entity_id) else int(g.donee_entity_id),
            "donee_raw_name": g.donee_name,
            "donee_raw_bn": g.donee_raw_bn,
            "hop2_amount": g.amount,
            "hop2_fiscal_year": fiscal_year,
            "is_cycle": bool(is_cycle),
        })
        if is_cycle or depth >= max_hop_depth or donee_bn is None:
            continue
        new_path = path | {donee_bn}
        for donee_year in regranter_years_by_bn.get(donee_bn, []):
            spawned.append((donee_bn, donee_year, new_path, root))
    return spawned


def write_flow_through_chains(con, sources=HOP1_SOURCES, threshold=INTERMEDIARY_THRESHOLD,
                               max_hop_depth=MAX_HOP_DEPTH, batch_size=BATCH_SIZE):
    """Streams the chain traversal straight into the flow_through_chains
    table, batch by batch, instead of building the whole graph as one
    in-memory DataFrame first and writing it in one shot -- see
    _iter_chain_edge_batches()'s docstring for why that distinction matters
    at real DB scale (confirmed OOM otherwise). Returns the total edge
    count. compute_aggregates() below reads the result back from the table
    rather than taking a chains_df, for the same reason."""
    columns_sql = ", ".join(f"{name} {dtype}" for name, dtype in CHAIN_EDGE_SCHEMA.items())
    con.execute(f"CREATE OR REPLACE TABLE flow_through_chains ({columns_sql})")
    n = 0
    for batch_df in _iter_chain_edge_batches(con, sources, threshold, max_hop_depth, batch_size):
        con.register("flow_through_batch_df", batch_df)
        con.execute("INSERT INTO flow_through_chains SELECT * FROM flow_through_batch_df")
        con.unregister("flow_through_batch_df")
        n += len(batch_df)
    return n


def compute_aggregates(con, intermediaries, sources=HOP1_SOURCES):
    """Reads headline aggregates back from the already-written
    flow_through_chains table (see write_flow_through_chains) rather than
    an in-memory chains_df -- the full graph is never held in Python memory
    at once at real DB scale."""
    total_hop1 = con.execute(
        f"SELECT COALESCE(SUM(amount_cad), 0) FROM grants_unified WHERE source_dataset IN ({_source_list_sql(sources)})"
    ).fetchone()[0]
    intermediary_hop1_total = intermediaries["hop1_amount"].sum() if len(intermediaries) else 0

    total_regranted = con.execute(
        "SELECT COALESCE(SUM(hop2_amount), 0) FROM flow_through_chains WHERE hop_depth = 1"
    ).fetchone()[0]
    top_intermediaries = con.execute("""
        SELECT intermediary_bn_root, intermediary_entity_id, SUM(hop2_amount) AS hop2_amount
        FROM flow_through_chains
        WHERE hop_depth = 1
        GROUP BY intermediary_bn_root, intermediary_entity_id
        ORDER BY hop2_amount DESC
        LIMIT 20
    """).fetchdf()
    depth_distribution = con.execute("""
        SELECT hop_depth, COUNT(*) AS edge_count
        FROM flow_through_chains
        GROUP BY hop_depth ORDER BY hop_depth
    """).fetchdf()
    n_cycles = con.execute("SELECT COUNT(*) FROM flow_through_chains WHERE is_cycle").fetchone()[0]

    return {
        "total_hop1": total_hop1,
        "n_intermediaries": intermediaries["bn_root"].nunique() if len(intermediaries) else 0,
        "intermediary_hop1_total": intermediary_hop1_total,
        "share_regranted": intermediary_hop1_total / total_hop1 if total_hop1 else None,
        "total_regranted": total_regranted,
        "top_intermediaries": top_intermediaries,
        "depth_distribution": depth_distribution,
        "n_cycles": n_cycles,
    }


# ── Report ────────────────────────────────────────────────────────────────

def _fmt_money(x):
    return f"${x:,.0f}" if x is not None else "—"


def build_report(intermediaries, aggregates, aga_khan_check):
    lines = [DRAFT_DISCLAIMER_MD]
    lines.append("# Flow-Through Mapping: Government Money Through Regranting Charities\n")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    lines.append(
        "## Honest denominator\n\n"
        "This report counts **co-occurrence**, not dollar-tracing: \"org X received "
        "$A in government money and separately re-granted $B\" in an overlapping "
        "fiscal year. Money is fungible and the two totals are not the same dollars "
        "-- this is the same \"claim and receipt\" discipline already used on org "
        "pages, applied here at the network level.\n"
    )
    lines.append(
        "## Reconciliation note\n\n"
        f"The spec's documented prototype (federal-only, \"latest filing only\" "
        f"intermediary rule) found **{PROTOTYPE_FEDERAL_ONLY_INTERMEDIARIES:,} charities** "
        f"both receiving federal G&C money (**${PROTOTYPE_FEDERAL_ONLY_HOP1_TOTAL:,.0f}** all-time) "
        f"and re-granting **${PROTOTYPE_FEDERAL_ONLY_REGRANTED:,.0f}** in their latest filing year. "
        "This report uses a broader government-funding scope (federal G&C + OTF + "
        "Canada Council) and the spec's stricter year-overlap intermediary rule "
        "instead of the prototype's latest-filing shortcut, so the numbers below "
        "are not directly comparable to the prototype's -- both rule changes are "
        "intentional (see this file's module docstring), not a discrepancy to "
        "reconcile away.\n"
    )
    lines.append("## Headline aggregates\n\n")
    lines.append(f"- Total hop-1 receipts (federal G&C + OTF + Canada Council, all years): {_fmt_money(aggregates['total_hop1'])}\n")
    lines.append(f"- Charities flagged as intermediaries: {aggregates['n_intermediaries']:,}\n")
    lines.append(f"- Hop-1 receipts among flagged-intermediary org-years: {_fmt_money(aggregates['intermediary_hop1_total'])}"
                  + (f" ({100 * aggregates['share_regranted']:.1f}% of all hop-1 receipts)\n" if aggregates['share_regranted'] is not None else "\n"))
    lines.append(f"- Total re-granted by intermediaries (direct, hop_depth=1 edges): {_fmt_money(aggregates['total_regranted'])}\n")
    lines.append(f"- Chains flagged as cycles (traversal stopped): {aggregates['n_cycles']:,}\n")

    lines.append("\n## Top intermediaries by amount re-granted\n")
    top = aggregates["top_intermediaries"].copy()
    if len(top):
        top["hop2_amount"] = top["hop2_amount"].map(_fmt_money)
        lines.append("| Intermediary BN root | Entity ID | Total re-granted |\n|---|---|---|\n" + "\n".join(
            f"| {r.intermediary_bn_root} | {r.intermediary_entity_id} | {r.hop2_amount} |" for r in top.itertuples()
        ) + "\n")
        if CANADAHELPS_BN_ROOT in aggregates["top_intermediaries"]["intermediary_bn_root"].values:
            lines.append(
                "\n**Note on CanadaHelps (bn_root " + CANADAHELPS_BN_ROOT + "):** it ranks highly here "
                "because it is a donation-processing platform that routes giving for a large share of "
                "Canadian charities, not because it makes discretionary re-grants the way a foundation "
                "or a charity like The Salvation Army does. Its \"donees\" in T3010 Schedule 6 are "
                "overwhelmingly pass-through disbursements to donor-designated charities, not grant-making "
                "decisions -- lumping it into \"top regranting intermediaries\" without this context would "
                "overstate how much of this list reflects discretionary re-granting.\n"
            )
    else:
        lines.append("_(no intermediaries found)_\n")

    lines.append("\n## Chain-depth distribution\n")
    depth = aggregates["depth_distribution"]
    if len(depth):
        lines.append("| Hop depth | Edges |\n|---|---|\n" + "\n".join(
            f"| {r.hop_depth} | {r.edge_count:,} |" for r in depth.itertuples()
        ) + "\n")
    else:
        lines.append("_(no chains found)_\n")

    lines.append("\n## Worked example: Aga Khan Foundation Canada\n\n")
    if aga_khan_check is not None and len(aga_khan_check):
        lines.append("| Donee | Amount |\n|---|---|\n" + "\n".join(
            f"| {r.donee_raw_name} | {_fmt_money(r.hop2_amount)} |" for r in aga_khan_check.itertuples()
        ) + "\n")
    else:
        lines.append("_(Aga Khan Foundation Canada not found as an intermediary under the current scope/threshold.)_\n")

    lines.append(
        "\n## Limitations\n\n"
        "- **Year overlap is calendar-year equality**, not month-level fiscal "
        "alignment -- a funder's fiscal year and a charity's T3010 fiscal period "
        "end don't necessarily line up (same caveat as Part A).\n"
        "- **Donee BN resolution is best-effort.** A donee with no BN on file, or "
        "a BN that doesn't resolve to an entity in this graph, still appears in "
        "the chain by raw name, just without a linked entity_id.\n"
        "- **Not every real regrant chain is captured.** Only qualified-donee "
        "gifts (Schedule 6) are traced; non-qualified-donee gifts and gifts made "
        "outside the flagged overlapping fiscal year are out of scope.\n"
    )
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    con = duckdb.connect(DB_PATH)

    print("Flagging intermediaries (hop-1 receipt + line 5050 > threshold, overlapping year) ...")
    intermediaries = flag_intermediaries(con, HOP1_SOURCES, INTERMEDIARY_THRESHOLD)
    print(f"  {intermediaries['bn_root'].nunique() if len(intermediaries) else 0:,} distinct intermediary charities")

    print("Building chains (hop 2 + capped hop 3 traversal) and writing flow_through_chains ...")
    n = write_flow_through_chains(con, HOP1_SOURCES, INTERMEDIARY_THRESHOLD, MAX_HOP_DEPTH)
    print(f"  flow_through_chains: {n:,} intermediary->donee edges")

    print("Computing headline aggregates ...")
    aggregates = compute_aggregates(con, intermediaries, HOP1_SOURCES)

    print("Checking Aga Khan Foundation Canada spot check ...")
    aga_khan_check = None
    candidate_bn_roots = con.execute(
        "SELECT bn_root FROM entities WHERE canonical_name ILIKE '%Aga Khan Foundation Canada%' AND bn_root IS NOT NULL"
    ).fetchdf()["bn_root"].tolist()
    if candidate_bn_roots:
        bn_list = ", ".join(f"'{b}'" for b in candidate_bn_roots)
        aga_khan_check = con.execute(f"""
            SELECT donee_raw_name, hop2_amount, hop2_fiscal_year
            FROM flow_through_chains
            WHERE intermediary_bn_root IN ({bn_list}) AND hop_depth = 1
            ORDER BY hop2_amount DESC
        """).fetchdf()
        if len(aga_khan_check):
            print(aga_khan_check.to_string(index=False))

    print("Writing docs/flow-through-report.md ...")
    report = build_report(intermediaries, aggregates, aga_khan_check)
    out_path = os.path.join(ROOT, "docs", "flow-through-report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  {out_path}")

    con.close()


if __name__ == "__main__":
    main()
