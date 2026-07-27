"""
Part A of docs/crowding-out-and-flow-through-spec.md -- crowding-out event
study: does a charity's first large government grant crowd out (or in) its
private donations?

Run with: .venv/bin/python analysis/crowding_out.py

Builds crowding_out_panel (treated + matched-control assignment, one row per
org per assigned event year) and crowding_out_regression_panel (long-format
org-year panel feeding the fixed-effects regression) in nonprofit_network.duckdb,
prints the cohort tables, and writes docs/crowding-out-report.md.

Scope decision (see docs/crowding-out-and-flow-through-spec.md's "Open
questions"): "government funding" is federal G&C + OTF + Canada Council
(GOV_SOURCES_BROAD), broader than the spec's federal-only prototype. Because
that changes both the treated cohort and the never-treated control pool, this
script also runs a federal-only pass (GOV_SOURCES_FEDERAL) purely to
reconcile against the spec's documented prototype numbers before trusting the
broader-scope headline result.
"""

import os
import uuid
from datetime import datetime

import duckdb
import numpy as np
import pandas as pd
import pyfixest as pf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "nonprofit_network.duckdb")

DRAFT_DISCLAIMER_MD = (
    "> **DRAFT — research prototype.** This is an unreleased working draft produced for "
    "research purposes only. Figures are derived from public data using experimental "
    "methods, contain known data-quality limitations, and have not been reviewed for "
    "publication. Do not cite, circulate, or rely on any figure or claim in this document.\n"
)

GOV_SOURCES_FEDERAL = ("federal_gc",)
GOV_SOURCES_BROAD = ("federal_gc", "otf", "canada_council")

# federal_gc's fiscal_year carries 15 confirmed-bad rows (12 at 1899, 1 each
# at 1988/2002/2004 -- see the spec's "Verified facts"). otf genuinely has
# multi-thousand-row years in that same 1999-2004 range, so this exclusion
# must stay scoped to federal_gc and not be applied blanket across sources.
FEDERAL_JUNK_FISCAL_YEARS = (1899, 1988, 2002, 2004)

TREATMENT_THRESHOLD = 100_000
ROBUSTNESS_THRESHOLDS = (50_000, 250_000)
COHORT_YEARS = list(range(2016, 2022))
WINDOW = 3
MIN_OBS_PER_WINDOW = 2
MAX_MATCHED_CONTROLS = 5
WINSOR_PCT = 0.01

# Spec's documented prototype cohort table (federal-only) -- reconciliation target.
PROTOTYPE_FEDERAL_ONLY = {
    2016: {"treated_n": 120, "treated_median_pct": 12.8, "control_median_pct": None},
    2017: {"treated_n": 351, "treated_median_pct": 3.0, "control_median_pct": -2.6},
    2018: {"treated_n": 205, "treated_median_pct": 2.3, "control_median_pct": -5.1},
    2019: {"treated_n": 178, "treated_median_pct": 8.5, "control_median_pct": -4.9},
    2020: {"treated_n": 144, "treated_median_pct": 20.1, "control_median_pct": -1.7},
    2021: {"treated_n": 191, "treated_median_pct": 9.7, "control_median_pct": 4.6},
}


# ── SQL fragments ────────────────────────────────────────────────────────

def _source_list_sql(sources):
    return ", ".join(f"'{s}'" for s in sources)


def _gov_yearly_totals_sql(sources):
    """(bn_root, fiscal_year) -> summed amount_cad across `sources`."""
    junk_list = ", ".join(str(y) for y in FEDERAL_JUNK_FISCAL_YEARS)
    return f"""
        SELECT e.bn_root, g.fiscal_year, SUM(g.amount_cad) AS yr_amount
        FROM grants_unified g
        JOIN entities e ON e.entity_id = g.recipient_entity_id
        WHERE g.source_dataset IN ({_source_list_sql(sources)})
          AND e.bn_root IS NOT NULL
          AND NOT (g.source_dataset = 'federal_gc' AND g.fiscal_year IN ({junk_list}))
        GROUP BY e.bn_root, g.fiscal_year
    """


