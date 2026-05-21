# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 12 — Transparency report (ADR 0010).

Generate a public-facing markdown report for a date window:
  - Fact-checks issued in window, by category + verdict_label
  - Corrections received (from `corrections` table)
  - Methodology updates (git log of docs/METHODOLOGY.md if available)
  - LLM spend total across all *_runs tables in window

CLI: `kahzaabu transparency-report --since YYYY-MM-DD [--until YYYY-MM-DD]`
Output: data/reports/transparency-<since>_to_<until>.md
"""
from __future__ import annotations

import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPORT_DIR = Path(__file__).resolve().parents[1] / "data" / "reports"
REPO_ROOT  = Path(__file__).resolve().parents[1]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _fact_checks_issued(conn: sqlite3.Connection,
                         since: str, until: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT id, category, claim, claim_date, topic, "
        "       verdict_label, truth_score_label, confidence "
        "FROM fact_checks "
        "WHERE published = 1 AND created_at >= ? AND created_at < ? "
        "ORDER BY claim_date DESC, id",
        (since, until + "T23:59:59"),
    ).fetchall()


def _corrections_received(conn: sqlite3.Connection,
                           since: str, until: str) -> list[sqlite3.Row]:
    # Schema check: corrections table may or may not exist depending
    # on whether the public-mode web UI has been used. Defensive query.
    conn.row_factory = sqlite3.Row
    has_table = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='corrections'"
    ).fetchone()
    if not has_table:
        return []
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(corrections)").fetchall()]
    # Find a 'created_at' or 'submitted_at' column; fall back to id ordering.
    ts_col = ("created_at" if "created_at" in cols
              else "submitted_at" if "submitted_at" in cols
              else None)
    if not ts_col:
        return conn.execute("SELECT * FROM corrections ORDER BY id DESC "
                             "LIMIT 100").fetchall()
    return conn.execute(
        f"SELECT * FROM corrections WHERE {ts_col} >= ? AND {ts_col} < ? "
        f"ORDER BY {ts_col} DESC",
        (since, until + "T23:59:59"),
    ).fetchall()


def _llm_spend_in_window(conn: sqlite3.Connection,
                          since: str, until: str) -> dict[str, float]:
    """Sum cost_usd across each *_runs table for the window."""
    tables = [
        "extraction_runs", "curation_runs", "verification_runs",
        "decomposition_runs", "matching_runs",
        "contradiction_finder_runs", "inspection_runs", "dv_compare_runs",
    ]
    out: dict[str, float] = {}
    for t in tables:
        # Each runs table has its own timestamp column name; we look
        # for a column ending in '_at' to filter on. Defensive.
        try:
            cols = [r[1] for r in conn.execute(
                f"PRAGMA table_info({t})").fetchall()]
        except sqlite3.OperationalError:
            continue
        if not cols or "cost_usd" not in cols:
            continue
        ts_col = ("started_at" if "started_at" in cols
                  else next((c for c in cols if c.endswith("_at")), None))
        if not ts_col:
            continue
        try:
            row = conn.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0) FROM {t} "
                f"WHERE {ts_col} >= ? AND {ts_col} < ?",
                (since, until + "T23:59:59"),
            ).fetchone()
            out[t] = float(row[0] or 0.0)
        except sqlite3.OperationalError:
            continue
    return out


def _methodology_changes(since: str, until: str) -> list[str]:
    """Return git-log lines touching docs/METHODOLOGY.md in the window.
    Best-effort; returns [] if git is unavailable or the file has no
    history yet."""
    try:
        out = subprocess.check_output(
            ["git", "log",
             f"--since={since}",
             f"--until={until} 23:59:59",
             "--pretty=format:%h %ad  %s", "--date=short",
             "--", "docs/METHODOLOGY.md"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return [ln for ln in out.decode().splitlines() if ln.strip()]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError):
        return []


def render_report(conn: sqlite3.Connection,
                   since: str,
                   until: Optional[str] = None) -> str:
    """Render the transparency report markdown for the date window
    [since, until]. `until` defaults to today (UTC)."""
    until = until or _today()
    now = datetime.now(timezone.utc).isoformat()

    fcs   = _fact_checks_issued(conn, since, until)
    corrs = _corrections_received(conn, since, until)
    spend = _llm_spend_in_window(conn, since, until)
    meth  = _methodology_changes(since, until)

    by_cat: dict[str, int] = defaultdict(int)
    by_verdict: dict[str, int] = defaultdict(int)
    for r in fcs:
        by_cat[r["category"] or "_NULL"] += 1
        by_verdict[r["verdict_label"] or "_NULL"] += 1

    total_spend = sum(spend.values())

    lines = [
        f"# Kahzaabu — transparency report",
        "",
        f"Window: **{since}** → **{until}**",
        f"Generated: {now}",
        "",
        "_Per ADR 0010. Regenerate monthly with "
        "`kahzaabu transparency-report --since YYYY-MM-DD`._",
        "",
        "## Fact-checks issued in window",
        "",
        f"Total: **{len(fcs)}**",
        "",
    ]
    if fcs:
        lines.append("### By category")
        lines.append("")
        lines.append("| Category | Count |")
        lines.append("|---|---|")
        for k in sorted(by_cat):
            lines.append(f"| {k} | {by_cat[k]} |")
        lines.append("")
        lines.append("### By AVeriTeC verdict label")
        lines.append("")
        lines.append("| Verdict | Count |")
        lines.append("|---|---|")
        for k in sorted(by_verdict):
            lines.append(f"| {k} | {by_verdict[k]} |")
        lines.append("")
    else:
        lines.append("*No published fact-checks in this window.*")
        lines.append("")

    lines += [
        "## Corrections received",
        "",
        f"Total: **{len(corrs)}**",
        "",
    ]
    if corrs:
        lines.append("Recent corrections:")
        lines.append("")
        for r in list(corrs)[:10]:
            # Show a short excerpt — corrections table shape varies.
            keys = list(r.keys()) if hasattr(r, "keys") else []
            excerpt = ""
            for k in ("body", "message", "text", "details"):
                if k in keys and r[k]:
                    excerpt = (r[k] or "")[:140]; break
            lines.append(f"- {excerpt or '(see DB)'}")
        lines.append("")

    lines += [
        "## LLM spend in window",
        "",
        f"Total: **${total_spend:.2f}**",
        "",
    ]
    if spend:
        lines.append("| Pipeline stage | Cost (USD) |")
        lines.append("|---|---|")
        for t in sorted(spend):
            lines.append(f"| {t} | ${spend[t]:.2f} |")
        lines.append("")

    lines += [
        "## Methodology updates in window",
        "",
    ]
    if meth:
        lines.append("Commits touching `docs/METHODOLOGY.md`:")
        lines.append("")
        for ln in meth:
            lines.append(f"- `{ln}`")
        lines.append("")
    else:
        lines.append("*No methodology changes in this window "
                      "(or git history unavailable).*")
        lines.append("")

    lines += [
        "---",
        "",
        "*Generated by `kahzaabu transparency-report`. "
        "See ADR 0010 for the methodology rationale.*",
    ]
    return "\n".join(lines)


def write_report(conn: sqlite3.Connection,
                  since: str,
                  until: Optional[str] = None,
                  out_dir: Optional[Path] = None) -> Path:
    """Render and save to `data/reports/transparency-<since>_to_<until>.md`."""
    until = until or _today()
    d = out_dir or REPORT_DIR
    d.mkdir(parents=True, exist_ok=True)
    fn = d / f"transparency-{since}_to_{until}.md"
    fn.write_text(render_report(conn, since, until))
    return fn
