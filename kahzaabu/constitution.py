# SPDX-License-Identifier: Apache-2.0
"""Constitution of the Republic of Maldives — parser + DB import.

Source: data/constitution/ConstitutionOfMaldives.pdf (English functional
translation by Ms. Dheena Hussain, 2008 baseline). Stored in the
`constitution_articles` table for cheap LIKE-based lookups from the
agentic Q&A tool.

This is automated analysis, not legal advice. The legally binding text is
the Dhivehi original; the constitution has been amended since 2008 and
the translation is non-official. Every output that cites an article must
carry that caveat.

Usage:
    from kahzaabu.constitution import import_constitution
    import_constitution(conn)                      # uses default data/...
    import_constitution(conn, txt_path=Path("..."))
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TXT = (Path(__file__).resolve().parents[1]
               / "data" / "constitution" / "ConstitutionOfMaldives.txt")

SOURCE_VERSION = "2008 baseline — Dheena Hussain functional translation"


def parse_constitution(txt_path: Path = DEFAULT_TXT) -> list[dict]:
    """Parse the constitution text (pdftotext -layout output) into article records.

    Returns: list of {article_no, chapter, title, body, source_version}.

    The PDF uses a two-column layout: titles in a narrow left margin,
    article number + body in the wider right column. `pdftotext -layout`
    preserves the columns spatially. An article-start line looks like:

        Manner of               108. The President shall be elected ...
        Presidential election

    Body continuation lines are deeply indented (no number, body column only).
    Title continuation lines are at the title column (no number).
    """
    txt = txt_path.read_text()
    lines = txt.splitlines()
    body_start = _find_body_start(lines)
    body = lines[body_start:]
    logger.info("constitution body starts at line %d of %d", body_start, len(lines))

    # Article-marker regex: a NN. token preceded by optional left-column
    # title text (>=2 spaces between them) and followed by body text.
    # Group 1: title-on-same-line (may be empty)
    # Group 2: article number
    # Group 3: body-on-same-line (may be empty)
    article_re = re.compile(
        r"^\s*(?:(\S.*?)\s{2,})?(\d+)\.\s*(.*)$"
    )

    records: list[dict] = []
    chapter = ""
    # Two separate title buffers:
    #   * current_title: full title for the article we're INSIDE right now.
    #     Seeded by the pre-marker pending_next_title + the on-line prefix
    #     when an NN. marker arrives.
    #   * pending_next_title: title-y lines we see while inside an article.
    #     If an NN. marker arrives soon, this becomes the next article's
    #     title; if instead a clearly-body line arrives first, these were
    #     false positives and get flushed to the current body.
    current_title: list[str] = []
    pending_next_title: list[str] = []
    current_no: Optional[int] = None
    current_title_inline: list[str] = []  # extra title lines within the title-window AFTER the marker
    current_body: list[str] = []
    title_lines_remaining = 0   # window of N lines after NN. that can still be title

    def _close_current():
        if current_no is not None:
            title_lines = current_title + current_title_inline
            records.append(_make_record(current_no, title_lines,
                                         current_body, chapter))

    for raw in body:
        # Chapter heading?
        ch = re.fullmatch(r"\s*(CHAPTER\s+[IVXLC]+)\s*", raw)
        if ch:
            _close_current()
            current_no = None
            current_body = []
            current_title_inline = []
            current_title = []
            pending_next_title = []
            chapter = ch.group(1).split(None, 1)[1]
            continue
        # The chapter title line (UPPERCASE) follows
        if chapter and re.fullmatch(r"\s*[A-Z][A-Z0-9 ,'’/&\-]{3,}\s*", raw) \
                and not chapter.endswith(raw.strip()):
            chapter = f"{chapter} — {raw.strip()}"
            continue

        m = article_re.match(raw)
        # Only accept matches where the number is in the typical article
        # range AND the line isn't a TOC echo (no trailing dots/page-ref).
        if m and not raw.rstrip().endswith(
                tuple(str(i) for i in range(0, 200))):
            title_prefix = (m.group(1) or "").strip()
            new_no = int(m.group(2))
            body_inline = (m.group(3) or "").strip()
            # Reject TOC-style lines (they have `..........` runs of dots)
            if "....." in raw:
                continue
            # Sequential check: articles in the 2008 Constitution go 1, 2,
            # …, 301 strictly increasing. Sub-clauses like `1. citizens of
            # the Maldives` inside article 9's body look identical to my
            # article-number regex — reject them by requiring forward
            # progress. The very first article (current_no is None) and
            # chapter transitions (next article jumps to a higher number)
            # are still accepted.
            #
            # KNOWN LIMITATION: if a future amendment renumbers articles
            # non-monotonically (e.g. inserts art 50A or removes art 60
            # leaving a gap), this guard would skip the renumbered/recovery
            # article. Rare in real constitutions but possible. Verify the
            # parser against any amended text before relying on it.
            is_sequential = current_no is None or new_no > current_no
            if 1 <= new_no <= 320 and is_sequential:
                _close_current()
                current_title = list(pending_next_title)
                if title_prefix:
                    current_title.append(title_prefix)
                pending_next_title = []
                current_no = new_no
                current_title_inline = []
                current_body = [body_inline] if body_inline else []
                title_lines_remaining = 2
                continue
            # Not a real article marker (sub-clause numbering or stale TOC
            # echo) — fall through and treat as body text.

        # Not an article-start line — classify as body vs title vs blank.
        stripped = raw.strip()
        if not stripped:
            if current_no is not None:
                current_body.append("")
            continue

        leading_spaces = len(raw) - len(raw.lstrip(" "))
        if current_no is None:
            # Pre-first-article — accumulate as title for the article #1
            pending_next_title.append(stripped)
            continue

        looks_titlish = (leading_spaces <= 4
                          and len(stripped) <= 50
                          and not stripped.endswith((":", ";", ".", ",", "—", ")"))
                          and not stripped.startswith(("(", "—", "•")))

        if title_lines_remaining > 0 and looks_titlish:
            # Within the title-window after NN. — continuation of the
            # CURRENT article's title.
            current_title_inline.append(stripped)
            title_lines_remaining -= 1
        elif looks_titlish:
            # Past the title window — might be the NEXT article's
            # pre-marker title. Buffer it; cap at 3 to avoid runaway
            # collection. If a body line arrives before NN., flush.
            pending_next_title.append(stripped)
            pending_next_title = pending_next_title[-3:]
        else:
            # Real body line — clear pending_next_title (it was a false
            # positive) and append to current body.
            if pending_next_title and current_no is not None:
                current_body.extend(pending_next_title)
                pending_next_title = []
            current_body.append(stripped)
            title_lines_remaining = 0

    _close_current()

    # Strip trailing blank lines.
    for r in records:
        r["body"] = r["body"].strip()

    # Dedupe by article_no, keeping the FIRST occurrence. The constitution
    # has main articles 1-301 followed by Schedules 1-3, which re-use small
    # numbers (Schedule 1 has its own 1-6 for oaths of office). Main
    # articles come first in the document, so first-wins keeps them.
    seen: set[int] = set()
    unique: list[dict] = []
    for r in records:
        if r["article_no"] in seen:
            continue
        if not r["body"]:
            continue
        seen.add(r["article_no"])
        unique.append(r)

    return unique


def _find_body_start(lines: list[str]) -> int:
    """Skip the TOC. The body begins at the first CHAPTER heading that's
    followed within ~50 lines by an actual `1.` article-number line."""
    chapter_positions = [
        i for i, ln in enumerate(lines)
        if re.fullmatch(r"CHAPTER\s+[IVXLC]+", ln.strip())
    ]
    for pos in chapter_positions:
        for j in range(pos + 1, min(pos + 60, len(lines))):
            if re.fullmatch(r"1\.", lines[j].strip()):
                return pos
    # fallback: skip first 500 lines
    return 500


def _make_record(no: int, title_lines: list[str], body_lines: list[str],
                  chapter: str) -> dict:
    title = " ".join(t.strip() for t in title_lines if t.strip())
    # Clean the body: drop bare page numbers, collapse runs of blank lines.
    cleaned: list[str] = []
    prev_blank = False
    for ln in body_lines:
        s = ln.strip()
        if re.fullmatch(r"\d+", s) or re.fullmatch(r"[ivx]+", s):
            continue
        if not s:
            if not prev_blank:
                cleaned.append("")
                prev_blank = True
            continue
        cleaned.append(s)
        prev_blank = False
    body = "\n".join(cleaned).strip()
    return {
        "article_no": no,
        "chapter": chapter,
        "title": title,
        "body": body,
        "source_version": SOURCE_VERSION,
    }


# ---------------------------------------------------------------------------
# DB import
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS constitution_articles (
    article_no INTEGER PRIMARY KEY,
    chapter TEXT,
    title TEXT,
    body TEXT NOT NULL,
    source_version TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_const_chapter ON constitution_articles(chapter);
"""