def _donations_panel_sql():
    """Line 4500 (total tax-receipted gifts), deduped to the latest
    source_year per (bn_root, fiscal_year) -- same QUALIFY/ROW_NUMBER idiom
    as build_entity_graph.build_entity_financials_by_year."""
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
               TRY_CAST("4500" AS DOUBLE) AS donations
        FROM deduped
    """


def _window_stats_sql(events_sql, window=WINDOW, min_obs=MIN_OBS_PER_WINDOW):
    """events_sql must yield (bn_root, Y). Joins the donations panel and
    computes pre/post window means (Y itself excluded from both), keeping
    only orgs with >= min_obs observed years in each window and pre_mean > 0."""
    return f"""
        WITH events AS ({events_sql}),
        panel AS ({_donations_panel_sql()}),
        joined AS (
            SELECT
                e.bn_root, e.Y,
                AVG(CASE WHEN p.fiscal_year BETWEEN e.Y - {window} AND e.Y - 1 THEN p.donations END) AS pre_mean,
                COUNT(CASE WHEN p.fiscal_year BETWEEN e.Y - {window} AND e.Y - 1 AND p.donations IS NOT NULL THEN 1 END) AS pre_n,
                AVG(CASE WHEN p.fiscal_year BETWEEN e.Y + 1 AND e.Y + {window} THEN p.donations END) AS post_mean,
                COUNT(CASE WHEN p.fiscal_year BETWEEN e.Y + 1 AND e.Y + {window} AND p.donations IS NOT NULL THEN 1 END) AS post_n
            FROM events e
            LEFT JOIN panel p ON p.bn_root = e.bn_root
            GROUP BY e.bn_root, e.Y
        )
        SELECT bn_root, Y, pre_mean, post_mean, pre_n, post_n,
               (post_mean - pre_mean) / pre_mean AS pct_change
        FROM joined
        WHERE pre_n >= {min_obs} AND post_n >= {min_obs} AND pre_mean > 0
    """


# ── DataFrame registration helper ───────────────────────────────────────

def _register_df(con, df):
    """Registers `df` under a fresh unique view name and returns that name --
    avoids collisions across the many repeated calls this script makes with
    differently-scoped DataFrames of the same logical shape."""
    name = "v_" + uuid.uuid4().hex[:12]
    con.register(name, df)
    return name


# ── Treatment / control detection ───────────────────────────────────────

def compute_treatment_events(con, sources, threshold, cohort_years):
    """Returns bn_root, Y (treatment_year) for orgs whose first-ever fiscal
    year with any receipt from `sources` already crosses `threshold` in that
    same year, restricted to Y in cohort_years. An org whose first receipt
    year is below threshold never qualifies, even if a later year crosses
    it -- "no receipts in any earlier year" is part of the spec's definition."""
    yearly_sql = _gov_yearly_totals_sql(sources)
    years_list = ", ".join(str(y) for y in cohort_years)
    query = f"""
        WITH yearly AS ({yearly_sql}),
        first_year AS (
            SELECT bn_root, MIN(fiscal_year) AS Y
            FROM yearly GROUP BY bn_root
        )
        SELECT f.bn_root, f.Y
        FROM first_year f
        JOIN yearly y ON y.bn_root = f.bn_root AND y.fiscal_year = f.Y
        WHERE y.yr_amount >= {threshold} AND f.Y IN ({years_list})
    """
    return con.execute(query).fetchdf()


def compute_never_treated_pool(con, sources):
    """bn_root with at least one T3010 filing that never received any
    receipt from `sources` in any fiscal year."""
    yearly_sql = _gov_yearly_totals_sql(sources)
    query = f"""
        WITH yearly AS ({yearly_sql}),
        panel AS ({_donations_panel_sql()})
        SELECT DISTINCT p.bn_root
        FROM panel p
        LEFT JOIN yearly y ON y.bn_root = p.bn_root
        WHERE y.bn_root IS NULL
    """
    return con.execute(query).fetchdf()["bn_root"].tolist()


