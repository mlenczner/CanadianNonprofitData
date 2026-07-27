"""
Read-only ad hoc SQL runner for nonprofit_network.duckdb (and, via
read_csv(), the raw source files alongside it).

Exists so routine "SELECT ... FROM entities WHERE ..." lookups can be
allowlisted by exact command prefix (see .claude/settings.json) without
allowlisting a bare python interpreter -- a wildcard on `python -c` would
let any code run (write files, drop tables, hit the network); this script
can only run a single read-only SELECT/WITH statement against a connection
that is itself opened read_only=True, so even a gap in the statement check
below can't turn into a write.

Usage:
    .venv/bin/python analysis/dbquery.py "SELECT COUNT(*) FROM entities"
    .venv/bin/python analysis/dbquery.py "SELECT DISTINCT recipient_type FROM read_csv('grants.csv', all_varchar=true)"

Not a general-purpose query tool: no CLI flags, no output format options,
no multi-statement scripts. If you need more than a single SELECT, use a
one-off .venv/bin/python -c script instead (that path still exists and
still prompts for approval, deliberately).
"""

import os
import re
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "nonprofit_network.duckdb")

MAX_ROWS = 500

# Coarse denylist, not a SQL parser -- word-boundary matched so it doesn't
# false-positive on identifiers that merely contain one of these as a
# substring (e.g. "UPDATED_AT" doesn't match \bUPDATE\b). Defense in depth
# only: the real guarantee is the read_only=True connection below, which
# makes every one of these fail at the DuckDB layer even if this regex has
# a gap.
FORBIDDEN_RE = re.compile(
    r"\b(ATTACH|DETACH|COPY|EXPORT|IMPORT|PRAGMA|INSTALL|LOAD|CREATE|INSERT|"
    r"UPDATE|DELETE|DROP|ALTER|CALL|SET|VACUUM|CHECKPOINT|BEGIN|COMMIT|"
    r"ROLLBACK|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def main():
    if len(sys.argv) != 2:
        print("usage: dbquery.py \"<SQL SELECT statement>\"", file=sys.stderr)
        sys.exit(2)

    sql = sys.argv[1].strip()
    # Reject stacked statements -- a trailing single semicolon is fine, but
    # anything followed by more non-whitespace is a second statement.
    body = sql[:-1].strip() if sql.endswith(";") else sql
    if ";" in body:
        print("refused: multiple statements are not allowed", file=sys.stderr)
        sys.exit(1)
    if not re.match(r"^(SELECT|WITH)\b", body, re.IGNORECASE):
        print("refused: only a single SELECT/WITH statement is allowed", file=sys.stderr)
        sys.exit(1)
    if FORBIDDEN_RE.search(body):
        print("refused: statement contains a non-read-only keyword", file=sys.stderr)
        sys.exit(1)

    con = duckdb.connect(DB_PATH, read_only=True)
    try:
        cur = con.execute(body)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(MAX_ROWS + 1)
    finally:
        con.close()

    if cols:
        print(" | ".join(cols))
    for row in rows[:MAX_ROWS]:
        print(" | ".join("" if v is None else str(v) for v in row))
    if len(rows) > MAX_ROWS:
        print(f"... truncated at {MAX_ROWS} rows", file=sys.stderr)


if __name__ == "__main__":
    main()
