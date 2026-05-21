# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 12 — Bias / fairness audit (ADR 0010).

Quantitative transparency: produce a markdown report that surfaces
the categorical distributions a critic would ask about.

  - Category × year contingency table + chi-squared
  - Category × topic contingency table + chi-squared
  - Speaker concentration
  - Verdict-label distribution
  - Authoritative-source coverage (ADR 0011)

`kahzaabu audit` CLI writes the result to `data/reports/audit-<date>.md`.
"""
from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPORT_DIR = Path(__file__).resolve().parents[1] / "data" / "reports"


# ───────────────────────────────────────────────────────────────────
# Chi-squared (stdlib only)
# ───────────────────────────────────────────────────────────────────

def _regularised_lower_gamma(s: float, x: float, n_terms: int = 200) -> float:
    """Series expansion for the regularised lower incomplete gamma
    P(s, x) = γ(s, x) / Γ(s). Used to compute chi-squared p-values
    without scipy. Convergent for x < s + 1; we use a generous term
    count and clamp the input to keep things stable.

    For a chi-squared distribution with k degrees of freedom and
    statistic X², the upper-tail p-value is 1 - P(k/2, X²/2).
    """
    if x <= 0 or s <= 0:
        return 0.0
    # log-domain to avoid overflow
    log_term = -x + s * math.log(x) - math.lgamma(s)
    total = 0.0
    term = 1.0 / s
    total += term
    for k in range(1, n_terms):
        term *= x / (s + k)
        total += term
        if term < 1e-15 * total:
            break
    return math.exp(log_term) * total


def chi_squared_p_value(stat: float, df: int) -> float:
    """Upper-tail p-value for chi-squared(stat, df)."""
    if df <= 0 or stat < 0:
        return float("nan")
    return max(0.0, min(1.0, 1.0 - _regularised_lower_gamma(df / 2, stat / 2)))


def chi_squared_stat(contingency: dict[str, dict[str, int]]) -> tuple[
        float, int]:
    """Compute the chi-squared statistic + degrees of freedom for a
    nested-dict contingency table `{row_label: {col_label: count}}`.

    Returns (stat, df). df = (rows - 1) * (cols - 1).
    """
    rows = sorted(contingency)
    cols = sorted({c for r in contingency.values() for c in r})
    if not rows or not cols:
        return 0.0, 0
    row_sums = {r: sum(contingency[r].get(c, 0) for c in cols) for r in rows}
    col_sums = {c: sum(contingency[r].get(c, 0) for r in rows) for c in cols}
    n = sum(row_sums.values())
    if n == 0:
        return 0.0, 0
    stat = 0.0
    for r in rows:
        for c in cols:
            obs = contingency[r].get(c, 0)
            exp = row_sums[r] * col_sums[c] / n
            if exp > 0:
                stat += (obs - exp) ** 2 / exp
    df = (len(rows) - 1) * (len(cols) - 1)
    return stat, df


# ───────────────────────────────────────────────────────────────────
# Audit queries
# ───────────────────────────────────────────────────────────────────

def _fetch_distribution(conn: sqlite3.Connection,
                         row_sql: str,
                         col_sql: str) -> dict[str, dict[str, int]]:
    """Build a contingency table by querying (row, col, COUNT(*)) and
    bucketing into a nested dict."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    sql = (f"SELECT {row_sql} AS row_v, {col_sql} AS col_v, COUNT(*) AS n "
           f"FROM fact_checks WHERE published = 1 "
           f"GROUP BY row_v, col_v")
    for r in conn.execute(sql):
        row_v = r[0] if r[0] is not None else "_NULL"
        col_v = r[1] if r[1] is not None else "_NULL"
        out[str(row_v)][str(col_v)] = int(r[2])
    return {k: dict(v) for k, v in out.items()}


def category_by_year(conn: sqlite3.Connection) -> dict:
    return _fetch_distribution(conn, "category",
                                 "strftime('%Y', claim_date)")


