"""Discovery-source interface: every jurisdiction adapter (REQ, Corporations
Canada, provincial registries later) yields records in this one shape so
normalize/block/match/classify stay source-agnostic. Adding a province means
writing a new ingest adapter against this shape, not touching the matcher."""
from collections import namedtuple

DiscoveryRecord = namedtuple("DiscoveryRecord", [
    "source_id",           # e.g. REQ's NEQ, Corporations Canada's Corporation number
    "jurisdiction",        # e.g. "QC" -- REQ is always "QC"; Corporations Canada varies per record
    "discovery_source",    # e.g. "req", "corporations_canada"
    "legal_name",
    "trade_names",         # tuple[str, ...] -- may be empty
    "address",
    "postal_code",         # normalized 6-char, no space, upper -- or None
    "city",
    "legal_form",
    "source_snapshot_date",
    "bn",                  # normalized 9-digit BN root, or None -- REQ never carries one; Corporations
                            # Canada does on ~99% of NFP Act rows, enabling an exact-match fast path
                            # (see discovery/classify.py's classify_charity_status_by_bn()) that skips
                            # the postal/FSA/city fuzzy cascade entirely. Defaults to None so existing
                            # keyword-argument construction (REQ, tests) is unaffected.
], defaults=(None,))
