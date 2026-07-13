"""Tests for analysis/classify_l2.py. No network -- a fake Anthropic client is
injected everywhere a real API call would happen. The real-API path (actually
calling client.messages.create against api.anthropic.com) is exercised only
by the pilot run itself, never by this suite."""

import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analysis import classify_l2 as cl2  # noqa: E402

NO_SLEEP = lambda s: None  # noqa: E731 -- retries in tests must not really sleep
SYS_BLOCKS = [{"type": "text", "text": "fake system prompt", "cache_control": {"type": "ephemeral"}}]


def make_response(codes, confidence, quote, rationale, input_tokens=100, output_tokens=30,
                   cache_write_tokens=0, cache_read_tokens=0):
    block = SimpleNamespace(type="tool_use", input={
        "codes": codes, "confidence": confidence, "quote": quote, "rationale": rationale,
    })
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                             cache_creation_input_tokens=cache_write_tokens,
                             cache_read_input_tokens=cache_read_tokens)
    return SimpleNamespace(content=[block], usage=usage)


class FakeMessages:
    def __init__(self, responses_by_text):
        # value is either one response/exception, or a list consumed one-per-call
        # (for scenarios where the same text is retried before it succeeds).
        self.responses_by_text = responses_by_text
        self.calls = []

    def create(self, **kwargs):
        text = kwargs["messages"][0]["content"]
        self.calls.append(text)
        resp = self.responses_by_text[text]
        if isinstance(resp, list):
            resp = resp.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


class FakeClient:
    def __init__(self, responses_by_text):
        self.messages = FakeMessages(responses_by_text)

    @property
    def calls(self):
        return self.messages.calls


def make_db(tmp_path, grant_rows, name="l2_test.duckdb"):
    db_path = str(tmp_path / name)
    con = cl2.open_db(db_path)
    con.executemany(
        "INSERT INTO l2_grant_text_map VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (r["owner_org"], r["ref_number"], r["recipient_legal_name"], r["business_number"],
             r["amount_cad"], r["text"], cl2.text_hash(r["text"]))
            for r in grant_rows
        ],
    )
    return con


def grant(owner_org, ref_number, name, bn, amount, text):
    return {"owner_org": owner_org, "ref_number": ref_number, "recipient_legal_name": name,
            "business_number": bn, "amount_cad": amount, "text": text}


# ── distinct-text dedup ──────────────────────────────────────────────────────

def test_distinct_text_dedup_propagates_to_both_grants(tmp_path):
    text = "Funding to support community food bank operations."
    con = make_db(tmp_path, [
        grant("DeptA", "REF-1", "Org One", "111", 1000.0, text),
        grant("DeptB", "REF-2", "Org Two", "222", 2000.0, text),
    ])
    dt = cl2.fetch_distinct_texts(con)
    assert len(dt) == 1
    assert dt[0][2] == 2  # n_grants_covered

    fake = FakeClient({text: make_response(["AAA00000"], "high", "food bank", "Food bank support")})
    calls = cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, {"AAA00000"}, sleep_fn=NO_SLEEP)
    assert calls == 1
    assert len(fake.calls) == 1

    rows = con.execute("SELECT owner_org, ref_number, codes FROM l2_grant_classifications ORDER BY owner_org").fetchall()
    assert len(rows) == 2
    assert all(r[2] == "AAA00000" for r in rows)


# ── quote enforcement ─────────────────────────────────────────────────────────

def test_quote_enforcement_downgrades_non_substring_quote_to_abstain(tmp_path):
    text = "Supports operations of the local shelter program."
    con = make_db(tmp_path, [grant("D", "R1", "Org", "1", 500.0, text)])
    dt = cl2.fetch_distinct_texts(con)
    fake = FakeClient({text: make_response(["AAA00000"], "high", "this quote is not in the text", "rationale")})
    cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, {"AAA00000"}, sleep_fn=NO_SLEEP)
    row = con.execute("SELECT confidence, codes, flags FROM l2_text_classifications").fetchone()
    assert row[0] == "abstain"
    assert row[1] == ""
    assert "quote_failed" in row[2]