def category_by_topic(conn: sqlite3.Connection,
                       topic_limit: int = 8) -> dict:
    # Restrict to the most-common topics so the table stays readable.
    topics = [r[0] for r in conn.execute(
        "SELECT topic FROM fact_checks WHERE published = 1 "
        "AND topic IS NOT NULL "
        "GROUP BY topic ORDER BY COUNT(*) DESC LIMIT ?",
        (topic_limit,)
    )]
    if not topics:
        return {}
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    placeholders = ",".join("?" * len(topics))
    sql = (f"SELECT category, topic, COUNT(*) FROM fact_checks "
           f"WHERE published = 1 AND topic IN ({placeholders}) "
           f"GROUP BY category, topic")
    for r in conn.execute(sql, topics):
        out[str(r[0])][str(r[1])] = int(r[2])
    return {k: dict(v) for k, v in out.items()}


def speaker_distribution(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [(r[0] or "_unknown", int(r[1]))
            for r in conn.execute(
                "SELECT speaker, COUNT(*) FROM fact_checks "
                "WHERE published = 1 GROUP BY speaker ORDER BY 2 DESC")]


def verdict_distribution(conn: sqlite3.Connection) -> dict[str, int]:
    return {r[0] or "_NULL": int(r[1])
            for r in conn.execute(
                "SELECT verdict_label, COUNT(*) FROM fact_checks "
                "WHERE published = 1 GROUP BY verdict_label "
                "ORDER BY 2 DESC")}


def truth_score_distribution(conn: sqlite3.Connection) -> dict[str, int]:
    return {(r[0] or "_NULL"): int(r[1])
            for r in conn.execute(
                "SELECT truth_score_label, COUNT(*) FROM fact_checks "
                "WHERE published = 1 GROUP BY truth_score_label "
                "ORDER BY MIN(truth_score)")}


def authoritative_source_coverage(conn: sqlite3.Connection) -> dict:
    """Coverage of fact_check_evidence by authoritative_entity_id
    (ADR 0011). Returns counts grouped by entity_id and the
    primary-source rate (fraction of evidence that's authoritative)."""
    total = conn.execute(
        "SELECT COUNT(*) FROM fact_check_evidence"
    ).fetchone()[0]
    authoritative = conn.execute(
        "SELECT COUNT(*) FROM fact_check_evidence "
        "WHERE authoritative_entity_id IS NOT NULL"
    ).fetchone()[0]
    by_entity = {r[0]: int(r[1]) for r in conn.execute(
        "SELECT authoritative_entity_id, COUNT(*) "
        "FROM fact_check_evidence "
        "WHERE authoritative_entity_id IS NOT NULL "
        "GROUP BY authoritative_entity_id ORDER BY 2 DESC")}
    return {
        "total_evidence_rows":    total,
        "authoritative_rows":     authoritative,
        "primary_source_rate":    (authoritative / total) if total else 0.0,
        "by_entity":              by_entity,
    }


# ───────────────────────────────────────────────────────────────────
# Markdown rendering
# ───────────────────────────────────────────────────────────────────

def _render_contingency(title: str,
                          data: dict[str, dict[str, int]],
                          interpretation: str = "") -> list[str]:
    """Render a contingency table + chi-squared as markdown lines."""
    lines = [f"## {title}", ""]
    if not data:
        lines.append("*(no data)*"); lines.append("")
        return lines
    rows = sorted(data)
    cols = sorted({c for r in data.values() for c in r})
    header = "| | " + " | ".join(cols) + " |"
    sep    = "|---|" + "|".join("---" for _ in cols) + "|"
    lines.append(header)
    lines.append(sep)
    for r in rows:
        line = f"| **{r}** | " + " | ".join(
            str(data[r].get(c, 0)) for c in cols) + " |"
        lines.append(line)
    stat, df = chi_squared_stat(data)
    p = chi_squared_p_value(stat, df) if df > 0 else float("nan")
    lines.append("")
    lines.append(f"chi-squared: **{stat:.3f}** (df = {df})  "
                 f"p-value ≈ **{p:.4f}**")
    if df > 0:
        crit_005 = _chi2_critical_005(df)
        verdict = ("reject null (distributions differ at p<0.05)"
                   if stat > crit_005
                   else "fail to reject null (distributions consistent)")
        lines.append(f"critical value at p=0.05 (df={df}): "
                      f"**{crit_005:.3f}** → {verdict}")
    if interpretation:
        lines.append("")
        lines.append(f"*{interpretation}*")
    lines.append("")
    return lines


def _chi2_critical_005(df: int) -> float:
    """Wilson–Hilferty approximation: returns the chi-squared critical
    value at p=0.05 for the given df. Accurate to <1% for df >= 2."""
    # z_{0.95} = 1.6449
    z = 1.6449
    return df * (1 - 2 / (9 * df) + z * math.sqrt(2 / (9 * df))) ** 3


def render_audit_report(conn: sqlite3.Connection) -> str:
    """Generate the full audit markdown."""
    now = datetime.now(timezone.utc).isoformat()
    lines: list[str] = [
        "# Kahzaabu — bias / fairness audit",
        "",
        f"Generated: {now}",
        "",
        "Quantitative transparency for kahzaabu's categorical distributions. "
        "Methodology: ADR 0010. The chi-squared tests check whether the "
        "row/column distributions are independent; a p-value < 0.05 means "
        "they are *not* independent (i.e. there's a relationship).",
        "",
        "**Important caveat**: kahzaabu's published corpus has a single "
        "speaker (Mohamed Muizzu, President). Speaker concentration is "
        "100% by design. Cross-corpus bias evaluation requires expanding "
        "the corpus beyond a single subject — out of scope for V2.",
        "",
    ]

    # Category × year
    cy = category_by_year(conn)
    lines += _render_contingency(
        "Category distribution by year",
        cy,
        interpretation=(
            "A significant p-value here means the mix of categories "
            "(LIE, MISLEADING, etc.) shifts year-over-year. That could "
            "reflect real policy-output changes, or analyst drift; both "
            "are worth surfacing.")
    )

    # Category × topic
    ct = category_by_topic(conn)
    lines += _render_contingency(
        "Category distribution by topic (top topics)",
        ct,
        interpretation=(
            "Some categories naturally cluster on certain topics "
            "(BROKEN_DEADLINE on infrastructure-promise topics; "
            "CREDIT_THEFT on inherited-project topics). Significant "
            "p-value is expected — the test surfaces *which* topics "
            "drive it.")
    )

    # Verdict-label distribution
    vd = verdict_distribution(conn)
    lines += [
        "## AVeriTeC verdict-label distribution",
        "",
        "| Verdict | Count |",
        "|---|---|",
    ]
    for k, n in vd.items():
        lines.append(f"| {k} | {n} |")
    lines.append("")

    # Truth-score ladder
    ts = truth_score_distribution(conn)
    lines += [
        "## Truth-O-Meter ladder distribution",
        "",
        "| Truth-score label | Count |",
        "|---|---|",
    ]
    for k, n in ts.items():
        lines.append(f"| {k} | {n} |")
    lines.append("")

    # Speaker concentration
    sd = speaker_distribution(conn)
    lines += ["## Speaker concentration", ""]
    total = sum(n for _, n in sd) or 1
    for sp, n in sd:
        pct = 100 * n / total
        lines.append(f"- **{sp}**: {n} / {total} ({pct:.1f}%)")
    lines.append("")
    if len(sd) <= 1:
        lines.append("*Single-speaker corpus — kahzaabu's design. "
                      "Re-run this audit once the corpus expands to "
                      "multi-speaker.*")
        lines.append("")

    # Authoritative-source coverage (ADR 0011)
    asc = authoritative_source_coverage(conn)
    lines += [
        "## Authoritative external-source coverage (ADR 0011)",
        "",
        f"- Total evidence rows: **{asc['total_evidence_rows']:,}**",
        f"- On registered public-sector domains: "
        f"**{asc['authoritative_rows']:,}** "
        f"(**{100*asc['primary_source_rate']:.1f}%**)",
        "",
        "Breakdown by entity:",
        "",
        "| entity_id | rows |",
        "|---|---|",
    ]
    if asc["by_entity"]:
        for eid, n in asc["by_entity"].items():
            lines.append(f"| {eid} | {n} |")
    else:
        lines.append("| *(none yet)* | 0 |")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*Audit produced by `kahzaabu audit`. See ADR 0010 "
                  "for the methodology rationale.*")
    return "\n".join(lines)


def write_audit_report(conn: sqlite3.Connection,
                        out_dir: Optional[Path] = None) -> Path:
    """Render the report and save it to `data/reports/audit-<date>.md`.
    Returns the written path."""
    d = out_dir or REPORT_DIR
    d.mkdir(parents=True, exist_ok=True)
    fn = d / f"audit-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    fn.write_text(render_audit_report(conn))
    return fn