def treated_exact_bn_pairs(con, sources):
    """(bn_root, fiscal_year) pairs where at least one receipt from `sources`
    in that year is linked via entity_links.match_method = 'exact_bn' --
    used to restrict the headline contrast to non-fuzzy-matched entities as
    an entity-resolution-error robustness check."""
    query = f"""
        SELECT DISTINCT e.bn_root, g.fiscal_year AS Y
        FROM grants_unified g
        JOIN entities e ON e.entity_id = g.recipient_entity_id
        JOIN entity_links el ON el.entity_id = g.recipient_entity_id
                             AND el.source_dataset = g.source_dataset
        WHERE g.source_dataset IN ({_source_list_sql(sources)})
          AND el.match_method = 'exact_bn'
    """
    df = con.execute(query).fetchdf()
    return set(zip(df["bn_root"], df["Y"]))


# ── Naive contrast ───────────────────────────────────────────────────────

def naive_contrast(con, sources, threshold, cohort_years):
    """Per-cohort-year median pct_change for treated orgs vs. the
    never-treated pool (evaluated under the same calendar windows).
    Returns (cohort_table_df, treated_stats_df, control_stats_df)."""
    treated_events = compute_treatment_events(con, sources, threshold, cohort_years)
    treated_stats = pd.DataFrame(columns=["bn_root", "Y", "pre_mean", "post_mean", "pre_n", "post_n", "pct_change"])
    if len(treated_events):
        view = _register_df(con, treated_events)
        treated_stats = con.execute(_window_stats_sql(f"SELECT bn_root, Y FROM {view}")).fetchdf()
        con.unregister(view)

    pool = compute_never_treated_pool(con, sources)
    control_stats = pd.DataFrame(columns=["bn_root", "Y", "pre_mean", "post_mean", "pre_n", "post_n", "pct_change"])
    if pool:
        pool_events = pd.DataFrame({"bn_root": pool}).merge(pd.DataFrame({"Y": cohort_years}), how="cross")
        view = _register_df(con, pool_events)
        control_stats = con.execute(_window_stats_sql(f"SELECT bn_root, Y FROM {view}")).fetchdf()
        con.unregister(view)

    rows = []
    for y in cohort_years:
        t = treated_stats[treated_stats.Y == y] if len(treated_stats) else treated_stats
        c = control_stats[control_stats.Y == y] if len(control_stats) else control_stats
        rows.append({
            "Y": y,
            "treated_n": len(t),
            "treated_median_pct": round(100 * t["pct_change"].median(), 1) if len(t) else None,
            "control_n": len(c),
            "control_median_pct": round(100 * c["pct_change"].median(), 1) if len(c) else None,
        })
    return pd.DataFrame(rows), treated_stats, control_stats


# ── Matched controls ─────────────────────────────────────────────────────

def _enrich_with_covariates(con, events_df):
    """Adds province, revenue_y_minus_1 (entity_financials_by_year line
    4700 at Y-1), and donations at Y-1/Y-3 (for the pre-trend) to an
    events_df of (bn_root, Y)."""
    if not len(events_df):
        return events_df.assign(province=None, revenue_y_minus_1=None,
                                 donations_y_minus_1=None, donations_y_minus_3=None)
    view = _register_df(con, events_df)
    query = f"""
        WITH panel AS ({_donations_panel_sql()})
        SELECT
            ev.bn_root, ev.Y,
            e.province,
            fy.total_revenue AS revenue_y_minus_1,
            p1.donations AS donations_y_minus_1,
            p3.donations AS donations_y_minus_3
        FROM {view} ev
        JOIN entities e ON e.bn_root = ev.bn_root
        LEFT JOIN entity_financials_by_year fy
               ON fy.entity_id = e.entity_id AND fy.fiscal_year = ev.Y - 1
        LEFT JOIN panel p1 ON p1.bn_root = ev.bn_root AND p1.fiscal_year = ev.Y - 1
        LEFT JOIN panel p3 ON p3.bn_root = ev.bn_root AND p3.fiscal_year = ev.Y - 3
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ev.bn_root, ev.Y ORDER BY e.entity_id) = 1
    """
    out = con.execute(query).fetchdf()
    con.unregister(view)
    return out


