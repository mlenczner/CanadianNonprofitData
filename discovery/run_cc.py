"""Orchestrate the Corporations Canada discovery pipeline end to end --
Phase 3 ("scale beyond Quebec") from docs/montreal-discovery-spec.md, using a
national federal registry instead of one provincial registry per province.

    .venv/bin/python discovery/run_cc.py --cc-csv data/corporations-active-non-cbca-en.csv

No Corporations Canada data is bundled or downloaded by this repo -- point
--cc-csv at the downloaded "Other active corporations" bulk CSV (see
ingest/cc.py; source: https://open.canada.ca/data/en/dataset/0032ce54-c5dd-
4b66-99a0-320a7b5e99f2). Requires nonprofit_network.duckdb to already exist
(run analysis/build_entity_graph.py first) and to NOT be held open by a
concurrent writer, since ingest/cra.py connects read-only.

Coverage caveat (see AGENTS.md): Corporations Canada only covers federally-
incorporated not-for-profit corporations. Most local/regional Canadian
nonprofits incorporate provincially, not federally -- this is an additive
national source for federally-chartered orgs, not a substitute for a
REQ-style registry in every province.

Exact-BN fast path: unlike REQ (no BN at all), ~99% of Corporations Canada's
NFP Act records carry a Business Number directly. Those are resolved via an
exact match against the CRA registry (classify.classify_charity_status_by_bn())
before the postal/FSA/city fuzzy cascade REQ needs for every record -- see
ingest/cc.py's docstring for the real coverage numbers this was confirmed
against.

--with-federal-grant-match links every record to federal_gc grant totals
the same way: exact-BN first (classify.classify_federal_grant_match_by_bn(),
zero collision risk -- a BN is a hard, unique key), falling back to REQ's
name-prefix fuzzy cascade (discovery/ingest/grant_recipients.py,
discovery/block.py) plus the same collision-reconciliation safety net
(classify.reconcile_grant_match_collisions()) only for the small remainder
with no BN of their own. Candidates are loaded nationwide (no province
filter), unlike REQ's Quebec-only pool, since Corporations Canada spans
every province.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discovery.block import (  # noqa: E402
    candidates_for, build_indexes, build_name_prefix_index, NO_BLOCKING_KEY,
)
from discovery.classify import (  # noqa: E402
    classify_charity_status, classify_charity_status_by_bn, classify_federal_grant_match,
    classify_federal_grant_match_by_bn, reconcile_bn_conflict, reconcile_grant_match_collisions,
    reconcile_low_confidence_grant_matches,
)
from discovery.ingest.cc import load_cc_records  # noqa: E402
from discovery.ingest.cra import load_cra_records  # noqa: E402
from discovery.ingest.grant_recipients import load_federal_grant_recipient_candidates  # noqa: E402
from discovery.match import best_match  # noqa: E402
from discovery.output import row_dict, write_outputs  # noqa: E402


def run(cc_csv, out_dir, with_federal_grant_match=False, db_path=None):
    print("Loading discovery source (Corporations Canada, NFP Act) ...")
    discovery_records = load_cc_records(cc_csv)
    print(f"  {len(discovery_records):,} federally-incorporated not-for-profit corporations")

    print("Loading CRA charity registry lookup (national, no region filter) ...")
    cra_records = load_cra_records(db_path=db_path, region=None)
    print(f"  {len(cra_records):,} charities nationwide")
    cra_by_bn = {r.bn_root: r for r in cra_records}
    postal_idx, fsa_idx, city_idx = build_indexes(cra_records)

    # grant_recipients_by_bn covers BOTH other_org and charity entities --
    # unlike the fuzzy fallback below, the exact-BN path isn't limited to the
    # non-charity set, since a charity's own federal grant total is just as
    # directly answerable via its BN (see classify_federal_grant_match_by_bn()'s
    # docstring). The fuzzy fallback pool is other_org only, national (no
    # province filter, unlike REQ's Quebec-only pool), and only built at all
    # if the flag is passed -- it's a real ~276k-row query, no point paying
    # for it otherwise.
    grant_recipients_by_bn = {}
    grant_fuzzy_pool = []
    grant_postal_idx = grant_fsa_idx = grant_city_idx = grant_name_prefix_idx = None
    if with_federal_grant_match:
        print("Loading federal_gc grant-recipient entities (nationwide, exact-BN + fuzzy fallback pools) ...")
        all_grant_recipients = load_federal_grant_recipient_candidates(
            db_path=db_path, province=None, entity_kinds=("other_org", "charity"),
        )
        grant_recipients_by_bn = {c.bn_root: c for c in all_grant_recipients if c.bn_root}
        print(f"  {len(all_grant_recipients):,} entities with >=1 federal_gc grant "
              f"({len(grant_recipients_by_bn):,} with a BN for exact matching)")
        # Separate query rather than filtering all_grant_recipients in Python,
        # since entity_kind isn't part of GrantRecipientCandidate's shape.
        grant_fuzzy_pool = load_federal_grant_recipient_candidates(
            db_path=db_path, province=None, entity_kinds=("other_org",),
        )
        grant_postal_idx, grant_fsa_idx, grant_city_idx = build_indexes(grant_fuzzy_pool)
        grant_name_prefix_idx = build_name_prefix_index(grant_fuzzy_pool)

    records_and_classifications = []
    block_level_counts = {}
    for rec in discovery_records:
        charity_cls = classify_charity_status_by_bn(rec.bn, cra_by_bn)
        if charity_cls is not None:
            block_level_counts["exact_bn"] = block_level_counts.get("exact_bn", 0) + 1
        else:
            candidates, block_level = candidates_for(rec, cra_records, postal_idx, fsa_idx, city_idx)
            block_level_counts[block_level] = block_level_counts.get(block_level, 0) + 1
            match_result = None if block_level == NO_BLOCKING_KEY else best_match(
                rec, candidates, name_of=lambda c: c.legal_name
            )
            charity_cls = classify_charity_status(block_level, match_result)
            # rec.bn is present but didn't exact-match anything (else we'd have
            # taken the branch above) -- that's direct evidence contradicting
            # an auto-accepted fuzzy match. See reconcile_bn_conflict().
            charity_cls = reconcile_bn_conflict(charity_cls, rec.bn, cra_by_bn)

        federal_grant_cls = None
        if with_federal_grant_match:
            federal_grant_cls = classify_federal_grant_match_by_bn(rec.bn, grant_recipients_by_bn)
            if federal_grant_cls is None and charity_cls.charity_status == "non_charity_nonprofit":
                grant_candidates_in_block, grant_block_level = candidates_for(
                    rec, grant_fuzzy_pool,
                    grant_postal_idx, grant_fsa_idx, grant_city_idx, grant_name_prefix_idx,
                )
                grant_match = None if grant_block_level == NO_BLOCKING_KEY else best_match(
                    rec, grant_candidates_in_block, name_of=lambda c: c.legal_name
                )
                federal_grant_cls = classify_federal_grant_match(grant_block_level, grant_match)

        records_and_classifications.append((rec, charity_cls, federal_grant_cls))

    if with_federal_grant_match:
        # Only the fuzzy-fallback matches carry real collision risk (an exact
        # BN match can't collide by construction -- see
        # classify_federal_grant_match_by_bn()'s docstring), but running the
        # whole list through the same reconciliation pass is harmless (a
        # legitimate BN match's Counter tally is always 1) and keeps this
        # mirrored exactly with run.py's REQ pipeline instead of special-casing.
        fg_list = reconcile_grant_match_collisions([fg for _, _, fg in records_and_classifications])
        # CC-specific, NOT applied to REQ's own pipeline: a spot-check of real
        # sub-100 fuzzy matches here found ~50% wrong (this pool is matched
        # nationwide with no province restriction, unlike REQ's), a failure
        # pattern REQ's own post-collision-fix spot-check didn't show. See
        # reconcile_low_confidence_grant_matches()'s docstring for the real
        # examples this was confirmed against.
        fg_list = reconcile_low_confidence_grant_matches(fg_list)
        records_and_classifications = [
            (rec, charity_cls, fg)
            for (rec, charity_cls, _), fg in zip(records_and_classifications, fg_list)
        ]

    rows = [
        row_dict(rec, charity_cls, federal_grant_cls=federal_grant_cls)
        for rec, charity_cls, federal_grant_cls in records_and_classifications
    ]

    # Distinct filenames for every output, not just the full dataset -- run.py
    # (REQ) writes charity_needs_review.csv/social_needs_review.csv with no
    # source prefix; reusing those names here would silently overwrite REQ's
    # already-committed output on the next run of this script. See
    # write_outputs()'s docstring for the concrete incident this guards against.
    full_path, charity_review_path, _social_review_path, federal_grant_review_path = write_outputs(
        rows, out_dir,
        full_filename="corporations_canada_discovery_flagged.csv",
        charity_review_filename="corporations_canada_charity_needs_review.csv",
        social_review_filename="corporations_canada_social_needs_review.csv",
        federal_grant_review_filename=(
            "corporations_canada_federal_grant_needs_review.csv" if with_federal_grant_match else None
        ),
    )

    print("\nMatch method breakdown (exact_bn resolved before any blocking attempt):")
    for level, n in sorted(block_level_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {level}: {n:,}")

    charity_counts = {}
    for r in rows:
        charity_counts[r["charity_status"]] = charity_counts.get(r["charity_status"], 0) + 1
    print("\nCharity status breakdown:")
    for status, n in sorted(charity_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {status}: {n:,}")

    if with_federal_grant_match:
        fg_counts = {}
        for r in rows:
            if r["federal_grant_status"] is not None:
                fg_counts[r["federal_grant_status"]] = fg_counts.get(r["federal_grant_status"], 0) + 1
        print("\nFederal grant match breakdown:")
        for status, n in sorted(fg_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {status}: {n:,}")

    print(f"\nWrote {full_path}")
    print(f"Wrote {charity_review_path}")
    if with_federal_grant_match:
        print(f"Wrote {federal_grant_review_path}")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc-csv", required=True,
                         help="Path to the downloaded corporations-active-non-cbca-en.csv")
    parser.add_argument("--out-dir", default=os.path.join(os.path.dirname(__file__), "output"))
    parser.add_argument("--with-federal-grant-match", action="store_true",
                         help="Also link every record to federal_gc grant totals (exact-BN + fuzzy fallback)")
    args = parser.parse_args()
    run(args.cc_csv, args.out_dir, with_federal_grant_match=args.with_federal_grant_match)


if __name__ == "__main__":
    main()
