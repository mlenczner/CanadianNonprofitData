"""Corporations Canada federal not-for-profit registry ingest -- the national
discovery source (Phase 3: scale beyond Quebec, per docs/montreal-discovery-
spec.md and AGENTS.md's Quebec Non-Charity Nonprofit Discovery section).
Reads a LOCAL CSV only; this module never downloads anything on its own.

Source: the "Other active corporations" bulk export from Corporations
Canada's Federal Corporations open-data dataset
(https://open.canada.ca/data/en/dataset/0032ce54-c5dd-4b66-99a0-320a7b5e99f2),
downloaded to data/corporations-active-non-cbca-en.csv. That file bundles
not-for-profit corporations (Canada Not-for-profit Corporations Act) together
with cooperatives, boards of trade, and a handful of special-act corporations
in one file -- filtered here to NFP Act only, mirroring REQ's narrow scope
(one legal form, not every category the source file contains).

Confirmed against a real 2026-07-16 snapshot (50,665 total rows, 49,431 NFP
Act after filtering): unlike REQ, which carries no BN at all, 99.0% of NFP
Act rows (48,950) already carry a Business Number. That BN, when present,
lets discovery/run_cc.py skip straight to an exact match against the CRA
registry instead of the postal/FSA/city fuzzy cascade REQ needs for every
record -- see DiscoveryRecord.bn and classify.classify_charity_status_by_bn().

"Corporate name - form 2" is a French/bilingual name variant (not a "trade
name" in REQ's sense), but it slots into DiscoveryRecord.trade_names
unchanged -- discovery/match.py already scores every trade name alongside
the legal name and keeps the best, which is exactly what's wanted here too:
a BN-less English-registered org whose French name matches a CRA charity's
French name should still be found.
"""
import csv

from discovery.config import CC_NFP_ACT_GOVERNING_LEGISLATION
from discovery.ingest.base import DiscoveryRecord
from discovery.normalize import normalize_bn, normalize_postal


def load_cc_records(csv_path, snapshot_date=None):
    """csv_path: path to the downloaded corporations-active-non-cbca-en.csv
    (Corporations Canada's "Other active corporations" bulk export, as
    shipped -- see config.py's CC_ACTIVE_NON_CBCA_FILE)."""
    records = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Governing legislation") != CC_NFP_ACT_GOVERNING_LEGISLATION:
                continue
            legal_name = (row.get("Corporate name - form 1") or "").strip()
            if not legal_name:
                continue
            trade_name = (row.get("Corporate name - form 2") or "").strip()
            jurisdiction = (row.get("Province/territory") or "").strip().upper() or None
            records.append(DiscoveryRecord(
                source_id=row.get("Corporation number"),
                jurisdiction=jurisdiction,
                discovery_source="corporations_canada",
                legal_name=legal_name,
                trade_names=(trade_name,) if trade_name else (),
                address=row.get("Street"),
                postal_code=normalize_postal(row.get("Postal code")),
                city=row.get("City/town"),
                legal_form=CC_NFP_ACT_GOVERNING_LEGISLATION,
                source_snapshot_date=snapshot_date,
                bn=normalize_bn(row.get("Business number (BN)")),
            ))
    return records
