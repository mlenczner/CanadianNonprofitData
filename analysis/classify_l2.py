"""
Level 2 Service Classification ("what does every org do")

Classifies distinct federal grant description texts with Candid PCS Subject
codes via the Anthropic API, with per-classification receipts, explicit
abstention, resumability, and a hard cost cap. See docs/l2-classification-spec.md
for the full spec (including a "Decisions" note at the bottom for judgment
calls this file had to make).

The cost-shape insight this is built around: federal grant descriptions are
heavily templated, so classifying 1.17M grant rows would mean classifying
the same handful of boilerplate strings tens of thousands of times. Instead:
dedupe latest-amendment grants -> normalize text -> group by exact distinct
(description_en + prog_name_en) text -> classify each distinct text ONCE,
in descending order of grants covered -> propagate to every grant sharing it.

Run with:
    python analysis/classify_l2.py --dry-run --max-calls 1100
    python analysis/classify_l2.py --limit 1000 --max-calls 1100   # the pilot
    python analysis/classify_l2.py --report docs/l2-pilot-report.md --max-calls 0

Respects AGENTS.md: reads grants.csv only via DuckDB aggregates.
"""

import argparse
import hashlib
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

import duckdb
import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRANTS_CSV = os.path.join(ROOT, "grants.csv")
TAXONOMY_XLSX = os.path.join(ROOT, "evidence", "PCS_Taxonomy_Definitions_2024.xlsx")
DB_PATH = os.path.join(ROOT, "evidence", "l2_classifications.duckdb")
HOUSING_CSV = os.path.join(ROOT, "evidence", "seed-classifications-housing-canada.csv")

MODEL = "claude-haiku-4-5-20251001"
PROMPT_VERSION = 1
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0

# Published Haiku 4.5 rates, verified against platform.claude.com/docs/en/about-claude/pricing
# and Anthropic's prompt-caching docs on 2026-07-12 -- these are a recorded assumption for
# --dry-run cost estimates, not fetched live. Re-verify before trusting a stale run's numbers.
PRICE_PER_MTOK_INPUT = 1.00
PRICE_PER_MTOK_OUTPUT = 5.00
PRICE_PER_MTOK_CACHE_WRITE_5MIN = 1.25
PRICE_PER_MTOK_CACHE_READ = 0.10
CHARS_PER_TOKEN_ESTIMATE = 4  # rough heuristic; no live tokenizer call in --dry-run
ASSUMED_OUTPUT_TOKENS_PER_CALL = 80

RETRYABLE_EXCEPTIONS = None  # populated lazily in call_with_retry() so tests don't need `anthropic`

# Housing benchmark: evidence/seed-classifications-housing-canada.csv's four category
# values, mapped to Candid PCS Subject codes by looking up the closest-matching term in
# the Subject sheet (see docs/l2-classification-spec.md's Decisions note for the lookup).
HOUSING_CATEGORY_TO_PCS = {
    "housing_first": "SS070102",       # Housing for homeless people
    "supportive_housing": "SS070100",  # Supportive housing
    "emergency_shelter": "SS070400",   # Homeless shelters
    "transitional_housing": "SS070106",  # Transitional living
}


# ── taxonomy ─────────────────────────────────────────────────────────────────

def load_taxonomy(xlsx_path=TAXONOMY_XLSX):
    """Subjects only (v1) -- one row per PCS code, with its full hierarchy path
    (built by tracking which of the 4 level columns is populated per row, since
    deeper rows don't repeat their ancestors' names) and a one-sentence definition."""
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb["Subject"]
    rows = list(ws.iter_rows(values_only=True))
    taxonomy = []
    path_stack = [None, None, None, None]
    for r in rows[1:]:
        code = r[0]
        if not code:
            continue
        levels = [r[2], r[3], r[4], r[5]]
        depth = next((i for i, v in enumerate(levels) if v is not None), None)
        if depth is None:
            continue
        name = str(levels[depth]).strip()
        path_stack[depth] = name
        for i in range(depth + 1, 4):
            path_stack[i] = None
        path = " > ".join(p for p in path_stack[: depth + 1] if p)
        definition = str(r[7] or "").strip()
        taxonomy.append({"code": code, "name": name, "path": path, "definition": definition, "depth": depth})
    return taxonomy


