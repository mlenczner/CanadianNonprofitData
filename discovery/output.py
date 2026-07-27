"""Write the flagged dataset + review queues. Output schema matches the
spec's Output section verbatim (marked there as provisional -- expect it to
shift once real REQ data has been ingested once)."""
import csv
import os

OUTPUT_FIELDS = [
    "source_id", "jurisdiction", "discovery_source", "legal_name", "trade_names",
    "address", "postal", "city", "legal_form",
    "charity_status", "matched_bn", "matched_cra_name",
    "charity_match_score", "charity_runner_up_score", "charity_match_method",
    "social_status", "social_signal", "social_match_score",
    "federal_grant_status", "matched_grant_entity_id", "matched_grant_entity_name",
    "federal_grant_match_score", "federal_grant_runner_up_score",
    "federal_grants_received", "federal_dollars_received",
    "review_flag", "discovery_snapshot_date", "cra_snapshot_date", "grants_snapshot_date",
]


def row_dict(discovery_record, charity_cls, social_cls=None, federal_grant_cls=None,
             cra_snapshot_date=None, grants_snapshot_date=None):
    row = {
        "source_id": discovery_record.source_id,
        "jurisdiction": discovery_record.jurisdiction,
        "discovery_source": discovery_record.discovery_source,
        "legal_name": discovery_record.legal_name,
        "trade_names": "; ".join(discovery_record.trade_names),
        "address": discovery_record.address,
        "postal": discovery_record.postal_code,
        "city": discovery_record.city,
        "legal_form": discovery_record.legal_form,
        "charity_status": charity_cls.charity_status,
        "matched_bn": charity_cls.matched_bn,
        "matched_cra_name": charity_cls.matched_cra_name,
        "charity_match_score": charity_cls.charity_match_score,
        "charity_runner_up_score": charity_cls.charity_runner_up_score,
        "charity_match_method": charity_cls.charity_match_method,
        "social_status": social_cls.social_status if social_cls else None,
        "social_signal": social_cls.social_signal if social_cls else None,
        "social_match_score": social_cls.social_match_score if social_cls else None,
        "federal_grant_status": federal_grant_cls.federal_grant_status if federal_grant_cls else None,
        "matched_grant_entity_id": federal_grant_cls.matched_grant_entity_id if federal_grant_cls else None,
        "matched_grant_entity_name": federal_grant_cls.matched_grant_entity_name if federal_grant_cls else None,
        "federal_grant_match_score": federal_grant_cls.federal_grant_match_score if federal_grant_cls else None,
        "federal_grant_runner_up_score":
            federal_grant_cls.federal_grant_runner_up_score if federal_grant_cls else None,
        "federal_grants_received": federal_grant_cls.federal_grants_received if federal_grant_cls else None,
        "federal_dollars_received": federal_grant_cls.federal_dollars_received if federal_grant_cls else None,
        "review_flag": (
            charity_cls.review_flag
            or (social_cls.review_flag if social_cls else None)
            or (federal_grant_cls.review_flag if federal_grant_cls else None)
        ),
        "discovery_snapshot_date": discovery_record.source_snapshot_date,
        "cra_snapshot_date": cra_snapshot_date,
        "grants_snapshot_date": grants_snapshot_date,
    }
    return row


def write_outputs(rows, out_dir, full_filename="quebec_discovery_flagged.csv",
                   charity_review_filename="charity_needs_review.csv",
                   social_review_filename="social_needs_review.csv",
                   federal_grant_review_filename=None):
    """All filenames are parametrized (not just the full-dataset one) --
    confirmed the hard way: an earlier version defaulted charity_review_
    filename/social_review_filename unconditionally, so running a second
    discovery source (Corporations Canada) silently overwrote the already-
    committed REQ output's charity_needs_review.csv/social_needs_review.csv
    with Corporations Canada's rows. Every caller running a distinct source
    must pass distinct filenames for all three, not just the headline one.

    federal_grant_review_filename defaults to None (that review queue isn't
    written at all) so existing callers that never run the federal-grant-
    match stage (e.g. run_cc.py) don't get an empty, meaningless file."""
    os.makedirs(out_dir, exist_ok=True)
    full_path = os.path.join(out_dir, full_filename)
    charity_review_path = os.path.join(out_dir, charity_review_filename)
    social_review_path = os.path.join(out_dir, social_review_filename)

    with open(full_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        w.writeheader()
        w.writerows(rows)

    with open(charity_review_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        w.writeheader()
        w.writerows(r for r in rows if r["charity_status"] == "needs_review")

    with open(social_review_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        w.writeheader()
        w.writerows(r for r in rows if r["social_status"] == "needs_review")

    federal_grant_review_path = None
    if federal_grant_review_filename:
        federal_grant_review_path = os.path.join(out_dir, federal_grant_review_filename)
        with open(federal_grant_review_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
            w.writeheader()
            w.writerows(r for r in rows if r["federal_grant_status"] == "needs_review")

    return full_path, charity_review_path, social_review_path, federal_grant_review_path
