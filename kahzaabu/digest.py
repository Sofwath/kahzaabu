# SPDX-License-Identifier: Apache-2.0
"""Daily fact-check digest (Slice F).

A short markdown summary of what kahzaabu noticed in the last 24h:

  - newly-scraped articles
  - newly-published fact-checks
  - article revisions (press-office edits) detected
  - fact-checks now flagged source_changed_at (ADR 0015 + Slice B)

Designed to be rendered by `kahzaabu digest` once a day and either
piped to a file or posted by an operator into whatever channel they
prefer (Telegram, Slack, email, RSS).

Architecture: pure-read against the SQLite. No LLM calls, no network.
Cheap enough to run every hour if desired — the cost is one SELECT
per section.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional


def _since(window_hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def render_digest(
    conn: sqlite3.Connection,
    window_hours: int = 24,
) -> str:
    """Build the markdown digest. Returns a string ready to write to
    a file or pipe to a messaging adapter.

    `window_hours=24` is the default; can be set to e.g. 168 for a
    weekly digest, or 720 for monthly (though the transparency-report
    command is a better fit for monthly cadence)."""
    since = _since(window_hours)
    window_label = (
        "last 24 hours" if window_hours == 24
        else f"last {window_hours}h"
    )
    lines: list[str] = [
        f"# Kahzaabu digest — {_today_iso()}",
        "",
        f"_Window: {window_label}._",
        "",
    ]

    # ── 1. Newly-scraped articles ─────────────────────────────────
    new_articles = conn.execute(
        """SELECT id, language, title, published_date, category
           FROM articles
           WHERE scraped_at >= ?
             AND language = 'EN'
             AND published_date >= '2023-11-17'
           ORDER BY published_date DESC, id DESC
           LIMIT 30""",
        (since,),
    ).fetchall()
    lines.append(f"## New articles ({len(new_articles)})")
    lines.append("")
    if not new_articles:
        lines.append("_None scraped in this window._")
    else:
        for r in new_articles[:15]:
            title = (r[2] or "")[:90]
            lines.append(
                f"- {r[3] or '?'}  [{r[0]}]  _{r[4]}_  {title}"
            )
        if len(new_articles) > 15:
            lines.append(f"- _… and {len(new_articles) - 15} more._")
    lines.append("")

    # ── 2. New fact-checks ─────────────────────────────────────────
    new_fcs = conn.execute(
        """SELECT id, category, verdict_label, truth_score_label,
                  claim, topic
           FROM fact_checks
           WHERE created_at >= ? AND published = 1
           ORDER BY created_at DESC""",
        (since,),
    ).fetchall()
    lines.append(f"## New fact-checks ({len(new_fcs)})")
    lines.append("")
    if not new_fcs:
        lines.append("_None published in this window._")
    else:
        for r in new_fcs[:10]:
            verdict = r[2] or r[1] or "?"
            ts = r[3] or ""
            claim = (r[4] or "")[:120]
            lines.append(
                f"- **fc#{r[0]}** _{verdict}_ {ts}  ·  {claim}"
            )
    lines.append("")

    # ── 3. Article revisions (press-office edits) ─────────────────
    revs = conn.execute(
        """SELECT id, article_id, language, replaced_at, diff_summary
           FROM article_revisions
           WHERE replaced_at >= ?
           ORDER BY replaced_at DESC
           LIMIT 30""",
        (since,),
    ).fetchall()
    lines.append(f"## Article edits detected ({len(revs)})")
    lines.append("")
    if not revs:
        lines.append("_No edits to already-scraped articles in this window._")
    else:
        lines.append(
            "Press-office edits to articles already in the archive. "
            "See `kahzaabu revisions show <rev_id>` for details."
        )
        lines.append("")
        for r in revs[:15]:
            lines.append(
                f"- article {r[1]} [{r[2]}]  rev#{r[0]}  "
                f"_{r[3][:19]}_  · {r[4]}"
            )
    lines.append("")

    # ── 4. Stale fact-checks (Slice B) ─────────────────────────────
    stale = conn.execute(
        """SELECT id, source_changed_at, claim, source_article_ids
           FROM fact_checks
           WHERE source_changed_at >= ?
           ORDER BY source_changed_at DESC
           LIMIT 20""",
        (since,),
    ).fetchall()
    lines.append(
        f"## Fact-checks needing review ({len(stale)})"
    )
    lines.append("")
    if not stale:
        lines.append(
            "_None — no source articles have changed for already-"
            "published fact-checks._"
        )
    else:
        lines.append(
            "These fact-checks' source articles were edited since "
            "publication. Use `kahzaabu fact-checks stale` for the "
            "full list and `kahzaabu revisions diff <rev_id>` to see "
            "what changed."
        )
        lines.append("")
        for r in stale[:10]:
            claim = (r[2] or "")[:80]
            lines.append(
                f"- **fc#{r[0]}** source edited {r[1][:19]}  "
                f"(articles: {r[3]}) · {claim}"
            )
    lines.append("")

    # Footer
    lines.append("---")
    lines.append(
        "_Kahzaabu is a reference / educational fact-checking "
        "archive — see [DISCLAIMER.md](DISCLAIMER.md). Generated by "
        "`kahzaabu digest`._"
    )
    return "\n".join(lines)


def write_digest(
    conn: sqlite3.Connection,
    out_path,
    window_hours: int = 24,
) -> str:
    """Render + write to disk. Returns the path written.

    `out_path` can be a Path or a string. Parent directory is
    created if it doesn't exist."""
    from pathlib import Path
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_digest(conn, window_hours=window_hours))
    return str(p)