def code_groups(code):
    return (code[:2], code[2:4], code[4:6], code[6:8])


def is_ancestor_or_self(ancestor_code, descendant_code):
    """True if ancestor_code's non-zero group prefix matches descendant_code's
    corresponding groups (PCS codes encode depth via trailing "00" groups)."""
    a, b = code_groups(ancestor_code), code_groups(descendant_code)
    if a[0] != b[0]:
        return False
    for i in (1, 2, 3):
        if a[i] == "00":
            break
        if b[i] != a[i]:
            return False
    return True


def codes_related(code_a, code_b):
    return code_a == code_b or is_ancestor_or_self(code_a, code_b) or is_ancestor_or_self(code_b, code_a)


INSTRUCTIONS = """You are classifying Government of Canada federal grant descriptions using the \
Candid Philanthropy Classification System (PCS) Subject taxonomy listed above.

For the given text, choose the most specific taxonomy level the text actually supports. Do not \
pick a broad code when a more specific child code fits; do not force a specific code the text \
doesn't clearly support.

Rules:
- Return at most 2 PCS codes, most-relevant first.
- If the text is boilerplate, purely administrative, or too vague to support any code with \
reasonable confidence, return confidence "abstain" with an empty codes list. This is a normal, \
expected, first-class outcome, not a failure -- classify only what has a reasonable chance of \
being accurate.
- The "quote" field must be a verbatim substring of the input text -- the exact span that \
justifies the classification. Do not paraphrase or summarize it. For an abstain, quote may be \
an empty string.
- "rationale" must be 15 words or fewer.
"""


def build_system_prompt(taxonomy):
    lines = ["# Candid PCS Subject Taxonomy (v1, Subjects only)\n"]
    for t in taxonomy:
        sentence = t["definition"].split(". ")[0].strip()
        if sentence and not sentence.endswith("."):
            sentence += "."
        lines.append(f"- {t['code']} ({t['path']}): {sentence}")
    lines.append("\n" + INSTRUCTIONS)
    return "\n".join(lines)


# ── text normalization / hashing ─────────────────────────────────────────────

