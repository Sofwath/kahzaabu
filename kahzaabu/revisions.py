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
      - image_urls_json: parsed + sorted, so the same set of URLs
        in different order still hashes the same (the press office
        sometimes shuffles image order without editing other content)

    The hash is deterministic across processes / machines / Python
    versions — the only inputs are the canonical text encoding."""
    title = title or ""
    body_text = body_text or ""
    reference = reference or ""
    # Normalise image URLs: parse, sort, re-dump. Order-insensitive.
    images_normalised: list = []
    if image_urls_json:
        try:
            parsed = json.loads(image_urls_json)
            if isinstance(parsed, list):
                images_normalised = sorted(str(x) for x in parsed)
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
    """Insert an article_revisions row capturing the OLD content.

    Returns the inserted revision id. Caller is responsible for
    issuing the subsequent UPDATE/REPLACE on the articles row.

    `old_row` must have the columns we archive (content_hash, title,
    body_text, body_html, image_urls, reference, published_date,
    observed_at — usually mapped from the prior `articles.scraped_at`).
    `new_row` is used only to compute the diff_summary."""
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
            old_row.get("scraped_at") or now,  # when WE first saw the old
            now,                                # when WE noticed the change
            summary,
        ),
    )
    conn.commit()
    return cur.lastrowid


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