def test_quote_enforcement_is_case_insensitive_and_whitespace_normalized(tmp_path):
    text = "Supports   the   Local  Shelter Program network."
    con = make_db(tmp_path, [grant("D", "R1", "Org", "1", 500.0, text)])
    dt = cl2.fetch_distinct_texts(con)
    fake = FakeClient({text: make_response(["AAA00000"], "high", "the local shelter program", "rationale")})
    cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, {"AAA00000"}, sleep_fn=NO_SLEEP)
    row = con.execute("SELECT confidence, flags FROM l2_text_classifications").fetchone()
    assert row[0] == "high"
    assert row[1] == ""


# ── unknown code ─────────────────────────────────────────────────────────────

def test_unknown_pcs_code_downgrades_to_abstain_with_bad_code_flag(tmp_path):
    text = "Some grant text about a specific program area."
    con = make_db(tmp_path, [grant("D", "R1", "Org", "1", 500.0, text)])
    dt = cl2.fetch_distinct_texts(con)
    fake = FakeClient({text: make_response(["ZZZ99999"], "high", "specific program area", "rationale")})
    cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, {"AAA00000"}, sleep_fn=NO_SLEEP)
    row = con.execute("SELECT confidence, codes, flags FROM l2_text_classifications").fetchone()
    assert row[0] == "abstain"
    assert row[1] == ""
    assert "bad_code" in row[2]


# ── abstain ──────────────────────────────────────────────────────────────────

def test_abstain_response_stored_with_no_codes(tmp_path):
    text = "Not a Project (Mandated or Core Funding)"
    con = make_db(tmp_path, [grant("D", "R1", "Org", "1", 500.0, text)])
    dt = cl2.fetch_distinct_texts(con)
    fake = FakeClient({text: make_response([], "abstain", "", "boilerplate, no substantive content")})
    cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, {"AAA00000"}, sleep_fn=NO_SLEEP)
    row = con.execute("SELECT status, confidence, codes, flags FROM l2_text_classifications").fetchone()
    assert row[0] == "ok"
    assert row[1] == "abstain"
    assert row[2] == ""
    assert row[3] == ""


# ── resume ───────────────────────────────────────────────────────────────────

def test_resume_skips_already_classified_hashes(tmp_path):
    text = "Program funding for youth mentorship activities."
    con = make_db(tmp_path, [grant("D", "R1", "Org", "1", 500.0, text)])
    dt = cl2.fetch_distinct_texts(con)

    fake1 = FakeClient({text: make_response(["AAA00000"], "high", "youth mentorship", "r")})
    calls1 = cl2.run_pipeline(fake1, con, dt, 10, SYS_BLOCKS, {"AAA00000"}, sleep_fn=NO_SLEEP)
    assert calls1 == 1

    fake2 = FakeClient({text: make_response(["AAA00000"], "high", "youth mentorship", "r")})
    calls2 = cl2.run_pipeline(fake2, con, dt, 10, SYS_BLOCKS, {"AAA00000"}, sleep_fn=NO_SLEEP)
    assert calls2 == 0
    assert len(fake2.calls) == 0


# ── --max-calls cap ──────────────────────────────────────────────────────────

def test_max_calls_stops_at_cap(tmp_path):
    texts = [f"Distinct text number {i} about a unique program." for i in range(3)]
    con = make_db(tmp_path, [
        grant(f"D{i}", f"R{i}", f"Org{i}", str(i), 100.0 * (3 - i), t) for i, t in enumerate(texts)
    ])
    dt = cl2.fetch_distinct_texts(con)
    fake = FakeClient({t: make_response(["AAA00000"], "high", "unique program", "r") for t in texts})
    calls = cl2.run_pipeline(fake, con, dt, max_calls=1, system_blocks=SYS_BLOCKS,
                              valid_codes={"AAA00000"}, sleep_fn=NO_SLEEP)
    assert calls == 1
    n_classified = con.execute("SELECT COUNT(*) FROM l2_text_classifications").fetchone()[0]
    assert n_classified == 1


# ── rollup view ──────────────────────────────────────────────────────────────

def test_rollup_view_math_two_grants_two_codes(tmp_path):
    text1 = "Funding for a shelter renovation project."
    text2 = "Funding for a supportive housing case management program."
    con = make_db(tmp_path, [
        grant("D1", "R1", "Test Org", "999", 1000.0, text1),
        grant("D2", "R2", "Test Org", "999", 2000.0, text2),
    ])
    dt = cl2.fetch_distinct_texts(con)
    valid_codes = {"SS070400", "SS070100"}
    fake = FakeClient({
        text1: make_response(["SS070400"], "high", "shelter renovation", "r1"),
        text2: make_response(["SS070100"], "high", "supportive housing case management", "r2"),
    })
    cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, valid_codes, sleep_fn=NO_SLEEP)
    rows = con.execute(
        "SELECT recipient_legal_name, pcs_code, n_grants, total_cad FROM l2_org_rollup ORDER BY pcs_code"
    ).fetchall()
    assert rows == [("Test Org", "SS070100", 1, 2000.0), ("Test Org", "SS070400", 1, 1000.0)]