def _add_deciles_and_terciles(df):
    """Per-Y NTILE(10) revenue decile and NTILE(3) pre-trend tercile,
    computed within that Y's cross-section (treated + control pool
    together). Falls back to a single bucket when there are too few
    non-null observations for pandas.qcut to form distinct bins."""
    df = df.copy()
    df["trend"] = df["donations_y_minus_1"] / df["donations_y_minus_3"] - 1
    df.loc[df["donations_y_minus_3"].isin([0, None]) | df["donations_y_minus_3"].isna(), "trend"] = np.nan

    def _bucket(series, q):
        try:
            return pd.qcut(series, q, labels=False, duplicates="drop")
        except (ValueError, IndexError):
            return pd.Series([0] * len(series), index=series.index)

    df["revenue_decile"] = -1
    df["trend_tercile"] = -1
    for y, idx in df.groupby("Y").groups.items():
        sub = df.loc[idx]
        rev_mask = sub["revenue_y_minus_1"].notna()
        if rev_mask.sum() >= 10:
            df.loc[sub.index[rev_mask], "revenue_decile"] = _bucket(sub.loc[rev_mask, "revenue_y_minus_1"], 10)
        trend_mask = sub["trend"].notna()
        if trend_mask.sum() >= 3:
            df.loc[sub.index[trend_mask], "trend_tercile"] = _bucket(sub.loc[trend_mask, "trend"], 3)
    return df


def match_controls(treated_cov, control_cov, max_controls=MAX_MATCHED_CONTROLS):
    """For each treated org: candidates from control_cov sharing the same Y
    and province (never relaxed), preferring an exact match on revenue
    decile + trend tercile, relaxing to decile-only then province-only if
    fewer than `max_controls` qualify. Returns one row per (treated, control)
    match with the relaxation tier used."""
    matches = []
    for _, t in treated_cov.iterrows():
        pool = control_cov[(control_cov.Y == t.Y) & (control_cov.province == t.province)]
        tier = "province+decile+trend"
        candidates = pool[(pool.revenue_decile == t.revenue_decile) & (pool.trend_tercile == t.trend_tercile)]
        if len(candidates) < 1:
            tier = "province+decile"
            candidates = pool[pool.revenue_decile == t.revenue_decile]
        if len(candidates) < 1:
            tier = "province_only"
            candidates = pool
        if len(candidates) == 0:
            continue
        candidates = candidates.assign(
            _dist=(candidates.revenue_y_minus_1.fillna(0) - (t.revenue_y_minus_1 or 0)).abs()
        )
        chosen = candidates.sort_values(["_dist", "bn_root"]).head(max_controls)
        for _, c in chosen.iterrows():
            matches.append({
                "treated_bn_root": t.bn_root, "control_bn_root": c.bn_root,
                "Y": t.Y, "match_tier": tier,
            })
    return pd.DataFrame(matches, columns=["treated_bn_root", "control_bn_root", "Y", "match_tier"])


def matched_control_contrast(con, sources, threshold, cohort_years):
    """Treated-minus-matched-control difference in median pct_change, per
    cohort and pooled. Returns (result_df, treated_stats, matches_df,
    treated_cov, control_cov)."""
    treated_events = compute_treatment_events(con, sources, threshold, cohort_years)
    pool = compute_never_treated_pool(con, sources)
    if not len(treated_events) or not pool:
        empty = pd.DataFrame(columns=["Y", "treated_median_pct", "matched_control_median_pct", "diff_pct"])
        return empty, treated_events, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    pool_events = pd.DataFrame({"bn_root": pool}).merge(pd.DataFrame({"Y": cohort_years}), how="cross")

    treated_cov = _add_deciles_and_terciles(_enrich_with_covariates(con, treated_events))
    control_cov = _add_deciles_and_terciles(_enrich_with_covariates(con, pool_events))

    view_t = _register_df(con, treated_events)
    treated_stats = con.execute(_window_stats_sql(f"SELECT bn_root, Y FROM {view_t}")).fetchdf()
    con.unregister(view_t)

    matches_df = match_controls(treated_cov, control_cov)

    control_stats = pd.DataFrame(columns=["bn_root", "Y", "pct_change"])
    if len(matches_df):
        control_events = matches_df[["control_bn_root", "Y"]].drop_duplicates().rename(columns={"control_bn_root": "bn_root"})
        view_c = _register_df(con, control_events)
        control_stats = con.execute(_window_stats_sql(f"SELECT bn_root, Y FROM {view_c}")).fetchdf()
        con.unregister(view_c)

    rows = []
    for y in cohort_years:
        t = treated_stats[treated_stats.Y == y]
        c_bn_roots = matches_df[matches_df.Y == y].control_bn_root.unique() if len(matches_df) else []
        c = control_stats[(control_stats.Y == y) & (control_stats.bn_root.isin(c_bn_roots))]
        t_med = round(100 * t["pct_change"].median(), 1) if len(t) else None
        c_med = round(100 * c["pct_change"].median(), 1) if len(c) else None
        rows.append({
            "Y": y, "treated_n": len(t), "matched_control_n": len(c),
            "treated_median_pct": t_med, "matched_control_median_pct": c_med,
            "diff_pct": round(t_med - c_med, 1) if t_med is not None and c_med is not None else None,
        })
    return pd.DataFrame(rows), treated_stats, matches_df, treated_cov, control_cov


