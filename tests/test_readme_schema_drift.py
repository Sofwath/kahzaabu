"""Catch drift between README's data-model documentation and the real DB schema.

The previous bug — README claiming `fact_checks.title` exists when the column
is actually `claim` — only surfaced when the agent invoked a tool that
queried the bogus column. This test catches it at unit-test time instead.

Strategy: for each table named in the README's data-model SQL block,
extract the columns the README mentions and assert every one of them
exists in the real schema. (Extra columns in the schema are OK — they're
omitted from docs for brevity. Columns mentioned in the docs that don't
exist are the failure mode we care about.)

Run:
    .venv/bin/python -m unittest tests.test_readme_schema_drift
"""
from __future__ import annotations

import re
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DB = ROOT / "data" / "kahzaabu.db"


# Tables we expect the README to describe. Update if you remove a table
# from the data-model section.
DOCUMENTED_TABLES = {
    "articles", "claims", "fact_checks", "fact_check_evidence",
    "article_fact_cards", "dv_en_inconsistencies",
    "manifesto_promises", "constitution_articles",
    "qna_sessions", "scrape_runs", "web_users",
}

# Column name aliases — README may use prose-friendly names that differ
# from the actual column. Keys are README terms; values are real columns.
# Empty for now; populate if/when readability divergences appear.
ALIASES: dict[str, dict[str, str]] = {}


def _parse_readme_data_model() -> dict[str, set[str]]:
    """Return {table_name: {column_names mentioned in README cols: lines}}.

    Strict parser: ONLY extracts columns from continuation lines that match
    the `-- cols: a, b, c, ...` pattern. Prose, types, and parenthetical
    notes are ignored — this avoids false positives like 'dv' from
    "EN ↔ DV translation diffs" leaking in as a phantom column name.
    """
    text = README.read_text()
    blocks = re.findall(r"```sql\n(.*?)\n```", text, re.DOTALL)
    data_model_block = None
    for b in blocks:
        if "articles" in b and "fact_checks" in b and "claims" in b:
            data_model_block = b
            break
    if not data_model_block:
        return {}

    result: dict[str, set[str]] = {}
    current_table = None
    in_cols = False
    pending_cols = ""

    def _flush_cols(table: str, raw: str) -> None:
        # Strip parenthetical commentary like `delivery_evidence_json (JSON:
        # linked article_ids + fact_check_ids + notes)` — the parens nest.
        stripped = re.sub(r"\([^)]*\)", "", raw)
        for tok in re.split(r"[,\s]+", stripped):
            tok = tok.strip().rstrip(",.)")
            if re.fullmatch(r"[a-z_][a-z0-9_]*", tok):
                result.setdefault(table, set()).add(tok)

    for raw_line in data_model_block.splitlines():
        # Top-level table line: starts with an identifier (no indent).
        m = re.match(r"^([a-z_][a-z0-9_]*)\s+--", raw_line)
        if m:
            if current_table and pending_cols:
                _flush_cols(current_table, pending_cols)
            current_table = m.group(1)
            result.setdefault(current_table, set())
            pending_cols = ""
            in_cols = False
            continue

        # Continuation line — only care about `cols:` and its continuations.
        stripped = raw_line.strip()
        if not stripped.startswith("--"):
            continue
        body = stripped[2:].strip()

        if body.lower().startswith("cols:"):
            in_cols = True
            pending_cols = body[5:]
        elif in_cols:
            # Heuristic for "still inside the cols: list": the line is a
            # bare comma-separated continuation. If it has a colon or paren-
            # ended sentence, we're past cols.
            if re.match(r"^[a-z0-9_,\s()]+$", body):
                pending_cols += " " + body
            else:
                if current_table and pending_cols:
                    _flush_cols(current_table, pending_cols)
                in_cols = False
                pending_cols = ""

    if current_table and pending_cols:
        _flush_cols(current_table, pending_cols)

    return result


def _real_schema_columns(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Return {table_name: {real column names}} from the live DB."""
    out: dict[str, set[str]] = {}
    for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({r[0]})")}
        out[r[0]] = cols
    return out


class ReadmeSchemaDriftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DB.exists():
            raise unittest.SkipTest(f"DB not present at {DB} — skipping drift check")
        cls.conn = sqlite3.connect(str(DB))
        cls.real = _real_schema_columns(cls.conn)
        cls.readme = _parse_readme_data_model()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_every_documented_table_exists_in_db(self):
        for t in DOCUMENTED_TABLES:
            with self.subTest(table=t):
                self.assertIn(
                    t, self.real,
                    f"README documents table '{t}' but it does not exist in "
                    f"the DB schema. Either add the migration or remove the "
                    f"README entry.",
                )

    def test_parser_found_columns_for_every_documented_table(self):
        """Guard against silent-pass: if someone reformats the data-model
        block and breaks the `cols: a, b, c` parser convention, the column
        existence check would trivially pass (empty set ⊆ anything). This
        invariant forces a real failure in that case."""
        MIN_COLS_PER_TABLE = 3  # every table in the docs has ≥ 3 columns IRL
        for t in DOCUMENTED_TABLES:
            with self.subTest(table=t):
                self.assertGreaterEqual(
                    len(self.readme.get(t, set())), MIN_COLS_PER_TABLE,
                    f"Parser extracted <{MIN_COLS_PER_TABLE} columns for "
                    f"'{t}' from README — likely the docs were reformatted "
                    f"away from the `-- cols: a, b, c` convention the parser "
                    f"expects. Either restore that format or update the "
                    f"parser in this test file.",
                )

    def test_every_column_named_in_readme_exists_in_real_schema(self):
        """The key invariant: if README says column X exists on table Y, it
        must actually exist. Catches the kind of bug we fixed in 43ac29f.
        Extra columns in the real schema not mentioned in README are fine
        (docs intentionally elide noise)."""
        for table, readme_cols in self.readme.items():
            if table not in self.real:
                continue  # already covered by the other test
            real_cols = self.real[table]
            for col in readme_cols:
                resolved = ALIASES.get(table, {}).get(col, col)
                with self.subTest(table=table, column=col):
                    self.assertIn(
                        resolved, real_cols,
                        f"README claims `{table}.{col}` but the real schema "
                        f"has no such column. Real columns: "
                        f"{sorted(real_cols)}",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
