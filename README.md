# Canadian Nonprofit Data

Analysis of the Government of Canada's **Proactive Disclosure — Grants and Contributions** dataset: compliance gaps, data quality issues, and spending patterns across federal departments.

**Dataset:** [Grants and Contributions](https://open.canada.ca/data/en/dataset/432527ab-7aac-45b5-81d6-7597107a7013)  
**Search portal:** [search.open.canada.ca/grants](https://search.open.canada.ca/grants/)

---

## Repository structure

```
docs/       Documentation — data problems and research questions
analysis/   Scripts for profiling and analyzing the dataset
```

| Path | Description |
|------|-------------|
| [`docs/data-publishing-problems.md`](docs/data-publishing-problems.md) | Documented issues with missing mandatory fields, inconsistent data, and structural publishing limitations |
| [`docs/questions-and-insights.md`](docs/questions-and-insights.md) | Working list of research questions and possible insights |
| [`analysis/profile_grants.py`](analysis/profile_grants.py) | Dataset profiler — completeness, field distributions, and quality metrics |

---

## Data

Download the full CSV from [Open Canada](https://open.canada.ca/data/en/dataset/432527ab-7aac-45b5-81d6-7597107a7013) and place it at the repo root as `grants.csv`.

The dataset is not tracked in this repository — at ~2 GB it exceeds GitHub's file size limits.

---

## Usage

Profile the dataset (requires Python 3):

```bash
python analysis/profile_grants.py grants.csv
```

The script outputs a compact JSON profile covering field completeness, top values, date ranges, and data quality metrics.