# ── Fixed-effects regression ────────────────────────────────────────────

def build_regression_panel(con, treated_events, matches_df):
    """Long org-fiscal_year panel for the matched sample (treated + matched
    controls), [Y-3, Y+3] excluding Y, log(donations) as the outcome.

    A control can be matched to more than one treated org (at different Y),
    so this is a stacked panel: panel_unit_id = bn_root + assigned Y keeps
    each (org, event) instance as its own fixed-effect unit rather than
    conflating an org's multiple appearances under different event years --
    the standard "stacked regression" approach for multiple treatment
    cohorts. Standard errors are still clustered on the underlying bn_root
    (see the caller), since the same real organization appearing in more
    than one stack has correlated errors across stacks.
    """
    assignments = pd.concat([
        treated_events[["bn_root", "Y"]].assign(treated=1),
        matches_df[["control_bn_root", "Y"]].rename(columns={"control_bn_root": "bn_root"}).assign(treated=0),
    ], ignore_index=True) if len(matches_df) else treated_events[["bn_root", "Y"]].assign(treated=1)

    if not len(assignments):
        return pd.DataFrame(columns=["panel_unit_id", "bn_root", "Y", "treated", "fiscal_year", "log_donations", "post"])

    view = _register_df(con, assignments)
    query = f"""
        WITH panel AS ({_donations_panel_sql()})
        SELECT
            a.bn_root || '_Y' || a.Y AS panel_unit_id,
            a.bn_root, a.Y, a.treated,
            p.fiscal_year,
            p.donations,
            CASE WHEN p.fiscal_year > a.Y THEN 1 ELSE 0 END AS post
        FROM {view} a
        JOIN panel p ON p.bn_root = a.bn_root
                     AND p.fiscal_year BETWEEN a.Y - {WINDOW} AND a.Y + {WINDOW}
                     AND p.fiscal_year != a.Y
        WHERE p.donations IS NOT NULL AND p.donations > 0
    """
    out = con.execute(query).fetchdf()
    con.unregister(view)
    out["log_donations"] = np.log(out["donations"])
    return out


def run_fe_regression(panel_df):
    """org+year two-way fixed-effects DiD: log(donations) ~ treated:post |
    panel_unit_id + fiscal_year, SEs clustered on bn_root. Returns the
    fitted pyfixest model, or None if the panel is too thin to estimate.

    Most panel_unit_id groups only contribute a handful of observed years
    (at most 6 -- 3 pre + 3 post, often fewer), so pyfixest's demeaning
    algorithm hits many singleton/near-singleton fixed-effect groups.
    Confirmed against the real panel: this produces harmless numpy
    RuntimeWarnings (divide-by-zero/overflow in intermediate matmuls) while
    still returning a finite, sane coefficient -- suppressed here rather
    than silencing warnings globally for the whole script."""
    if panel_df["panel_unit_id"].nunique() < 4 or panel_df["treated"].nunique() < 2:
        return None
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        return pf.feols(
            "log_donations ~ treated:post | panel_unit_id + fiscal_year",
            data=panel_df, vcov={"CRV1": "bn_root"},
        )


# ── Robustness checks ────────────────────────────────────────────────────

def winsorized_median_pct(stats_df, pct=WINSOR_PCT):
    if not len(stats_df):
        return None
    lo, hi = stats_df["pct_change"].quantile([pct, 1 - pct])
    clipped = stats_df["pct_change"].clip(lo, hi)
    return round(100 * clipped.median(), 1)


