# SPDX-License-Identifier: Apache-2.0
"""Article-revision tracking (ADR 0015).

When presidency.gov.mv quietly edits an article we've already scraped
(a "4 → 1" numeric fix, a swapped photo, a softened claim), the
scraper must:

  1. Detect the change via content-hash compare on every fetch.
  2. Preserve the OLD version in article_revisions before the upsert.
  3. Record a brief diff_summary so an operator scanning the
     revisions log can decide whether to re-extract claims.

This module is the helper layer. The scraper calls:

    h = compute_content_hash(title, body_text, reference, image_urls_json)
    if h != stored_hash:
        archive_revision(conn, article_id, language,
                          old_row, new_row, diff_summary=...)
        # ... then UPDATE articles with the new content

compute_content_hash and generate_diff_summary are pure functions —
no DB access, no side effects — so they're trivially testable and
reusable from the CLI (`kahzaabu revisions show <id>`).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


def _strip_query_string(url: str) -> str:
    """Drop everything after '?' in a URL.

    CDNs commonly tack cache-busting tokens onto image URLs
    (?v=1234, ?t=timestamp) that change between scrapes WITHOUT the
    underlying photo changing. Stripping the query string lets the
    hash see those as the same URL, suppressing phantom revisions.

    A real photo SWAP changes the path itself (photo_v1.jpg →
    photo_v2.jpg), which is preserved through this normalisation,
    so genuine image edits still trigger correctly."""
    if not url:
        return ""
    qm = url.find("?")
    return url[:qm] if qm >= 0 else url


def compute_content_hash(
    title: Optional[str],
    body_text: Optional[str],
    reference: Optional[str],
    image_urls_json: Optional[str],
) -> str:
    """Stable SHA-256 over the editable fields.

    Order and normalisation matter — the same logical content must
    always produce the same hash. We normalise:
      - None → empty string (the field was missing, not "the field
        was 'None'")
      - image_urls_json: parsed + sorted + query-strings stripped,
        so the same set of URLs in different order with different
        cache-bust tokens still hashes the same (the press office
        sometimes shuffles photo order without editing content; the
        CDN sometimes appends ?v=... tokens between scrapes)

    The hash is deterministic across processes / machines / Python
    versions — the only inputs are the canonical text encoding."""
    title = title or ""
    body_text = body_text or ""
    reference = reference or ""
    # Normalise image URLs: parse, strip query strings, sort, re-dump.
    # Order-insensitive + query-string-insensitive.
    images_normalised: list = []
    if image_urls_json:
        try:
            parsed = json.loads(image_urls_json)
            if isinstance(parsed, list):
                images_normalised = sorted(
                    _strip_query_string(str(x)) for x in parsed
                )
        except (json.JSONDecodeError, ValueError):
            # Malformed JSON — hash the raw string so a change still
            # registers (don't silently treat as no-images).
            images_normalised = [image_urls_json]

    payload = "\n\n".join([
        f"TITLE: {title}",
        f"BODY: {body_text}",
        f"REF: {reference}",
        f"IMAGES: {json.dumps(images_normalised, separators=(',', ':'))}",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _numbers_in(text: Optional[str]) -> list[str]:
    """Extract numeric tokens. Includes integers, decimals, and
    comma-separated thousands."""
    if not text:
        return []
    return re.findall(r"\d[\d,]*\.?\d*", text)


def _image_count(image_urls_json: Optional[str]) -> int:
    if not image_urls_json:
        return 0
    try:
        parsed = json.loads(image_urls_json)
        return len(parsed) if isinstance(parsed, list) else 0
    except (json.JSONDecodeError, ValueError):
        return 0


def generate_diff_summary(old: dict, new: dict) -> str:
    """Auto-generated digest of what changed between old and new.

    Pure regex/length-based — no LLM. Designed to surface the
    fact-check-relevant cases:

      - NUMERIC SHIFTS — the "4 → 1" case from the original
        motivation. Lists number pairs that appear in old but not
        new (and vice versa).
      - LENGTH DELTAS — body got materially longer/shorter.
      - TITLE CHANGES — flagged separately because they're often
        the most visible edit.
      - IMAGE COUNT — image list shrank/grew.
      - REFERENCE — reference (e.g. press-release number) changed.

    Format is a single-line semicolon-separated digest so the CLI
    list view stays compact. The full bodies are stored on the row
    for `kahzaabu revisions show` to render a real diff if needed."""
    parts: list[str] = []

    if (old.get("title") or "") != (new.get("title") or ""):
        parts.append("title changed")
    if (old.get("reference") or "") != (new.get("reference") or ""):
        parts.append(
            f"reference {old.get('reference') or '∅'} "
            f"→ {new.get('reference') or '∅'}")

    old_nums = set(_numbers_in(old.get("body_text")))
    new_nums = set(_numbers_in(new.get("body_text")))
    removed = old_nums - new_nums
    added = new_nums - old_nums
    if removed or added:
        # Show up to 5 each — keep the summary line readable
        rem_s = ", ".join(sorted(removed)[:5])
        add_s = ", ".join(sorted(added)[:5])
        if rem_s and add_s:
            parts.append(f"numbers: removed {rem_s}; added {add_s}")
        elif rem_s:
            parts.append(f"numbers: removed {rem_s}")
        else:
            parts.append(f"numbers: added {add_s}")

    old_len = len(old.get("body_text") or "")
    new_len = len(new.get("body_text") or "")
    if abs(old_len - new_len) > 20:  # ignore tiny whitespace edits
        delta = new_len - old_len
        sign = "+" if delta > 0 else ""
        parts.append(f"body length {old_len} → {new_len} ({sign}{delta})")

    old_img = _image_count(old.get("image_urls"))
    new_img = _image_count(new.get("image_urls"))
    if old_img != new_img:
        parts.append(f"images: {old_img} → {new_img}")

    return "; ".join(parts) if parts else "(no detectable substantive change)"


def archive_revision(
    conn: sqlite3.Connection,
    article_id: int,
    language: str,
    old_row: dict,
    new_row: dict,
) -> int:
    """Insert an article_revisions row capturing the OLD content,
    THEN flag every fact_check whose source_article_ids includes
    this article with `source_changed_at = now()` (Slice 15B).

    Returns the inserted revision id. Caller is responsible for
    issuing the subsequent UPDATE/REPLACE on the articles row.

    The fact-check flagging is done in the same transaction as the
    revision insert — so an operator running `kahzaabu fact-checks
    stale` immediately after a scrape sees a consistent view (either
    both the revision row and the flags appear, or neither does)."""
    now = datetime.now(timezone.utc).isoformat()
    summary = generate_diff_summary(old_row, new_row)
    cur = conn.execute(
        """INSERT INTO article_revisions (
            article_id, language, content_hash, title, body_text,
            body_html, image_urls, reference, published_date,
            observed_at, replaced_at, diff_summary
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            article_id, language,
            old_row.get("content_hash") or "",
            old_row.get("title") or "",
            old_row.get("body_text"),
            old_row.get("body_html"),
            old_row.get("image_urls"),
            old_row.get("reference"),
            old_row.get("published_date"),
            old_row.get("scraped_at") or now,
            now,
            summary,
        ),
    )
    flag_affected_factchecks(conn, article_id, now)
    conn.commit()
    return cur.lastrowid