# ── malformed response ────────────────────────────────────────────────────────

def test_malformed_response_records_error_not_crash(tmp_path):
    text = "A text whose fake response will be malformed."
    con = make_db(tmp_path, [grant("D", "R1", "Org", "1", 10.0, text)])
    dt = cl2.fetch_distinct_texts(con)
    bad_response = SimpleNamespace(content=[], usage=None)  # no tool_use block at all
    fake = FakeClient({text: bad_response})
    calls = cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, {"AAA00000"}, sleep_fn=NO_SLEEP)
    assert calls == 1
    row = con.execute("SELECT status, error_message FROM l2_text_classifications").fetchone()
    assert row[0] == "error"
    assert row[1] is not None


def test_malformed_response_missing_confidence_is_error(tmp_path):
    text = "Another text with an incomplete fake response."
    con = make_db(tmp_path, [grant("D", "R1", "Org", "1", 10.0, text)])
    dt = cl2.fetch_distinct_texts(con)
    block = SimpleNamespace(type="tool_use", input={"codes": ["AAA00000"]})  # confidence missing entirely
    bad_response = SimpleNamespace(content=[block], usage=None)
    fake = FakeClient({text: bad_response})
    cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, {"AAA00000"}, sleep_fn=NO_SLEEP)
    row = con.execute("SELECT status FROM l2_text_classifications").fetchone()
    assert row[0] == "error"


def test_omitted_quote_and_rationale_default_to_empty_not_a_parse_error(tmp_path):
    # A local model was observed, in practice, to omit optional-seeming keys
    # entirely on an abstain response rather than sending empty values --
    # that's parser leniency, not weaker enforcement: an omitted, non-abstain
    # "quote" still fails the mechanical verbatim-substring check and
    # downgrades through the normal quote_failed path, exactly like a wrong
    # quote would, rather than crashing as "malformed".
    text = "Some grant text that should be flagged for a missing quote."
    con = make_db(tmp_path, [grant("D", "R1", "Org", "1", 10.0, text)])
    dt = cl2.fetch_distinct_texts(con)
    block = SimpleNamespace(type="tool_use", input={"codes": ["AAA00000"], "confidence": "high"})
    fake = FakeClient({text: SimpleNamespace(content=[block], usage=None)})
    cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, {"AAA00000"}, sleep_fn=NO_SLEEP)
    row = con.execute("SELECT status, confidence, codes, flags FROM l2_text_classifications").fetchone()
    assert row[0] == "ok"
    assert row[1] == "abstain"
    assert row[2] == ""
    assert "quote_failed" in row[3]


def test_omitted_codes_on_genuine_abstain_is_accepted(tmp_path):
    text = "Not a Project (Mandated or Core Funding)"
    con = make_db(tmp_path, [grant("D", "R1", "Org", "1", 10.0, text)])
    dt = cl2.fetch_distinct_texts(con)
    block = SimpleNamespace(type="tool_use", input={"confidence": "abstain"})  # codes/quote/rationale all omitted
    fake = FakeClient({text: SimpleNamespace(content=[block], usage=None)})
    cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, {"AAA00000"}, sleep_fn=NO_SLEEP)
    row = con.execute("SELECT status, confidence, codes, flags FROM l2_text_classifications").fetchone()
    assert row == ("ok", "abstain", "", "")


def test_retry_recovers_after_a_transient_malformed_response(tmp_path):
    text = "Text that fails once before succeeding."
    con = make_db(tmp_path, [grant("D", "R1", "Org", "1", 10.0, text)])
    dt = cl2.fetch_distinct_texts(con)
    bad = SimpleNamespace(content=[], usage=None)
    good = make_response(["AAA00000"], "high", "fails once before succeeding", "r")
    fake = FakeClient({text: [bad, good]})
    calls = cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, {"AAA00000"}, sleep_fn=NO_SLEEP)
    assert calls == 1
    assert len(fake.calls) == 2  # one retry
    row = con.execute("SELECT status, confidence FROM l2_text_classifications").fetchone()
    assert row == ("ok", "high")