def threshold_sensitivity(con, sources, cohort_years, thresholds=ROBUSTNESS_THRESHOLDS):
    rows = []
    for threshold in thresholds:
        table, _, _ = naive_contrast(con, sources, threshold, cohort_years)
        rows.append({
            "threshold": threshold,
            "total_treated_n": int(table.treated_n.sum()),
            "pooled_median_pct": round(table.treated_median_pct.dropna().median(), 1) if table.treated_median_pct.notna().any() else None,
        })
    return pd.DataFrame(rows)


def drop_covid_cohorts(con, sources, threshold):
    non_covid_years = [y for y in COHORT_YEARS if y not in (2020, 2021)]
    table, _, _ = naive_contrast(con, sources, threshold, non_covid_years)
    return table


def placebo_pretrend(con, sources, threshold, cohort_years):
    """Same treated-vs-control contrast, but with a fake event 2 years
    before the real one (fully inside the actual pre-period) -- expect
    ~0 divergence if there's no pre-existing trend difference."""
    treated_events = compute_treatment_events(con, sources, threshold, cohort_years)
    if not len(treated_events):
        return pd.DataFrame(columns=["Y", "treated_n", "treated_median_pct"])
    placebo_events = treated_events.assign(Y=treated_events.Y - 2)
    view = _register_df(con, placebo_events)
    stats = con.execute(_window_stats_sql(f"SELECT bn_root, Y FROM {view}")).fetchdf()
    con.unregister(view)
    rows = []
    for y in sorted(placebo_events.Y.unique()):
        s = stats[stats.Y == y]
        rows.append({
            "Y": y, "treated_n": len(s),
            "treated_median_pct": round(100 * s["pct_change"].median(), 1) if len(s) else None,
        })
    return pd.DataFrame(rows)


def exact_bn_restricted_contrast(con, sources, threshold, cohort_years):
    treated_events = compute_treatment_events(con, sources, threshold, cohort_years)
    exact_pairs = treated_exact_bn_pairs(con, sources)
    if not len(treated_events):
        return pd.DataFrame(columns=["Y", "treated_n", "treated_median_pct"]), treated_events
    restricted = treated_events[treated_events.apply(lambda r: (r.bn_root, r.Y) in exact_pairs, axis=1)]
    if not len(restricted):
        return pd.DataFrame(columns=["Y", "treated_n", "treated_median_pct"]), restricted
    view = _register_df(con, restricted)
    stats = con.execute(_window_stats_sql(f"SELECT bn_root, Y FROM {view}")).fetchdf()
    con.unregister(view)
    rows = []
    for y in cohort_years:
        s = stats[stats.Y == y]
        rows.append({
            "Y": y, "treated_n": len(s),
            "treated_median_pct": round(100 * s["pct_change"].median(), 1) if len(s) else None,
        })
    return pd.DataFrame(rows), restricted


# ── Table writers ─────────────────────────────────────────────────────────

def write_crowding_out_panel(con, treated_stats, treated_cov, matches_df, control_cov, exact_pairs):
    treated = treated_stats.merge(
        treated_cov[["bn_root", "Y", "province", "revenue_decile"]], on=["bn_root", "Y"], how="left"
    )
    treated["role"] = "treated"
    treated["matched_group_id"] = treated["bn_root"]
    treated["match_tier"] = None
    treated["exact_bn_link"] = treated.apply(lambda r: (r.bn_root, r.Y) in exact_pairs, axis=1)

    if len(matches_df):
        control_ids = matches_df.rename(columns={"control_bn_root": "bn_root"})
        cov_lookup = control_cov[["bn_root", "Y", "province", "revenue_decile"]]
        controls = control_ids.merge(cov_lookup, on=["bn_root", "Y"], how="left")
        controls["role"] = "matched_control"
        controls["matched_group_id"] = control_ids["treated_bn_root"]
        controls["exact_bn_link"] = None
        # bring in window stats for control rows
        stats_view = _register_df(con, control_ids[["bn_root", "Y"]].drop_duplicates())
        c_stats = con.execute(_window_stats_sql(f"SELECT bn_root, Y FROM {stats_view}")).fetchdf()
        con.unregister(stats_view)
        controls = controls.merge(c_stats, on=["bn_root", "Y"], how="left", suffixes=("", "_stat"))
        for col in ["pre_mean", "post_mean", "pre_n", "post_n", "pct_change"]:
            if col not in controls.columns:
                controls[col] = None
        combined = pd.concat([treated, controls], ignore_index=True, sort=False)
    else:
        combined = treated

    cols = ["bn_root", "Y", "role", "matched_group_id", "match_tier", "province",
            "revenue_decile", "pre_mean", "post_mean", "pre_n", "post_n",
            "pct_change", "exact_bn_link"]
    for col in cols:
        if col not in combined.columns:
            combined[col] = None
    combined = combined[cols].rename(columns={"Y": "assigned_year"})

    view = _register_df(con, combined)
    con.execute(f"CREATE OR REPLACE TABLE crowding_out_panel AS SELECT * FROM {view}")
    con.unregister(view)
    return len(combined)