def flag_affected_factchecks(
    conn: sqlite3.Connection,
    article_id: int,
    when: str,
) -> int:
    """Set source_changed_at on every fact-check whose
    source_article_ids JSON array contains this article_id.

    Returns the count of fact-checks flagged. source_article_ids is
    stored as JSON (e.g. "[36702, 36742]"); we use LIKE-with-bracket
    matching to avoid false-positive substring hits (36 vs 3677),
    then JSON-parse to confirm. The LIKE prefilter is just to limit
    the rows we have to parse.

    Idempotent semantics: a fact-check already flagged gets its
    timestamp UPDATED to the latest 'when' — this keeps the flag
    pointing at the most recent affecting revision, which is what
    an operator wants for triage."""
    import json as _json
    # LIKE prefilter — bracket characters disambiguate "36" from "36702"
    # when both appear in the JSON. We then parse to confirm membership.
    candidates = conn.execute(
        """SELECT id, source_article_ids
           FROM fact_checks
           WHERE source_article_ids LIKE ?""",
        (f"%{article_id}%",),
    ).fetchall()
    flagged = 0
    for r in candidates:
        fc_id = r[0] if not hasattr(r, "keys") else r["id"]
        raw = r[1] if not hasattr(r, "keys") else r["source_article_ids"]
        try:
            ids = _json.loads(raw or "[]")
        except (TypeError, _json.JSONDecodeError):
            continue
        if article_id not in ids:
            continue
        conn.execute(
            "UPDATE fact_checks SET source_changed_at = ? WHERE id = ?",
            (when, fc_id),
        )
        flagged += 1
    return flagged


