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
    pending_title: list[str] = []   # title lines for the NEXT article
    current_no: Optional[int] = None
    current_title_inline: list[str] = []  # title-column text seen WHILE inside an article
    current_body: list[str] = []

    def _close_current():
        if current_no is not None:
            # The title is pending_title (lines BEFORE the marker) plus any
            # title-column continuation lines that came AFTER the marker on
            # subsequent lines (current_title_inline).
            title_lines = pending_title + current_title_inline
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
            pending_title = []
            chapter = ch.group(1).split(None, 1)[1]  # roman only for now
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
            if 1 <= new_no <= 320:
                # Close previous, start new — pending_title was consumed by
                # _close_current via _make_record. Reset it for this new
                # article: seed with the same-line prefix if present.
                _close_current()
                pending_title = [title_prefix] if title_prefix else []
                current_no = new_no
                current_title_inline = []
                current_body = [body_inline] if body_inline else []
                continue

        # Not an article-start line — classify as body vs title vs blank.
        stripped = raw.strip()
        if not stripped:
            if current_no is not None:
                current_body.append("")
            continue

        # Heuristic: lines that start at left margin (≤4 leading spaces) AND
        # are short-ish are title continuations. Anything else is body.
        leading_spaces = len(raw) - len(raw.lstrip(" "))
        if current_no is None:
            # Pre-first-article — accumulate as title for first article
            pending_title.append(stripped)
            continue

        if leading_spaces <= 4 and len(stripped) <= 50:
            # title continuation for the CURRENT (open) article — captures
            # multi-line titles like "Manner of\nPresidential election"
            current_title_inline.append(stripped)
        else:
            current_body.append(stripped)

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


def init_constitution_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def import_constitution(conn: sqlite3.Connection,
                          txt_path: Path = DEFAULT_TXT) -> int:
    """Idempotent: REPLACE INTO so a re-import refreshes content."""
    from datetime import datetime, timezone
    init_constitution_schema(conn)
    records = parse_constitution(txt_path)
    now = datetime.now(timezone.utc).isoformat()
    for r in records:
        conn.execute(
            "INSERT OR REPLACE INTO constitution_articles "
            "(article_no, chapter, title, body, source_version, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (r["article_no"], r["chapter"], r["title"], r["body"],
             r["source_version"], now),
        )
    conn.commit()
    logger.info("constitution: imported %d articles", len(records))
    return len(records)


def lookup(conn: sqlite3.Connection, query: str, limit: int = 5) -> list[dict]:
    """Cheap LIKE-based search. Returns articles whose title or body matches.

    Ranks rough-relevance by: title match > body match, with title hits
    boosted. Limited to `limit` results.
    """
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