def normalize_text(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_verbatim_substring(quote, text):
    if not quote:
        return False
    norm_quote = re.sub(r"\s+", " ", quote.strip()).lower()
    norm_text = re.sub(r"\s+", " ", text.strip()).lower()
    return bool(norm_quote) and norm_quote in norm_text


# ── DB setup ─────────────────────────────────────────────────────────────────

def open_db(db_path=DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = duckdb.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS l2_text_classifications (
            text_hash VARCHAR, text VARCHAR, n_grants_covered INTEGER,
            codes VARCHAR, confidence VARCHAR, quote VARCHAR, rationale VARCHAR,
            flags VARCHAR, model VARCHAR, prompt_version INTEGER, status VARCHAR,
            error_message VARCHAR, input_tokens INTEGER, output_tokens INTEGER,
            cache_write_tokens INTEGER, cache_read_tokens INTEGER, ts TIMESTAMP,
            PRIMARY KEY (text_hash, prompt_version)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS l2_grant_text_map (
            owner_org VARCHAR, ref_number VARCHAR, recipient_legal_name VARCHAR,
            business_number VARCHAR, amount_cad DOUBLE, text VARCHAR, text_hash VARCHAR
        )
    """)
    _create_views(con)
    return con


def _create_views(con):
    # DuckDB doesn't allow prepared parameters inside CREATE VIEW, so
    # PROMPT_VERSION (a trusted internal int constant, never user input) is
    # interpolated directly rather than passed as a bound parameter.
    con.execute(f"""
        CREATE OR REPLACE VIEW l2_grant_classifications AS
        SELECT m.owner_org, m.ref_number, m.recipient_legal_name, m.business_number, m.amount_cad,
               c.text_hash, c.codes, c.confidence, c.quote, c.rationale, c.status, c.prompt_version
        FROM l2_grant_text_map m
        JOIN l2_text_classifications c ON c.text_hash = m.text_hash AND c.prompt_version = {int(PROMPT_VERSION)}
    """)
    con.execute("""
        CREATE OR REPLACE VIEW l2_org_rollup AS
        WITH exploded AS (
            SELECT g.recipient_legal_name, g.business_number, g.ref_number, g.amount_cad,
                   g.confidence, g.quote,
                   UNNEST(string_split(g.codes, ',')) AS code
            FROM l2_grant_classifications g
            WHERE g.status = 'ok' AND g.codes IS NOT NULL AND g.codes != ''
        )
        SELECT
            recipient_legal_name, business_number, code AS pcs_code,
            COUNT(*) AS n_grants,
            SUM(amount_cad) AS total_cad,
            MAX(CASE confidence WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END) AS best_confidence_rank,
            ANY_VALUE(quote) AS example_quote,
            ANY_VALUE(ref_number) AS example_ref_number
        FROM exploded
        GROUP BY recipient_legal_name, business_number, code
    """)


def build_grant_text_map(con, grants_csv_path=GRANTS_CSV):
    """Latest-amendment dedup (same SQL as raw_grants_latest), then normalize +
    concatenate description_en + prog_name_en into the dedup/classification text.
    Rebuilt fresh each run (CREATE OR REPLACE) so it always matches grants.csv --
    this table has no dependency on prior classification results."""
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _l2_latest AS
        SELECT * FROM read_csv('{grants_csv_path}', all_varchar=true)
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY TRIM(owner_org), TRIM(ref_number)
            ORDER BY COALESCE(TRY_CAST(NULLIF(TRIM(amendment_number), '') AS INTEGER), 0) DESC
        ) = 1
    """)
    con.execute(r"""
        CREATE OR REPLACE TABLE l2_grant_text_map AS
        SELECT owner_org, ref_number, recipient_legal_name, business_number, amount_cad, text,
               sha256(text) AS text_hash
        FROM (
            SELECT
                TRIM(owner_org) AS owner_org,
                TRIM(ref_number) AS ref_number,
                recipient_legal_name,
                recipient_business_number AS business_number,
                TRY_CAST(REPLACE(REPLACE(TRIM(agreement_value), ',', ''), '$', '') AS DOUBLE) AS amount_cad,
                TRIM(REGEXP_REPLACE(COALESCE(description_en, '') || ' ' || COALESCE(prog_name_en, ''), '\s+', ' ', 'g')) AS text
            FROM _l2_latest
        )
        WHERE text != ''
    """)
    _create_views(con)


def fetch_distinct_texts(con, limit=None):
    """(text_hash, text, n_grants_covered), ordered by grants covered descending
    so early spend (under --max-calls) covers the most rows first."""
    query = """
        SELECT text_hash, ANY_VALUE(text) AS text, COUNT(*) AS n_grants
        FROM l2_grant_text_map
        GROUP BY text_hash
        ORDER BY n_grants DESC, text_hash
    """
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return con.execute(query).fetchall()


def fetch_already_classified_hashes(con, prompt_version=PROMPT_VERSION):
    rows = con.execute(
        "SELECT text_hash FROM l2_text_classifications WHERE prompt_version = ? AND status = 'ok'",
        [prompt_version],
    ).fetchall()
    return {r[0] for r in rows}


def store_result(con, text_hash, text, n_grants, prompt_version=PROMPT_VERSION, **fields):
    con.execute(
        "DELETE FROM l2_text_classifications WHERE text_hash = ? AND prompt_version = ?",
        [text_hash, prompt_version],
    )
    con.execute(
        """INSERT INTO l2_text_classifications
           (text_hash, text, n_grants_covered, codes, confidence, quote, rationale, flags,
            model, prompt_version, status, error_message, input_tokens, output_tokens,
            cache_write_tokens, cache_read_tokens, ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            text_hash, text[:500], n_grants,
            ",".join(fields.get("codes") or []),
            fields.get("confidence"), fields.get("quote"), fields.get("rationale"),
            ",".join(fields.get("flags") or []),
            fields.get("model", MODEL), prompt_version, fields.get("status", "ok"),
            fields.get("error_message"),
            fields.get("input_tokens"), fields.get("output_tokens"),
            fields.get("cache_write_tokens"), fields.get("cache_read_tokens"),
            datetime.now(timezone.utc),
        ],
    )


# ── classification call ──────────────────────────────────────────────────────

class MalformedResponse(Exception):
    pass


def parse_tool_response(response):
    """Defensive parsing of the tool-use block, even though the SDK's schema
    validation makes a badly-shaped result unlikely -- spec requires it."""
    content = getattr(response, "content", None)
    if not content:
        raise MalformedResponse("empty response content")
    block = content[0]
    if getattr(block, "type", None) != "tool_use":
        raise MalformedResponse(f"expected a tool_use block, got {getattr(block, 'type', None)!r}")
    data = getattr(block, "input", None)
    if not isinstance(data, dict):
        raise MalformedResponse("tool_use input is not a dict")
    codes = data.get("codes")
    confidence = data.get("confidence")
    quote = data.get("quote")
    rationale = data.get("rationale")
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        raise MalformedResponse("codes is not a list of strings")
    if confidence not in ("high", "medium", "abstain"):
        raise MalformedResponse(f"unexpected confidence value {confidence!r}")
    if not isinstance(quote, str) or not isinstance(rationale, str):
        raise MalformedResponse("quote/rationale missing or not strings")
    if confidence == "abstain" and codes:
        raise MalformedResponse("abstain must have an empty codes list")
    if confidence != "abstain" and not codes:
        raise MalformedResponse("non-abstain confidence must have >=1 code")
    usage = getattr(response, "usage", None)
    return {
        "codes": codes, "confidence": confidence, "quote": quote, "rationale": rationale,
        "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
        "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", None) if usage else None,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", None) if usage else None,
    }


TOOL_SCHEMA = {
    "name": "classify",
    "description": "Classify a grant description with Candid PCS subject codes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "codes": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
            "confidence": {"type": "string", "enum": ["high", "medium", "abstain"]},
            "quote": {"type": "string"},
            "rationale": {"type": "string"},
        },
        "required": ["codes", "confidence", "quote", "rationale"],
    },
}


def call_model(client, text, system_blocks, model=MODEL):
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=system_blocks,
        tools=[TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "classify"},
        messages=[{"role": "user", "content": text}],
    )
    return parse_tool_response(response)


def _retryable_exceptions():
    global RETRYABLE_EXCEPTIONS
    if RETRYABLE_EXCEPTIONS is None:
        import anthropic
        RETRYABLE_EXCEPTIONS = (
            anthropic.APIConnectionError, anthropic.RateLimitError,
            anthropic.InternalServerError, anthropic.APITimeoutError,
            anthropic.OverloadedError,
        )
    return RETRYABLE_EXCEPTIONS


def call_with_retry(client, text, system_blocks, model=MODEL, max_retries=MAX_RETRIES, sleep_fn=time.sleep):
    """Retries on transient API errors (rate limit, connection, 5xx) with
    exponential backoff. A malformed response is retried too (could be a
    one-off glitch) but a non-retryable API error (auth, bad request) fails
    immediately without burning the retry budget. `anthropic` is a pinned
    dependency (also needed for the fake client in tests to raise real
    exception instances), so the retryable set is always resolved the same
    way regardless of whether `client` is the real SDK client or a test double."""
    retryable = _retryable_exceptions()
    last_error = None
    for attempt in range(max_retries):
        try:
            return call_model(client, text, system_blocks, model), None
        except MalformedResponse as e:
            last_error = f"malformed response: {e}"
        except retryable as e:
            last_error = str(e)
        except Exception as e:
            return None, str(e)
        if attempt < max_retries - 1:
            sleep_fn(RETRY_BACKOFF_BASE * (2 ** attempt))
    return None, last_error or "max retries exceeded"


# ── pipeline ─────────────────────────────────────────────────────────────────

def classify_one(client, con, text_hash, text, n_grants, system_blocks, valid_codes,
                  model=MODEL, sleep_fn=time.sleep):
    result, error = call_with_retry(client, text, system_blocks, model=model, sleep_fn=sleep_fn)
    if error:
        store_result(con, text_hash, text, n_grants, status="error", error_message=error, model=model)
        return "error"

    flags = []
    codes, confidence, quote = result["codes"], result["confidence"], result["quote"]
    if confidence != "abstain":
        if not is_verbatim_substring(quote, text):
            flags.append("quote_failed")
            codes, confidence = [], "abstain"
        elif any(c not in valid_codes for c in codes):
            flags.append("bad_code")
            codes, confidence = [], "abstain"

    store_result(
        con, text_hash, text, n_grants, status="ok", model=model,
        codes=codes, confidence=confidence, quote=quote, rationale=result["rationale"], flags=flags,
        input_tokens=result["input_tokens"], output_tokens=result["output_tokens"],
        cache_write_tokens=result["cache_write_tokens"], cache_read_tokens=result["cache_read_tokens"],
    )
    return confidence


def run_pipeline(client, con, distinct_texts, max_calls, system_blocks, valid_codes,
                  model=MODEL, sleep_fn=time.sleep):
    already_done = fetch_already_classified_hashes(con)
    calls_made = 0
    for text_hash, text, n_grants in distinct_texts:
        if text_hash in already_done:
            continue
        if calls_made >= max_calls:
            break
        classify_one(client, con, text_hash, text, n_grants, system_blocks, valid_codes,
                     model=model, sleep_fn=sleep_fn)
        calls_made += 1
    return calls_made


# ── dry-run / reporting ───────────────────────────────────────────────────────

def estimate_cost(distinct_texts, system_prompt, max_calls):
    planned = min(len(distinct_texts), max_calls)
    system_tokens = len(system_prompt) / CHARS_PER_TOKEN_ESTIMATE
    user_tokens_total = sum(len(t[1]) for t in distinct_texts[:planned]) / CHARS_PER_TOKEN_ESTIMATE
    cache_write_cost = system_tokens * PRICE_PER_MTOK_CACHE_WRITE_5MIN / 1e6 if planned else 0
    cache_read_cost = max(planned - 1, 0) * system_tokens * PRICE_PER_MTOK_CACHE_READ / 1e6
    input_cost = user_tokens_total * PRICE_PER_MTOK_INPUT / 1e6
    output_cost = planned * ASSUMED_OUTPUT_TOKENS_PER_CALL * PRICE_PER_MTOK_OUTPUT / 1e6
    total = cache_write_cost + cache_read_cost + input_cost + output_cost
    return {
        "planned_calls": planned, "system_tokens_estimate": round(system_tokens),
        "cache_write_cost": cache_write_cost, "cache_read_cost": cache_read_cost,
        "input_cost": input_cost, "output_cost": output_cost, "total_cost": total,
    }


def print_dry_run_report(distinct_texts, system_prompt, max_calls, out=sys.stdout):
    total_grants = sum(t[2] for t in distinct_texts)
    est = estimate_cost(distinct_texts, system_prompt, max_calls)
    print("L2 classification -- dry run", file=out)
    print(f"  Distinct texts: {len(distinct_texts):,}", file=out)
    print(f"  Grants covered by those distinct texts: {total_grants:,}", file=out)
    print(f"  Planned calls (min of distinct texts and --max-calls {max_calls}): {est['planned_calls']:,}", file=out)
    print("  Cost assumptions (Haiku 4.5, verified 2026-07-12 via Anthropic pricing docs):", file=out)
    print(f"    input ${PRICE_PER_MTOK_INPUT:.2f}/MTok, output ${PRICE_PER_MTOK_OUTPUT:.2f}/MTok,"
          f" 5-min cache write ${PRICE_PER_MTOK_CACHE_WRITE_5MIN:.2f}/MTok,"
          f" cache read ${PRICE_PER_MTOK_CACHE_READ:.2f}/MTok", file=out)
    print(f"    ~{CHARS_PER_TOKEN_ESTIMATE} chars/token (heuristic, no live tokenizer call),"
          f" ~{ASSUMED_OUTPUT_TOKENS_PER_CALL} output tokens/call (estimate)", file=out)
    print(f"    System prompt (taxonomy + instructions): ~{est['system_tokens_estimate']:,} tokens,"
          f" cached after the first call", file=out)
    print(f"  Estimated cost: ${est['total_cost']:.2f}"
          f" (cache write ${est['cache_write_cost']:.2f} + cache read ${est['cache_read_cost']:.2f}"
          f" + input ${est['input_cost']:.2f} + output ${est['output_cost']:.2f})", file=out)


# ── housing benchmark & pilot report ─────────────────────────────────────────

def load_housing_benchmark(csv_path=HOUSING_CSV):
    import csv as csv_module
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv_module.DictReader(f))


def compute_housing_agreement(con, housing_rows):
    """For benchmark rows whose ref_number the pilot classified (status='ok',
    codes non-empty), compare the LLM's code(s) to the mapped PCS code for
    that row's category. "Agree" = exact match or ancestor/descendant."""
    agreements, disagreements, matched = [], [], 0
    for row in housing_rows:
        mapped_code = HOUSING_CATEGORY_TO_PCS.get(row["category"])
        if not mapped_code:
            continue
        ref = (row.get("receipt_ref_number") or "").strip()
        if not ref:
            continue
        result = con.execute(
            "SELECT codes, confidence, quote FROM l2_grant_classifications "
            "WHERE ref_number = ? AND status = 'ok' AND codes IS NOT NULL AND codes != '' LIMIT 1",
            [ref],
        ).fetchone()
        if not result:
            continue
        matched += 1
        llm_codes = result[0].split(",")
        agree = any(codes_related(c, mapped_code) for c in llm_codes)
        entry = {
            "recipient": row["recipient_legal_name"], "category": row["category"],
            "mapped_code": mapped_code, "llm_codes": llm_codes,
            "confidence": result[1], "quote": result[2], "ref_number": ref,
        }
        (agreements if agree else disagreements).append(entry)
    return {"matched": matched, "agreements": agreements, "disagreements": disagreements}


def write_pilot_report(con, distinct_texts, system_prompt, max_calls, out_path, seed=42):
    rows = con.execute(
        "SELECT status, confidence, flags, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens "
        "FROM l2_text_classifications WHERE prompt_version = ?", [PROMPT_VERSION],
    ).fetchall()
    n = len(rows)
    n_ok = sum(1 for r in rows if r[0] == "ok")
    n_error = sum(1 for r in rows if r[0] == "error")
    n_high = sum(1 for r in rows if r[1] == "high")
    n_medium = sum(1 for r in rows if r[1] == "medium")
    n_abstain = sum(1 for r in rows if r[1] == "abstain")
    n_quote_failed = sum(1 for r in rows if r[2] and "quote_failed" in r[2])
    n_bad_code = sum(1 for r in rows if r[2] and "bad_code" in r[2])

    actual_input = sum(r[3] or 0 for r in rows)
    actual_output = sum(r[4] or 0 for r in rows)
    actual_cache_write = sum(r[5] or 0 for r in rows)
    actual_cache_read = sum(r[6] or 0 for r in rows)
    actual_cost = (
        actual_input * PRICE_PER_MTOK_INPUT
        + actual_output * PRICE_PER_MTOK_OUTPUT
        + actual_cache_write * PRICE_PER_MTOK_CACHE_WRITE_5MIN
        + actual_cache_read * PRICE_PER_MTOK_CACHE_READ
    ) / 1e6
    est = estimate_cost(distinct_texts, system_prompt, max_calls)

    housing_rows = load_housing_benchmark()
    bench = compute_housing_agreement(con, housing_rows)
    n_agree = len(bench["agreements"])
    agreement_pct = (n_agree / bench["matched"] * 100) if bench["matched"] else None

    sample_pool = con.execute(
        "SELECT text, codes, quote, rationale FROM l2_text_classifications "
        "WHERE prompt_version = ? AND status = 'ok' AND confidence = 'high'", [PROMPT_VERSION],
    ).fetchall()
    rng = random.Random(seed)
    sample = rng.sample(sample_pool, min(25, len(sample_pool)))

    lines = []
    lines.append("> **DRAFT — research prototype.** This is an unreleased working draft produced for "
                  "research purposes only. Figures are derived from public data using experimental "
                  "methods, contain known data-quality limitations, and have not been reviewed for "
                  "publication. Do not cite, circulate, or rely on any figure or claim in this document.\n")
    lines.append("# L2 Classification Pilot Report\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

    lines.append("## Dry-run vs. pilot\n")
    lines.append(f"- Distinct texts in scope: {len(distinct_texts):,}")
    lines.append(f"- Dry-run cost estimate for this scope: ${est['total_cost']:.2f}")
    lines.append(f"- Pilot classification rows written: {n:,} (ok: {n_ok:,}, error: {n_error:,})")
    lines.append(f"- Actual cost (from real token usage): ${actual_cost:.2f}"
                 f" ({actual_input:,} input / {actual_output:,} output /"
                 f" {actual_cache_write:,} cache-write / {actual_cache_read:,} cache-read tokens)\n")

    lines.append("## Confidence distribution\n")
    if n_ok:
        lines.append(f"- high: {n_high:,} ({n_high/n_ok:.1%})")
        lines.append(f"- medium: {n_medium:,} ({n_medium/n_ok:.1%})")
        lines.append(f"- abstain: {n_abstain:,} ({n_abstain/n_ok:.1%})")
    lines.append(f"- quote_failed downgrades: {n_quote_failed:,}")
    lines.append(f"- bad_code downgrades: {n_bad_code:,}\n")

    lines.append("## Housing benchmark\n")
    lines.append("Category -> PCS code mapping (looked up in the Subject sheet):\n")
    for cat, code in HOUSING_CATEGORY_TO_PCS.items():
        lines.append(f"- `{cat}` -> `{code}`")
    lines.append("")
    if bench["matched"]:
        lines.append(f"Of {bench['matched']} benchmark grants the pilot classified with a code, "
                     f"{n_agree} agree with the mapped category (match or ancestor/descendant): "
                     f"**{agreement_pct:.1f}%**.\n")
    else:
        lines.append("No benchmark grants (by ref_number) were classified in this pilot scope.\n")
    if bench["disagreements"]:
        lines.append("Disagreements:\n")
        lines.append("| Recipient | CSV category (mapped code) | LLM code(s) | LLM quote |")
        lines.append("|---|---|---|---|")
        for d in bench["disagreements"]:
            lines.append(f"| {d['recipient']} | {d['category']} ({d['mapped_code']}) | "
                         f"{', '.join(d['llm_codes'])} | “{d['quote']}” |")
        lines.append("")

    lines.append("## 25 random high-confidence classifications for review\n")
    lines.append("| Text (truncated) | Code(s) | Quote | Rationale |")
    lines.append("|---|---|---|---|")
    for text, codes, quote, rationale in sample:
        lines.append(f"| {text[:100]} | {codes} | “{quote}” | {rationale} |")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return out_path


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report numbers, classify nothing")
    parser.add_argument("--limit", type=int, default=None, help="classify only the top-N distinct texts by grants covered")
    parser.add_argument("--max-calls", type=int, required=True, help="hard cap on classification calls this run (required)")
    parser.add_argument("--report", metavar="PATH", help="write a pilot report from the current DB state; makes no API calls")
    parser.add_argument("--grants-csv", default=GRANTS_CSV)
    parser.add_argument("--taxonomy", default=TAXONOMY_XLSX)
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args(argv)

    taxonomy = load_taxonomy(args.taxonomy)
    valid_codes = {t["code"] for t in taxonomy}
    system_prompt = build_system_prompt(taxonomy)

    con = open_db(args.db)
    build_grant_text_map(con, args.grants_csv)
    distinct_texts = fetch_distinct_texts(con, limit=args.limit)

    if args.report:
        path = write_pilot_report(con, distinct_texts, system_prompt, args.max_calls, args.report)
        print(f"Wrote {path}")
        return

    if args.dry_run:
        print_dry_run_report(distinct_texts, system_prompt, args.max_calls)
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    system_blocks = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    calls_made = run_pipeline(client, con, distinct_texts, args.max_calls, system_blocks, valid_codes)
    print(f"Made {calls_made} classification calls (prompt_version={PROMPT_VERSION}).")


if __name__ == "__main__":
    main()
