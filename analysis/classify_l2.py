"""
Level 2 Service Classification ("what does every org do")

Classifies distinct federal grant description texts with Candid PCS Subject
codes, with per-classification receipts, explicit abstention, resumability,
and a hard cost cap. Two interchangeable backends: Anthropic's API (the
spec's original design) or a local Ollama model over its OpenAI-compatible
endpoint (used when no ANTHROPIC_API_KEY is available -- see the Decisions
note in docs/l2-classification-spec.md). Quote/code enforcement, dedup,
resumability, and storage are 100% backend-independent: OllamaClient just
mimics the exact minimal shape classify_l2.py already expects from the real
Anthropic client, so none of that logic branches on backend at all.

The cost-shape insight this is built around: federal grant descriptions are
heavily templated, so classifying 1.17M grant rows would mean classifying
the same handful of boilerplate strings tens of thousands of times. Instead:
dedupe latest-amendment grants -> normalize text -> group by exact distinct
(description_en + prog_name_en) text -> classify each distinct text ONCE,
in descending order of grants covered -> propagate to every grant sharing it.

Run with:
    python analysis/classify_l2.py --dry-run --max-calls 1100
    python analysis/classify_l2.py --backend ollama --limit 1000 --max-calls 1100   # the pilot
    python analysis/classify_l2.py --report docs/l2-pilot-report.md --max-calls 0

Respects AGENTS.md: reads grants.csv only via DuckDB aggregates.
"""

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace

import duckdb
import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRANTS_CSV = os.path.join(ROOT, "grants.csv")
TAXONOMY_XLSX = os.path.join(ROOT, "evidence", "PCS_Taxonomy_Definitions_2024.xlsx")
DB_PATH = os.path.join(ROOT, "evidence", "l2_classifications.duckdb")
HOUSING_CSV = os.path.join(ROOT, "evidence", "seed-classifications-housing-canada.csv")

MODEL = "claude-haiku-4-5-20251001"
OLLAMA_BASE_URL = "http://localhost:11434/v1"
OLLAMA_MODEL = "qwen2.5:7b"
OLLAMA_NUM_CTX = 32768  # the taxonomy system prompt is large; Ollama's default num_ctx is far too small
OLLAMA_TIMEOUT = 180

# Bumped from 1 -> 2 for the Ollama-backend pilot: no Anthropic classification
# ever actually completed under version 1 (blocked on a missing API key), but
# resume-skip logic only keys on (text_hash, prompt_version), not model -- so
# a future Anthropic pilot must not silently inherit Ollama's results under
# the same version. Version 1 stays reserved/unused.
PROMPT_VERSION = 2
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


def build_system_prompt(taxonomy, include_definitions=True):
    """include_definitions=False drops the ~1-sentence definition per code,
    keeping only the code + full hierarchy path -- ~75% smaller (61K -> 15.6K
    estimated tokens). Used for the Ollama backend: with no prompt caching,
    a local 7B model re-processes the whole system prompt from scratch on
    every single call, so the full definition-annotated version (economical
    under Anthropic's caching) is impractically slow there. The hierarchy
    path is kept because it's what disambiguates similar-sounding leaf names
    (e.g. multiple different "...services" codes) -- only the prose
    definition is dropped."""
    lines = ["# Candid PCS Subject Taxonomy (v1, Subjects only)\n"]
    for t in taxonomy:
        if include_definitions:
            sentence = t["definition"].split(". ")[0].strip()
            if sentence and not sentence.endswith("."):
                sentence += "."
            lines.append(f"- {t['code']} ({t['path']}): {sentence}")
        else:
            lines.append(f"- {t['code']}: {t['path']}")
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
    """Defensive parsing of the tool-use block. The Anthropic SDK's schema
    validation makes a badly-shaped result unlikely, but a local model over
    Ollama's OpenAI-compatible endpoint is noticeably less strict: qwen2.5:7b
    was observed, on real requests, to sometimes omit "codes"/"quote"/
    "rationale" entirely for an abstain response rather than sending empty
    values. A merely-omitted-but-implicitly-empty key defaults to its
    zero-value here instead of raising -- this is parser leniency, not an
    enforcement change: quote verification and code validation below still
    run in full on whatever value results (an omitted quote defaults to ""
    and then fails is_verbatim_substring() exactly like a wrong quote would,
    downgrading to abstain+quote_failed through the normal path, not a
    special case). A wrong-typed value that IS present still raises."""
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
    if codes is None:
        codes = []
    confidence = data.get("confidence")
    quote = data.get("quote")
    if quote is None:
        quote = ""
    rationale = data.get("rationale")
    if rationale is None:
        rationale = ""
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        raise MalformedResponse("codes is not a list of strings")
    if confidence not in ("high", "medium", "abstain"):
        raise MalformedResponse(f"unexpected confidence value {confidence!r}")
    if not isinstance(quote, str) or not isinstance(rationale, str):
        raise MalformedResponse("quote/rationale present but not strings")
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