# ── taxonomy hierarchy helpers ────────────────────────────────────────────────

def test_is_ancestor_or_self():
    assert cl2.is_ancestor_or_self("SS070100", "SS070102")
    assert not cl2.is_ancestor_or_self("SS070102", "SS070100")
    assert cl2.is_ancestor_or_self("SS070100", "SS070100")


def test_codes_related_checks_both_directions():
    assert cl2.codes_related("SS070102", "SS070100")
    assert cl2.codes_related("SS070100", "SS070102")
    assert not cl2.codes_related("SS070102", "SS070400")


# ── text helpers ──────────────────────────────────────────────────────────────

def test_normalize_text_collapses_whitespace():
    assert cl2.normalize_text("  a   b\n\tc  ") == "a b c"


def test_is_verbatim_substring_case_and_whitespace_insensitive():
    assert cl2.is_verbatim_substring("Food  Bank", "Support for the food bank network.")
    assert not cl2.is_verbatim_substring("soup kitchen", "Support for the food bank network.")
    assert not cl2.is_verbatim_substring("", "anything")


# ── housing benchmark: ref_number collision safety ───────────────────────────

def test_housing_benchmark_skips_ambiguous_colliding_ref_number(tmp_path):
    # Regression test: ref_number is NOT globally unique in grants.csv (a
    # known, unrelated data-quality issue -- the same ref reused by entirely
    # different grants/departments). An earlier version of
    # compute_housing_agreement joined on ref_number alone and silently
    # picked whichever of the colliding rows came back first, producing
    # dozens of "disagreements" that were really the same unrelated grant's
    # classification repeated under different benchmark org names.
    colliding_ref = "GC-COLLIDE-0001"
    text_a = "Funding for an emergency homeless shelter overnight program."
    text_b = "Grant to support a folk arts festival celebrating regional traditions."
    con = make_db(tmp_path, [
        grant("DeptA", colliding_ref, "Shelter Org", "1", 1000.0, text_a),
        grant("DeptB", colliding_ref, "Arts Org", "2", 2000.0, text_b),
    ])
    dt = cl2.fetch_distinct_texts(con)
    fake = FakeClient({
        text_a: make_response(["SS070400"], "high", "emergency homeless shelter", "shelter"),
        text_b: make_response(["SA020000"], "high", "folk arts festival", "arts"),
    })
    cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, {"SS070400", "SA020000"}, sleep_fn=NO_SLEEP)

    housing_rows = [{"category": "emergency_shelter", "receipt_ref_number": colliding_ref,
                      "recipient_legal_name": "Shelter Org"}]
    bench = cl2.compute_housing_agreement(con, housing_rows)
    assert bench["matched"] == 0
    assert bench["skipped_ambiguous"] == 1
    assert bench["agreements"] == []
    assert bench["disagreements"] == []


def test_housing_benchmark_matches_non_colliding_ref_number(tmp_path):
    ref = "GC-UNIQUE-0001"
    text = "Funding for an emergency homeless shelter overnight program."
    con = make_db(tmp_path, [grant("DeptA", ref, "Shelter Org", "1", 1000.0, text)])
    dt = cl2.fetch_distinct_texts(con)
    fake = FakeClient({text: make_response(["SS070400"], "high", "emergency homeless shelter", "shelter")})
    cl2.run_pipeline(fake, con, dt, 10, SYS_BLOCKS, {"SS070400"}, sleep_fn=NO_SLEEP)

    housing_rows = [{"category": "emergency_shelter", "receipt_ref_number": ref,
                      "recipient_legal_name": "Shelter Org"}]
    bench = cl2.compute_housing_agreement(con, housing_rows)
    assert bench["matched"] == 1
    assert bench["skipped_ambiguous"] == 0
    assert len(bench["agreements"]) == 1


# ── real-taxonomy-file integration (skipped if the file isn't present) ───────

@pytest.mark.skipif(not os.path.exists(cl2.TAXONOMY_XLSX), reason="taxonomy file not present")
def test_load_real_taxonomy():
    taxonomy = cl2.load_taxonomy()
    assert len(taxonomy) > 500
    codes = {t["code"] for t in taxonomy}
    assert set(cl2.HOUSING_CATEGORY_TO_PCS.values()) <= codes