def backfill_content_hashes(
    conn: sqlite3.Connection,
    progress_cb=None,
) -> dict:
    """One-shot: compute and store content_hash for every article
    row where it's currently NULL.

    No revision rows are written — the migration's design treats
    NULL as "first observation, can't tell if anything changed".
    This populates the baseline so SUBSEQUENT scrapes can detect
    edits against a known hash.

    Idempotent: re-running only touches rows where content_hash IS
    NULL. Already-hashed rows are skipped. `progress_cb(done, total)`
    is invoked periodically for long-running runs.

    Returns: {"total": int, "updated": int, "skipped": int}."""
    # Count work upfront for progress reporting.
    total = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE content_hash IS NULL"
    ).fetchone()[0]
    if total == 0:
        return {"total": 0, "updated": 0, "skipped": 0}

    updated = 0
    BATCH = 200
    while True:
        rows = conn.execute(
            """SELECT id, language, title, body_text, reference, image_urls
               FROM articles WHERE content_hash IS NULL LIMIT ?""",
            (BATCH,),
        ).fetchall()
        if not rows:
            break
        # Routed through db.set_article_content_hash to preserve the
        # single-writer invariant (see ADR 0015 + the regression test
        # in tests/test_revisions.py::SingleWriterInvariant).
        from kahzaabu import db as _db
        for r in rows:
            id_, lang, title, body, ref, images = r
            h = compute_content_hash(title, body, ref, images)
            _db.set_article_content_hash(conn, id_, lang, h)
            updated += 1
            if progress_cb is not None and updated % 500 == 0:
                progress_cb(updated, total)
        conn.commit()
        # If the batch was short, we're done.
        if len(rows) < BATCH:
            break
    if progress_cb is not None:
        progress_cb(updated, total)
    return {"total": total, "updated": updated, "skipped": 0}


def list_revisions(
    conn: sqlite3.Connection,
    article_id: int,
    language: Optional[str] = None,
) -> list[dict]:
    """Return revisions oldest-first (chronological replay order)."""
    if language:
        rows = conn.execute(
            """SELECT id, article_id, language, content_hash, title,
                      observed_at, replaced_at, diff_summary
               FROM article_revisions
               WHERE article_id = ? AND language = ?
               ORDER BY replaced_at ASC""",
            (article_id, language),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, article_id, language, content_hash, title,
                      observed_at, replaced_at, diff_summary
               FROM article_revisions
               WHERE article_id = ?
               ORDER BY replaced_at ASC""",
            (article_id,),
        ).fetchall()
    return [dict(r) if hasattr(r, "keys") else
            {k: r[i] for i, k in enumerate(
                ["id", "article_id", "language", "content_hash",
                 "title", "observed_at", "replaced_at", "diff_summary"])}
            for r in rows]


def unified_diff_for_revision(
    conn: sqlite3.Connection,
    revision_id: int,
    n_context: int = 3,
) -> Optional[str]:
    """Generate a unified diff (line-by-line, with line numbers) of
    the body_text between the archived revision and the article's
    CURRENT state.

    Returns None if the revision_id doesn't exist. Returns an empty
    string if the bodies are byte-identical (rare — the only way
    that happens is if the revision was archived for non-body
    reasons like a title change).

    Diff format is Python's difflib.unified_diff output, which is the
    standard `diff -u` shape any operator already knows how to read.

    diff_summary on the revision row tells the operator WHAT shifted
    at a digest level ("numbers: removed 4; added 1"); this function
    tells them WHERE in the body — the position-of-change context
    that motivated this helper."""
    import difflib

    rev = get_revision(conn, revision_id)
    if rev is None:
        return None

    cur_row = conn.execute(
        "SELECT body_text FROM articles WHERE id = ? AND language = ?",
        (rev["article_id"], rev["language"]),
    ).fetchone()
    current_body = ""
    if cur_row is not None:
        # sqlite3.Row supports indexing; otherwise tuple
        current_body = (cur_row[0] if not hasattr(cur_row, "keys")
                          else cur_row["body_text"]) or ""

    old_body = rev.get("body_text") or ""
    if old_body == current_body:
        return ""

    diff_lines = difflib.unified_diff(
        old_body.splitlines(),
        current_body.splitlines(),
        fromfile=f"article {rev['article_id']} ({rev['language']}) — "
                 f"revision {revision_id} ({rev['observed_at'][:19]})",
        tofile=f"article {rev['article_id']} ({rev['language']}) — "
               f"current ({rev['replaced_at'][:19]} or later)",
        n=n_context,
        lineterm="",
    )
    return "\n".join(diff_lines)


def get_revision(conn: sqlite3.Connection, revision_id: int) -> Optional[dict]:
    """Return the full revision row (including body_text/body_html)."""
    row = conn.execute(
        "SELECT * FROM article_revisions WHERE id = ?",
        (revision_id,),
    ).fetchone()
    if row is None:
        return None
    if hasattr(row, "keys"):
        return dict(row)
    cols = [d[0] for d in conn.execute(
        "SELECT * FROM article_revisions WHERE id = ? LIMIT 0",
        (revision_id,),
    ).description]
    return {cols[i]: row[i] for i in range(len(cols))}
