"""
Canadian Nonprofit Data — Source Downloader
Downloads the CRA T3010 charity registry extracts and the Canada Council
for the Arts open grants data into data/, so they can be linked to
grants.csv by analysis/build_entity_graph.py.

Run with: python analysis/download_sources.py
"""

import os
import subprocess

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

T3010_DATASET = "05b3abd0-e70f-4b3b-a9c5-acc436bd15b6"
T3010_BASE = f"https://open.canada.ca/data/dataset/{T3010_DATASET}/resource"

SOURCES = {
    # CRA T3010 charity registry — most recent year (2023) filings.
    "t3010_identification.csv": f"{T3010_BASE}/31a52caf-fa79-4ab3-bded-1ccc7b61c17f/download/ident_2023_updated.csv",
    "t3010_qualified_donees.csv": f"{T3010_BASE}/c603fe1f-cc4c-480e-b1cd-7fd949c42487/download/qualified_donees_2023_updated.csv",
    "t3010_non_qualified_donees.csv": f"{T3010_BASE}/d205c11b-9803-4d3a-8ac8-ab7b648d145b/download/non_qualified_donees_2023_updated.csv",
    "t3010_financials.csv": f"{T3010_BASE}/0b9b4b01-5cb6-4981-b007-ae88f48cc799/download/financial_d_and_schedule_6_2023_updated.csv",
    # Canada Council for the Arts — grant recipients, 2017-18 to 2024-25.
    "canada_council_grants.csv": "https://canadacouncil.ca/-/media/Files/CCA/Research/stats-and-stories/data-tables/2024-25/en/Open-Data-2017-2025.csv",
}


def download(name, url):
    dest = os.path.join(DATA_DIR, name)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  skip (already present): {name} ({os.path.getsize(dest):,} bytes)")
        return

    print(f"  downloading {name} ...")
    # open.canada.ca's WAF rejects urllib's default request signature but
    # accepts curl's, so shell out rather than use urllib.request here.
    subprocess.run(
        ["curl", "-sL", "-o", dest, url],
        check=True,
    )
    size = os.path.getsize(dest)
    if size < 10_000:
        with open(dest, "r", errors="replace") as f:
            head = f.read(300)
        raise RuntimeError(f"    {name} looks wrong ({size} bytes): {head!r}")
    print(f"    done: {name} ({size:,} bytes)")


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    print(f"Downloading source files into {DATA_DIR}/\n")
    for name, url in SOURCES.items():
        download(name, url)
    print("\nAll sources ready.")