class OllamaError(Exception):
    pass


def _ollama_model_exists(base_url, tag, timeout):
    req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return any(m.get("model") == tag or m.get("name") == tag for m in data.get("models", []))


def _ollama_ensure_context_model(base_url, base_model, num_ctx, timeout):
    """Returns a model tag guaranteed to have num_ctx configured, creating it
    via Ollama's native /api/create if needed. Empirically required: Ollama's
    OpenAI-compatible /v1/chat/completions endpoint was observed to silently
    ignore a "num_ctx" passed under "options" (verified via a raw request --
    prompt_tokens came back far lower than the actual prompt length implied,
    consistent with silent truncation to a much smaller default context). A
    derived model with PARAMETER num_ctx baked in, via the native API, is the
    reliable fix. Same base weights either way -- classify_l2.py still
    records the base model name (e.g. "qwen2.5:7b") in the DB, since this
    tag only exists to work around a context-configuration gap, not because
    a different model was actually used."""
    api_base = base_url.rsplit("/v1", 1)[0]
    tag = f"{base_model}-ctx{num_ctx}"
    if _ollama_model_exists(api_base, tag, timeout):
        return tag
    body = json.dumps({"model": tag, "from": base_model, "parameters": {"num_ctx": num_ctx}}).encode("utf-8")
    req = urllib.request.Request(f"{api_base}/api/create", data=body, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for line in resp:
            status = json.loads(line.decode("utf-8"))
            if status.get("error"):
                raise OllamaError(f"failed to create context model {tag!r}: {status['error']}")
    return tag


class OllamaClient:
    """Adapts Ollama's OpenAI-compatible /v1/chat/completions endpoint (local,
    no API key, no cost) to the exact minimal interface classify_l2.py already
    expects from the real Anthropic client: client.messages.create(...) ->
    an object with .content[0].type/.input and .usage. Because the shape
    matches exactly, call_model(), parse_tool_response(), classify_one(), and
    every bit of quote/code enforcement run completely unchanged -- this
    class is the only backend-specific code in the whole pipeline.

    num_ctx defaults high (32768) because the taxonomy system prompt is large
    and Ollama's own default context window is far too small to fit it. See
    _ollama_ensure_context_model()'s docstring for why this goes through a
    derived model tag rather than a per-request "options" override.
    """

    def __init__(self, base_url=OLLAMA_BASE_URL, num_ctx=OLLAMA_NUM_CTX, timeout=OLLAMA_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.messages = self
        self._context_model_cache = {}

    def create(self, model, max_tokens, system, tools, tool_choice, messages):
        wire_model = self._context_model_cache.get(model)
        if wire_model is None:
            wire_model = _ollama_ensure_context_model(self.base_url, model, self.num_ctx, self.timeout)
            self._context_model_cache[model] = wire_model
        system_text = "\n".join(b["text"] for b in system) if isinstance(system, list) else system
        tool = tools[0]
        payload = {
            "model": wire_model,
            "messages": [{"role": "system", "content": system_text}] + list(messages),
            "tools": [{
                "type": "function",
                "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]},
            }],
            "tool_choice": {"type": "function", "function": {"name": tool["name"]}},
            "max_tokens": max_tokens,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise OllamaError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}") from e
        except urllib.error.URLError as e:
            raise OllamaError(f"connection error: {e}") from e
        except TimeoutError as e:
            raise OllamaError(f"timeout: {e}") from e
        return _wrap_openai_response(data)


def _wrap_usage(usage):
    usage = usage or {}
    return SimpleNamespace(
        input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )


def _wrap_openai_response(data):
    """Reshapes an OpenAI-compatible chat-completion response into the same
    .content[0].type/.input / .usage shape parse_tool_response() expects.
    Unlike the Anthropic SDK (which parses tool-use input into a dict for
    you), OpenAI-style tool_calls[].function.arguments is a raw JSON
    string -- genuinely-malformed JSON is a real possibility here, not just
    a defensive-coding nicety, and is passed through as block.input=None so
    parse_tool_response()'s existing isinstance(data, dict) check catches it."""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        return SimpleNamespace(content=[], usage=_wrap_usage(data.get("usage")))
    fn = tool_calls[0].get("function", {})
    try:
        parsed_input = json.loads(fn.get("arguments", ""))
    except (json.JSONDecodeError, TypeError):
        parsed_input = None
    block_type = "tool_use" if isinstance(parsed_input, dict) else "malformed"
    block = SimpleNamespace(type=block_type, input=parsed_input)
    return SimpleNamespace(content=[block], usage=_wrap_usage(data.get("usage")))


def _retryable_exceptions():
    global RETRYABLE_EXCEPTIONS
    if RETRYABLE_EXCEPTIONS is None:
        exceptions = [OllamaError]
        try:
            import anthropic
            exceptions.extend([
                anthropic.APIConnectionError, anthropic.RateLimitError,
                anthropic.InternalServerError, anthropic.APITimeoutError,
                anthropic.OverloadedError,
            ])
        except ImportError:
            pass
        RETRYABLE_EXCEPTIONS = tuple(exceptions)
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


def print_dry_run_report(distinct_texts, system_prompt, max_calls, backend="anthropic", out=sys.stdout):
    total_grants = sum(t[2] for t in distinct_texts)
    planned = min(len(distinct_texts), max_calls)
    print("L2 classification -- dry run", file=out)
    print(f"  Backend: {backend}", file=out)
    print(f"  Distinct texts: {len(distinct_texts):,}", file=out)
    print(f"  Grants covered by those distinct texts: {total_grants:,}", file=out)
    print(f"  Planned calls (min of distinct texts and --max-calls {max_calls}): {planned:,}", file=out)
    if backend == "ollama":
        print(f"  Local model (Ollama, {OLLAMA_MODEL}) -- no API cost. No prompt caching either "
              f"(that's an Anthropic-specific optimization); system prompt re-sent every call.", file=out)
        print(f"    ~{CHARS_PER_TOKEN_ESTIMATE} chars/token (heuristic, no live tokenizer call);"
              f" system prompt ~{len(system_prompt) // CHARS_PER_TOKEN_ESTIMATE:,} tokens"
              f" (num_ctx={OLLAMA_NUM_CTX})", file=out)
        return
    est = estimate_cost(distinct_texts, system_prompt, max_calls)
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
    that row's category. "Agree" = exact match or ancestor/descendant.

    ref_number is NOT globally unique in grants.csv (confirmed elsewhere in
    this project: ~24,851 refs collide across departments -- the same ref
    reused by entirely different grants/recipients/departments). The housing
    benchmark CSV doesn't carry the raw owner_org needed to disambiguate, so
    a ref_number with more than one distinct owner_org in l2_grant_text_map
    is skipped rather than guessing which one the benchmark row actually
    meant -- silently picking one (as an earlier version of this function
    did) produced dozens of "disagreements" that were really all the same
    unrelated grant's classification repeated under different org names."""
    agreements, disagreements, matched, skipped_ambiguous = [], [], 0, 0
    for row in housing_rows:
        mapped_code = HOUSING_CATEGORY_TO_PCS.get(row["category"])
        if not mapped_code:
            continue
        ref = (row.get("receipt_ref_number") or "").strip()
        if not ref:
            continue
        owners = con.execute(
            "SELECT DISTINCT owner_org FROM l2_grant_text_map WHERE ref_number = ?", [ref],
        ).fetchall()
        if len(owners) > 1:
            skipped_ambiguous += 1
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
    return {
        "matched": matched, "agreements": agreements, "disagreements": disagreements,
        "skipped_ambiguous": skipped_ambiguous,
    }


def write_pilot_report(con, distinct_texts, system_prompt, max_calls, out_path, seed=42, backend="anthropic"):
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
    if backend == "ollama":
        cost_line = (f"- Actual cost: $0.00 (local Ollama model, {OLLAMA_MODEL})."
                     f" Token usage: {actual_input:,} input / {actual_output:,} output"
                     f" (no prompt caching -- Anthropic-specific optimization, not applicable here)\n")
    else:
        actual_cost = (
            actual_input * PRICE_PER_MTOK_INPUT
            + actual_output * PRICE_PER_MTOK_OUTPUT
            + actual_cache_write * PRICE_PER_MTOK_CACHE_WRITE_5MIN
            + actual_cache_read * PRICE_PER_MTOK_CACHE_READ
        ) / 1e6
        est = estimate_cost(distinct_texts, system_prompt, max_calls)
        cost_line = (f"- Dry-run cost estimate for this scope: ${est['total_cost']:.2f}\n"
                     f"- Actual cost (from real token usage): ${actual_cost:.2f}"
                     f" ({actual_input:,} input / {actual_output:,} output /"
                     f" {actual_cache_write:,} cache-write / {actual_cache_read:,} cache-read tokens)\n")

    housing_rows = load_housing_benchmark()
    bench = compute_housing_agreement(con, housing_rows)
    n_agree = len(bench["agreements"])
    agreement_pct = (n_agree / bench["matched"] * 100) if bench["matched"] else None
    by_category = {}
    for d in bench["agreements"]:
        c = by_category.setdefault(d["category"], {"agree": 0, "total": 0})
        c["agree"] += 1
        c["total"] += 1
    for d in bench["disagreements"]:
        c = by_category.setdefault(d["category"], {"agree": 0, "total": 0})
        c["total"] += 1

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
    lines.append(f"- Backend: {backend}" + (f" ({OLLAMA_MODEL})" if backend == "ollama" else f" ({MODEL})"))
    lines.append(f"- Distinct texts in scope: {len(distinct_texts):,}")
    lines.append(f"- Pilot classification rows written: {n:,} (ok: {n_ok:,}, error: {n_error:,})")
    lines.append(cost_line)

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
    lines.append(f"({bench['skipped_ambiguous']} benchmark rows skipped: their ref_number is reused by more "
                 f"than one distinct owner_org in grants.csv, and the benchmark CSV doesn't carry the raw "
                 f"owner_org needed to disambiguate which grant it actually meant -- excluded rather than "
                 f"guessed.)\n")
    if bench["matched"]:
        lines.append(f"Of {bench['matched']} benchmark **rows** the pilot classified with a code, "
                     f"{n_agree} agree with the mapped category (match or ancestor/descendant): "
                     f"**{agreement_pct:.1f}%**. Note: the benchmark CSV can tag the same underlying "
                     f"grant under more than one category (168 ref_numbers are tagged both "
                     f"`emergency_shelter` and `supportive_housing`), so this counts benchmark rows, "
                     f"not distinct grants -- see the per-category and disagreement-cluster breakdowns "
                     f"below for what's actually driving the number.\n")
        lines.append("By category:\n")
        lines.append("| Category | Agree | Total | Rate |")
        lines.append("|---|---|---|---|")
        for cat, c in sorted(by_category.items()):
            lines.append(f"| {cat} | {c['agree']} | {c['total']} | {c['agree']/c['total']:.1%} |")
        lines.append("")
    else:
        lines.append("No benchmark grants (by ref_number) were classified in this pilot scope.\n")
    if bench["disagreements"]:
        # Grouped by the underlying (category, LLM codes, quote) combination rather
        # than listed one row per org: a single heavily-templated boilerplate text
        # (expected, per this pipeline's whole cost-shape design) can be shared by
        # dozens of distinct benchmark orgs, and listing each individually would
        # make one root cause look like dozens of independent disagreements.
        groups = {}
        for d in bench["disagreements"]:
            key = (d["category"], d["mapped_code"], tuple(d["llm_codes"]), d["quote"])
            groups.setdefault(key, []).append(d["recipient"])
        lines.append(f"Disagreements ({len(bench['disagreements'])} benchmark rows in "
                     f"{len(groups)} distinct disagreement pattern(s)):\n")
        lines.append("| Count | CSV category (mapped code) | LLM code(s) | LLM quote | Example recipients |")
        lines.append("|---|---|---|---|---|")
        for (category, mapped_code, llm_codes, quote), recipients in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            examples = ", ".join(recipients[:3]) + (f", and {len(recipients) - 3} more" if len(recipients) > 3 else "")
            lines.append(f"| {len(recipients)} | {category} ({mapped_code}) | "
                         f"{', '.join(llm_codes)} | “{quote}” | {examples} |")
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
    parser.add_argument("--backend", choices=["anthropic", "ollama"], default="anthropic",
                         help="anthropic (default, needs ANTHROPIC_API_KEY) or a local Ollama model")
    parser.add_argument("--ollama-base-url", default=OLLAMA_BASE_URL)
    parser.add_argument("--ollama-model", default=OLLAMA_MODEL)
    parser.add_argument("--ollama-num-ctx", type=int, default=OLLAMA_NUM_CTX)
    parser.add_argument("--grants-csv", default=GRANTS_CSV)
    parser.add_argument("--taxonomy", default=TAXONOMY_XLSX)
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args(argv)

    taxonomy = load_taxonomy(args.taxonomy)
    valid_codes = {t["code"] for t in taxonomy}
    system_prompt = build_system_prompt(taxonomy, include_definitions=(args.backend != "ollama"))

    con = open_db(args.db)
    build_grant_text_map(con, args.grants_csv)
    distinct_texts = fetch_distinct_texts(con, limit=args.limit)

    if args.report:
        path = write_pilot_report(con, distinct_texts, system_prompt, args.max_calls, args.report, backend=args.backend)
        print(f"Wrote {path}")
        return

    if args.dry_run:
        print_dry_run_report(distinct_texts, system_prompt, args.max_calls, backend=args.backend)
        return

    if args.backend == "ollama":
        client = OllamaClient(base_url=args.ollama_base_url, num_ctx=args.ollama_num_ctx)
        model = args.ollama_model
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
            sys.exit(1)
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        model = MODEL

    system_blocks = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    calls_made = run_pipeline(client, con, distinct_texts, args.max_calls, system_blocks, valid_codes, model=model)
    print(f"Made {calls_made} classification calls (backend={args.backend}, model={model}, prompt_version={PROMPT_VERSION}).")


if __name__ == "__main__":
    main()