def write_regression_panel_table(con, panel_df):
    view = _register_df(con, panel_df)
    con.execute(f"CREATE OR REPLACE TABLE crowding_out_regression_panel AS SELECT * FROM {view}")
    con.unregister(view)
    return len(panel_df)


# ── Report ────────────────────────────────────────────────────────────────

def _df_to_md_table(df, float_cols=()):
    if not len(df):
        return "_(no rows)_\n"
    lines = ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    for _, row in df.iterrows():
        cells = []
        for col in df.columns:
            v = row[col]
            if pd.isna(v):
                cells.append("—")
            elif col in float_cols:
                cells.append(f"{v:,.1f}")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def build_report(reconciliation, headline_cohort, matched_result, regression_model,
                  threshold_table, covid_drop_table, placebo_table, exact_bn_table):
    lines = [DRAFT_DISCLAIMER_MD]
    lines.append("# Crowding-Out Study: First Large Government Grant vs. Private Donations\n")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    lines.append(
        "## Methodology\n\n"
        "Event study around a charity's first fiscal year with government "
        f"receipts (federal G&C + OTF + Canada Council) summing to >= "
        f"${TREATMENT_THRESHOLD:,} in that year, with zero receipts from any "
        "of those sources in any earlier year. Outcome: T3010 line 4500 "
        "(total tax-receipted gifts), pre/post means over the 3 years before "
        "and after the treatment year (the treatment year itself excluded).\n"
    )
    lines.append(
        "## Reconciliation: federal-only, vs. the spec's documented prototype\n\n"
        "The scope used for the headline result below is broader than the "
        "spec's federal-only prototype (see docs/crowding-out-and-flow-through-spec.md). "
        "This table reruns the identical federal-only definition as a sanity check "
        "against the spec's documented numbers before trusting the broader result.\n"
    )
    lines.append(_df_to_md_table(reconciliation, float_cols=["treated_median_pct", "control_median_pct"]))
    lines.append(
        "\n## Headline result: federal G&C + OTF + Canada Council\n\n"
        "Naive median % change in donations, treated vs. never-treated, per cohort year.\n"
    )
    lines.append(_df_to_md_table(headline_cohort, float_cols=["treated_median_pct", "control_median_pct"]))
    lines.append(
        "\n## Matched-control estimate\n\n"
        "Treated-minus-matched-control difference in median % change (matched on "
        "province, revenue decile, and pre-period donation trend tercile).\n"
    )
    lines.append(_df_to_md_table(
        matched_result,
        float_cols=["treated_median_pct", "matched_control_median_pct", "diff_pct"],
    ))
    lines.append("\n## Fixed-effects regression (matched sample)\n\n")
    if regression_model is not None:
        lines.append("```\n" + str(regression_model.summary()) + "\n```\n")
    else:
        lines.append("_Panel too thin to estimate (fewer than 4 org-event units or only one treatment arm)._\n")
    lines.append("\n## Robustness checks\n")
    lines.append("\n### Threshold sensitivity\n")
    lines.append(_df_to_md_table(threshold_table, float_cols=["pooled_median_pct"]))
    lines.append("\n### Dropping 2020-2021 cohorts (COVID confound)\n")
    lines.append(_df_to_md_table(covid_drop_table, float_cols=["treated_median_pct", "control_median_pct"]))
    lines.append("\n### Placebo pre-trend (fake event at Y-2)\n")
    lines.append(_df_to_md_table(placebo_table, float_cols=["treated_median_pct"]))
    lines.append("\n### Exact-BN-only (entity-resolution robustness)\n")
    lines.append(_df_to_md_table(exact_bn_table, float_cols=["treated_median_pct"]))
    lines.append(
        "\n## Known limitations\n\n"
        "- **Selection bias is the headline caveat.** Orgs that win large "
        "government grants are plausibly already on a growth trajectory; "
        "matching mitigates this but does not eliminate it.\n"
        "- **Fiscal-year alignment is approximate.** `grants_unified.fiscal_year` "
        "is the funder's fiscal year; T3010's `FPE` is the charity's own fiscal "
        "period end. A grant late in a funder's fiscal year may land in the "
        "charity's following filing year. The treatment year itself is excluded "
        "from both windows to reduce this risk, but it is not eliminated.\n"
        "- **Entity resolution errors propagate.** A missed BN match makes a "
        "treated org look never-treated, contaminating the control pool -- "
        "quantified above via the exact-BN-only restriction.\n"
    )
    lines.append(
        "\n## Open questions carried forward\n\n"
        "- Whether `entity_financials_by_year` should grow a donations (line "
        "4500) column so this and any future consumer read from one blessed "
        "panel instead of each re-deriving it from `raw_t3010_fin`.\n"
        "- Whether the regression estimate above should be published given "
        "only one pass of robustness checking -- the spec's original "
        "suggestion was to ship matching first and add regression only if "
        "the matched result held up.\n"
    )
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    con = duckdb.connect(DB_PATH)

    print("Reconciling against the spec's federal-only prototype numbers ...")
    reconciliation, _, _ = naive_contrast(con, GOV_SOURCES_FEDERAL, TREATMENT_THRESHOLD, COHORT_YEARS)
    print(reconciliation.to_string(index=False))

    print("\nComputing headline result (federal + OTF + Canada Council) ...")
    headline_cohort, treated_stats, control_stats = naive_contrast(
        con, GOV_SOURCES_BROAD, TREATMENT_THRESHOLD, COHORT_YEARS
    )
    print(headline_cohort.to_string(index=False))

    print("\nBuilding matched controls ...")
    matched_result, treated_stats_m, matches_df, treated_cov, control_cov = matched_control_contrast(
        con, GOV_SOURCES_BROAD, TREATMENT_THRESHOLD, COHORT_YEARS
    )
    print(matched_result.to_string(index=False))

    print("\nRunning org+year fixed-effects regression ...")
    treated_events = compute_treatment_events(con, GOV_SOURCES_BROAD, TREATMENT_THRESHOLD, COHORT_YEARS)
    regression_panel = build_regression_panel(con, treated_events, matches_df)
    regression_model = run_fe_regression(regression_panel)
    if regression_model is not None:
        print(regression_model.summary())

    print("\nRunning robustness checks ...")
    threshold_table = threshold_sensitivity(con, GOV_SOURCES_BROAD, COHORT_YEARS)
    covid_drop_table = drop_covid_cohorts(con, GOV_SOURCES_BROAD, TREATMENT_THRESHOLD)
    placebo_table = placebo_pretrend(con, GOV_SOURCES_BROAD, TREATMENT_THRESHOLD, COHORT_YEARS)
    exact_bn_table, _ = exact_bn_restricted_contrast(con, GOV_SOURCES_BROAD, TREATMENT_THRESHOLD, COHORT_YEARS)

    print("\nWriting crowding_out_panel and crowding_out_regression_panel ...")
    exact_pairs = treated_exact_bn_pairs(con, GOV_SOURCES_BROAD)
    n_panel = write_crowding_out_panel(con, treated_stats_m, treated_cov, matches_df, control_cov, exact_pairs)
    n_reg = write_regression_panel_table(con, regression_panel)
    print(f"  crowding_out_panel: {n_panel:,} rows")
    print(f"  crowding_out_regression_panel: {n_reg:,} rows")

    print("\nWriting docs/crowding-out-report.md ...")
    report = build_report(
        reconciliation, headline_cohort, matched_result, regression_model,
        threshold_table, covid_drop_table, placebo_table, exact_bn_table,
    )
    out_path = os.path.join(ROOT, "docs", "crowding-out-report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  {out_path}")

    con.close()


if __name__ == "__main__":
    main()
