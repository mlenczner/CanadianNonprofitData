"""
Canadian Nonprofit Data — Source Downloader
Downloads the CRA T3010 charity registry extracts (2013-2024) and the
Canada Council for the Arts open grants data into data/, so they can be
linked to grants.csv by analysis/build_entity_graph.py.

T3010 dataset IDs and resource IDs differ every year with no stable
"latest" endpoint, so this discovers them via the CKAN API
(open.canada.ca/data/api/3/action/...) rather than hardcoding URLs.

Run with: python analysis/download_sources.py
"""

import json
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
T3010_DIR = os.path.join(DATA_DIR, "t3010")

CKAN_PACKAGE_SEARCH = "https://open.canada.ca/data/api/3/action/package_search"
CKAN_PACKAGE_SHOW = "https://open.canada.ca/data/api/3/action/package_show"

# 2013-2024: confirmed same file-naming convention (ident_<year>[_updated].csv
# etc.) and covered by CRA's own T3010 Open Data Dictionary. Years before 2013
# use an incompatible legacy layout (no per-schedule files, different field
# names) and are out of scope for this pipeline.
T3010_YEARS = range(2013, 2025)

# CKAN resource "name" (not filename — the "_updated" suffix on filenames is
# applied inconsistently across years) mapped to our local file-kind prefix.
RESOURCE_KIND_BY_NAME = {
    "identification": "identification",
    "qualified donees": "qualified_donees",
    "non-qualified donees": "non_qualified_donees",
    "financial data": "financials",
}

CANADA_COUNCIL_URL = "https://canadacouncil.ca/-/media/Files/CCA/Research/stats-and-stories/data-tables/2024-25/en/Open-Data-2017-2025.csv"


def ckan_get(url):
    # open.canada.ca's WAF rejects urllib's default request signature but
    # accepts curl's, so shell out rather than use urllib.request here
    # (same issue as the CSV downloads below).
    result = subprocess.run(["curl", "-sL", url], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def find_t3010_dataset_ids():
    """One search call returns every 'List of charities' dataset; filter to
    the years we want rather than querying per-year."""
    data = ckan_get(f"{CKAN_PACKAGE_SEARCH}?q=%22list+of+charities%22&rows=100")
    by_year = {}
    for r in data["result"]["results"]:
        title = r["title"].strip()
        for year in T3010_YEARS:
            if title.lower() == f"{year} list of charities":
                by_year[year] = r["id"]
    missing = [y for y in T3010_YEARS if y not in by_year]
    if missing:
        raise RuntimeError(f"Could not find T3010 datasets for years: {missing}")
    return by_year


def get_t3010_resource_urls(dataset_id):
    data = ckan_get(f"{CKAN_PACKAGE_SHOW}?id={dataset_id}")
    urls = {}
    for r in data["result"]["resources"]:
        kind = RESOURCE_KIND_BY_NAME.get(r["name"].strip().lower())
        if kind:
            urls[kind] = r["url"]
    return urls


def download(label, url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  skip (already present): {label} ({os.path.getsize(dest):,} bytes)")
        return

    print(f"  downloading {label} ...")
    # open.canada.ca's WAF rejects urllib's default request signature but
    # accepts curl's, so shell out rather than use urllib.request here.
    subprocess.run(["curl", "-sL", "-o", dest, url], check=True)

    size = os.path.getsize(dest)
    with open(dest, "r", errors="replace") as f:
        head = f.read(300)
    if head.lstrip().lower().startswith(("<html", "<!doctype")):
        raise RuntimeError(f"    {label} looks wrong ({size} bytes): {head!r}")
    print(f"    done: {label} ({size:,} bytes)")


def download_t3010():
    os.makedirs(T3010_DIR, exist_ok=True)
    print("Discovering T3010 dataset IDs for 2013-2024 via CKAN ...")
    dataset_ids = find_t3010_dataset_ids()

    for year in T3010_YEARS:
        print(f"  {year} (dataset {dataset_ids[year]}):")
        resource_urls = get_t3010_resource_urls(dataset_ids[year])
        for kind in ("identification", "qualified_donees", "non_qualified_donees", "financials"):
            url = resource_urls.get(kind)
            if not url:
                print(f"    WARNING: no '{kind}' resource found for {year}, skipping")
                continue
            dest = os.path.join(T3010_DIR, f"{kind}_{year}.csv")
            download(f"t3010/{kind}_{year}.csv", url, dest)


def download_canada_council():
    dest = os.path.join(DATA_DIR, "canada_council_grants.csv")
    download("canada_council_grants.csv", CANADA_COUNCIL_URL, dest)


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    print(f"Downloading source files into {DATA_DIR}/\n")
    download_t3010()
    download_canada_council()
    print("\nAll sources ready.")