# FTS5 schema. Column order is LOAD-BEARING: the bm25() call in lookup()
# weights columns positionally. If you reorder these, update _BM25_WEIGHTS
# below AND the bm25(…) call in lookup().
#   col 0: article_no  (UNINDEXED — not tokenised, not weighted)
#   col 1: title       (weight 10.0 — title hits dominate)
#   col 2: body        (weight 1.0  — body provides fallback evidence)
FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS constitution_articles_fts
USING fts5(article_no UNINDEXED, title, body);
"""
_FTS_COLUMNS = ("article_no", "title", "body")
_BM25_WEIGHTS = (10.0, 1.0)   # title, body — must match FTS_SQL column order
                              # MINUS the leading UNINDEXED column


def init_constitution_schema(conn: sqlite3.Connection) -> bool:
    """Create the main table and (if SQLite has FTS5) the FTS index.
    Returns True iff FTS5 is available."""
    conn.executescript(SCHEMA_SQL)
    has_fts = False
    try:
        conn.executescript(FTS_SQL)
        has_fts = True
    except sqlite3.OperationalError as e:
        logger.info("constitution: FTS5 not available (%s) — will use LIKE fallback", e)
    conn.commit()
    return has_fts


def import_constitution(conn: sqlite3.Connection,
                          txt_path: Path = DEFAULT_TXT) -> int:
    """Idempotent: REPLACE INTO so a re-import refreshes content."""
    from datetime import datetime, timezone
    has_fts = init_constitution_schema(conn)
    records = parse_constitution(txt_path)
    now = datetime.now(timezone.utc).isoformat()
    # Clear FTS so re-imports don't double-insert
    if has_fts:
        try:
            conn.execute("DELETE FROM constitution_articles_fts")
        except sqlite3.OperationalError:
            pass
    for r in records:
        conn.execute(
            "INSERT OR REPLACE INTO constitution_articles "
            "(article_no, chapter, title, body, source_version, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (r["article_no"], r["chapter"], r["title"], r["body"],
             r["source_version"], now),
        )
        if has_fts:
            conn.execute(
                "INSERT INTO constitution_articles_fts "
                "(article_no, title, body) VALUES (?, ?, ?)",
                (r["article_no"], r["title"], r["body"]),
            )
    conn.commit()
    logger.info("constitution: imported %d articles (FTS5: %s)",
                len(records), "yes" if has_fts else "no")
    return len(records)


def lookup(conn: sqlite3.Connection, query: str, limit: int = 5) -> list[dict]:
    """Search the Constitution. Uses FTS5 with BM25 ranking when available
    (returns ~3× better top results, fewer iterations for the agent);
    falls back to LIKE for SQLite builds without FTS5.
    """
    if not query or not query.strip():
        return []

    # Try FTS5 first. The bm25() weights are passed positionally — keep
    # _BM25_WEIGHTS aligned with FTS_SQL's column order.
    weights_sql = ", ".join(str(w) for w in _BM25_WEIGHTS)
    try:
        rows = conn.execute(
            f"""SELECT a.article_no, a.chapter, a.title, a.body,
                       a.source_version
               FROM constitution_articles_fts f
               JOIN constitution_articles a ON a.article_no = f.article_no
               WHERE constitution_articles_fts MATCH ?
               ORDER BY bm25(constitution_articles_fts, {weights_sql})
               LIMIT ?""",
            (_fts_sanitize(query), limit),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]
        # Zero hits via FTS — fall through to LIKE.
    except sqlite3.OperationalError:
        pass

    # LIKE fallback (used when FTS5 missing OR returned 0 results)
    q = f"%{query.lower()}%"
    rows = conn.execute(
        """SELECT article_no, chapter, title, body, source_version,
                  CASE WHEN LOWER(title) LIKE ? THEN 2 ELSE 1 END AS rank
           FROM constitution_articles
           WHERE LOWER(title) LIKE ? OR LOWER(body) LIKE ?
           ORDER BY rank DESC, article_no ASC
           LIMIT ?""",
        (q, q, q, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _fts_sanitize(query: str) -> str:
    """Turn freeform user text into a safe FTS5 MATCH expression.

    FTS5 has special operators (AND, OR, NOT, NEAR, *, ", etc.) — quoting
    each word handles them. Splits on whitespace and wraps each token in
    double quotes so 'NOT' or punctuation can't blow up the query.
    """
    tokens = re.findall(r"[A-Za-z0-9']+", query)
    if not tokens:
        return '""'
    return " ".join(f'"{t}"' for t in tokens)
