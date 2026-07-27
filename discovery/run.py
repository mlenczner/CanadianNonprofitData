"""Orchestrate the Quebec discovery pipeline end to end (spec's Build sequence
Phase 1, plus optional --with-social-signal and --with-federal-grant-match
flags for later stages). Started as a Montreal-only pilot -- --region
defaults to "quebec" (the whole province) now that the pilot's been
validated; pass --region montreal to reproduce the original narrower run.

    .venv/bin/python discovery/run.py --req-dir path/to/unzipped/req/export
    .venv/bin/python discovery/run.py --req-dir path/to/unzipped/req/export --with-social-signal --out-dir discovery/output
    .venv/bin/python discovery/run.py --req-dir path/to/unzipped/req/export --with-federal-grant-match
    .venv/bin/python discovery/run.py --req-dir path/to/unzipped/req/export --region montreal

No REQ data is bundled or downloaded by this repo -- point --req-dir at the
directory the REQ open-data ZIP unzips to (must contain Entreprise.csv and
Nom.csv; see ingest/req.py). Requires nonprofit_network.duckdb to already
exist (run analysis/build_entity_graph.py first) and to NOT be held open by
a concurrent writer, since ingest/cra.py, ingest/grants.py, and
ingest/grant_recipients.py all connect read-only.

--with-federal-grant-match matches the non_charity_nonprofit set against
federal grant recipients analysis/build_entity_graph.py already resolved
into `entities` but never linked to a real legal identity -- see
ingest/grant_recipients.py's docstring for why this is genuinely different
from --with-social-signal (that stage only checks whether a *specific
federal program* looks social-themed; this one answers "did this confirmed
Quebec nonprofit receive ANY federal grant, and how much"). No BN safety net
exists here the way Corporations Canada's exact-BN fast path has one --
validate the federal_grant_match bucket against a labeled sample before
trusting it at scale.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discovery.block import (  # noqa: E402
    candidates_for, build_indexes, build_name_prefix_index, NO_BLOCKING_KEY,
)
from discovery.classify import (  # noqa: E402
    classify_charity_status, classify_federal_grant_match, classify_social_purpose,
    reconcile_grant_match_collisions,
)
from discovery.config import QUEBEC_PROVINCE_CODE  # noqa: E402
from discovery.ingest.cra import load_cra_records  # noqa: E402
from discovery.ingest.grant_recipients import load_federal_grant_recipient_candidates  # noqa: E402
from discovery.ingest.grants import load_federal_social_signal_candidates  # noqa: E402
from discovery.ingest.req import load_req_records  # noqa: E402
from discovery.match import best_match  # noqa: E402
from discovery.output import row_dict, write_outputs  # noqa: E402


def run(req_dir, out_dir, with_social_signal=False, with_federal_grant_match=False,
        db_path=None, region="quebec"):
    region_label = "Montreal region" if region == "montreal" else "Quebec"

    print("Loading discovery source (REQ) ...")
    discovery_records = load_req_records(req_dir, region_filter=(region == "montreal"))
    print(f"  {len(discovery_records):,} NPO establishments in {region_label}")

    print("Loading CRA charity registry lookup ...")
    cra_records = load_cra_records(db_path=db_path, region=region)
    print(f"  {len(cra_records):,} charities in {region_label}")
    postal_idx, fsa_idx, city_idx = build_indexes(cra_records)

    social_candidates = []
    social_postal_idx = social_fsa_idx = social_city_idx = None
    if with_social_signal:
        print("Loading federal G&C social-purpose signal ...")
        # REQ only covers Quebec entities, so there's no point matching a
        # discovery record against a grant recipient anywhere else -- filtered
        # in SQL (see ingest/grants.py) rather than after the fact.
        social_candidates = load_federal_social_signal_candidates(db_path=db_path, province=QUEBEC_PROVINCE_CODE)
        print(f"  {len(social_candidates):,} social-program grant rows")
        social_postal_idx, social_fsa_idx, social_city_idx = build_indexes(social_candidates)

    grant_recipient_candidates = []
    grant_postal_idx = grant_fsa_idx = grant_city_idx = grant_name_prefix_idx = None
    if with_federal_grant_match:
        print("Loading federal_gc grant-recipient entities (non-charity, Quebec) ...")
        # Same province restriction as the social-signal stage above, for the
        # same reason -- REQ only covers Quebec entities.
        grant_recipient_candidates = load_federal_grant_recipient_candidates(
            db_path=db_path, province=QUEBEC_PROVINCE_CODE
        )
        print(f"  {len(grant_recipient_candidates):,} other_org entities with >=1 federal_gc grant")
        # Pre-build all four indexes once, same as the social-signal block
        # above -- passing postal_idx=None etc. into candidates_for() would
        # silently rebuild them from scratch on EVERY discovery record
        # instead of once, since candidates_for() treats None as "not
        # supplied, build it now." This pool carries province only, never
        # city/postal (see ingest/grant_recipients.py), so postal_idx/fsa_idx/
        # city_idx all end up empty and only the name-prefix tier ever hits --
        # still built the same way for consistency with candidates_for()'s
        # general contract, and in case a future candidate pool here does
        # carry an address.
        grant_postal_idx, grant_fsa_idx, grant_city_idx = build_indexes(grant_recipient_candidates)
        grant_name_prefix_idx = build_name_prefix_index(grant_recipient_candidates)

    # federal_grant_cls is collected into its own parallel list rather than
    # folded straight into row_dict() per record -- reconcile_grant_match_
    # collisions() below needs to see EVERY record's match before it can tell
    # whether a given grant-recipient entity was independently claimed by
    # more than one discovery record, which is only knowable after the full
    # pass completes.
    records_and_classifications = []
    block_level_counts = {}
    for rec in discovery_records:
        candidates, block_level = candidates_for(rec, cra_records, postal_idx, fsa_idx, city_idx)
        block_level_counts[block_level] = block_level_counts.get(block_level, 0) + 1
        match_result = None if block_level == NO_BLOCKING_KEY else best_match(
            rec, candidates, name_of=lambda c: c.legal_name
        )
        charity_cls = classify_charity_status(block_level, match_result)

        social_cls = None
        if with_social_signal and charity_cls.charity_status == "non_charity_nonprofit":
            # Same postal -> FSA -> city cascade as the charity match above --
            # unblocked, this was a discovery-record x national-candidate
            # cross product that didn't finish in hours at province scale.
            social_candidates_in_block, _ = candidates_for(
                rec, social_candidates, social_postal_idx, social_fsa_idx, social_city_idx
            )
            social_match = best_match(rec, social_candidates_in_block, name_of=lambda c: c.legal_name)
            social_cls = classify_social_purpose(social_match)

        federal_grant_cls = None
        if with_federal_grant_match and charity_cls.charity_status == "non_charity_nonprofit":
            grant_candidates_in_block, grant_block_level = candidates_for(
                rec, grant_recipient_candidates,
                grant_postal_idx, grant_fsa_idx, grant_city_idx, grant_name_prefix_idx,
            )
            grant_match = None if grant_block_level == NO_BLOCKING_KEY else best_match(
                rec, grant_candidates_in_block, name_of=lambda c: c.legal_name
            )
            federal_grant_cls = classify_federal_grant_match(grant_block_level, grant_match)

        records_and_classifications.append((rec, charity_cls, social_cls, federal_grant_cls))

    if with_federal_grant_match:
        reconciled = reconcile_grant_match_collisions([fg for _, _, _, fg in records_and_classifications])
        records_and_classifications = [
            (rec, charity_cls, social_cls, fg)
            for (rec, charity_cls, social_cls, _), fg in zip(records_and_classifications, reconciled)
        ]

    rows = [
        row_dict(rec, charity_cls, social_cls, federal_grant_cls)
        for rec, charity_cls, social_cls, federal_grant_cls in records_and_classifications
    ]

    full_path, charity_review_path, social_review_path, federal_grant_review_path = write_outputs(
        rows, out_dir,
        federal_grant_review_filename="federal_grant_needs_review.csv" if with_federal_grant_match else None,
    )

    print("\nBlocking level breakdown:")
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
        print("\nFederal grant match breakdown (non_charity_nonprofit set only):")
        for status, n in sorted(fg_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {status}: {n:,}")

    print(f"\nWrote {full_path}")
    print(f"Wrote {charity_review_path}")
    if with_social_signal:
        print(f"Wrote {social_review_path}")
    if with_federal_grant_match:
        print(f"Wrote {federal_grant_review_path}")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--req-dir", required=True,
                         help="Directory containing the unzipped REQ open-data export (Entreprise.csv, Nom.csv)")
    parser.add_argument("--out-dir", default=os.path.join(os.path.dirname(__file__), "output"))
    parser.add_argument("--with-social-signal", action="store_true",
                         help="Also run the Phase 2 social-purpose stage (federal G&C signal)")
    parser.add_argument("--with-federal-grant-match", action="store_true",
                         help="Also match the non-charity set against federal_gc grant-recipient entities")
    parser.add_argument("--region", choices=["quebec", "montreal"], default="quebec",
                         help="quebec (default, whole province) or montreal (the original pilot's island-only subset)")
    args = parser.parse_args()
    run(args.req_dir, args.out_dir, with_social_signal=args.with_social_signal,
        with_federal_grant_match=args.with_federal_grant_match, region=args.region)


if __name__ == "__main__":
    main()
