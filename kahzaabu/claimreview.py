"""V2 Slice 6 — schema.org ClaimReview JSON-LD export (ADR 0006).

Generates a discovery-ready JSON-LD blob per published fact-check.
Google Fact Check Explorer, Bing's fact-check surfacing, and similar
indexers consume schema.org ClaimReview markup; without it, kahzaabu's
output is invisible to those discovery surfaces.

The blob is stored in fact_checks.claimreview_jsonld (cached) and served
three ways (per ADR 0006):

  1. Inline <script type="application/ld+json"> on /factcheck/{id} page
     (Slice 7 — web UI work).
  2. GET /api/factchecks/{id}/jsonld endpoint (this slice).
  3. GET /api/claimreviews/feed.json aggregate (this slice).

All three serve the same cached blob unless ?refresh=1 is supplied.

This module is PURE — no LLM calls, no I/O beyond the DB. Building a
blob is a deterministic transformation of fact_checks + truth_score +
articles.

Disclaimer requirement: ADR 0006 mandates every emitted blob carries
the automated-analysis disclaimer. Stripping the disclaimer is a
material accuracy issue; tests pin the field's presence.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Optional

logger = logging.getLogger("kahzaabu")

# Public configuration — base URL where kahzaabu is hosted. Defaults to
# localhost for dev; production deploys set KAHZAABU_PUBLIC_BASE_URL.
PUBLIC_BASE_URL_ENV = "KAHZAABU_PUBLIC_BASE_URL"
DEFAULT_BASE_URL = "http://localhost:8765"
ORG_NAME = "Kahzaabu"
ORG_URL_ENV = "KAHZAABU_ORG_URL"
ORG_SAMEAS_ENV = "KAHZAABU_ORG_SAMEAS"   # comma-separated URLs

DISCLAIMER = (
    "This fact-check is the output of an automated analysis pipeline. "
    "The categorical verdict and 1-6 truth score are derived "
    "deterministically from extracted evidence; the underlying claim "
    "is verified against the official press release archive at "
    "presidency.gov.mv and (where applicable) Anthropic's web_search "
    "tool. Constitutional citations use the 2008 Dheena Hussain "
    "functional translation; the legally binding text is the Dhivehi "
    "original. This is not legal advice and not finished journalism — "
    "review the original press release before quoting."
)


def _base_url(env: Optional[dict] = None) -> str:
    env = env if env is not None else os.environ
    return env.get(PUBLIC_BASE_URL_ENV, DEFAULT_BASE_URL).rstrip("/")


def _org_block(env: Optional[dict] = None) -> dict:
    env = env if env is not None else os.environ
    block: dict = {
        "@type": "Organization",
        "name": ORG_NAME,
        "url": env.get(ORG_URL_ENV) or _base_url(env),
    }
    sameas = env.get(ORG_SAMEAS_ENV)
    if sameas:
        urls = [u.strip() for u in sameas.split(",") if u.strip()]
        if urls:
            block["sameAs"] = urls
    return block


def _format_date(d: Optional[str]) -> Optional[str]:
    """Schema.org dates are ISO-8601. Trim trailing time component when
    we only have a YYYY-MM-DD claim_date."""
    if not d:
        return None
    return d[:10]


def build_jsonld(conn: sqlite3.Connection, fact_check_id: int,
                  env: Optional[dict] = None) -> dict:
    """Assemble a ClaimReview JSON-LD blob for one fact_check. Returns
    a Python dict (caller can json.dumps()). Raises ValueError if the
    row doesn't exist; returns the blob for unpublished rows but the
    caller is responsible for not exposing it publicly."""
    env = env if env is not None else os.environ

    row = conn.execute(
        """SELECT id, category, verdict_label, truth_score,
                  truth_score_label, claim_date, claim, public_summary,
                  source_article_ids, evidence_quotes, speaker,
                  canonical_url, created_at
           FROM fact_checks WHERE id = ?""",
        (fact_check_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"fact_check {fact_check_id} not found")

    fc = dict(row) if hasattr(row, "keys") else dict(zip(
        ("id", "category", "verdict_label", "truth_score",
         "truth_score_label", "claim_date", "claim", "public_summary",
         "source_article_ids", "evidence_quotes", "speaker",
         "canonical_url", "created_at"), row))

    base = _base_url(env)
    fc_url = fc.get("canonical_url") or f"{base}/factcheck/{fc['id']}"

    # Source articles → appearance[]. Use presidency.gov.mv URL when we
    # can derive it from the article.reference field; fall back to a
    # kahzaabu-side article-detail URL.
    article_urls = _resolve_source_article_urls(
        conn, fc.get("source_article_ids"), base,
    )

    # reviewRating: ADR 0006's mapping (ratingValue=truth_score 1-6).
    rating: dict = {
        "@type": "Rating",
        "bestRating": 6,
        "worstRating": 1,
    }
    if fc.get("truth_score") is not None:
        rating["ratingValue"] = int(fc["truth_score"])
    if fc.get("truth_score_label"):
        rating["alternateName"] = _humanize_truth_label(fc["truth_score_label"])
    explanation = _rating_explanation(fc)
    if explanation:
        rating["ratingExplanation"] = explanation

    item_reviewed: dict = {
        "@type": "Claim",
        "datePublished": _format_date(fc.get("claim_date")),
        "author": {
            "@type": "Person",
            "name": fc.get("speaker") or "Mohamed Muizzu",
            "jobTitle": "President of the Maldives",
        },
    }
    if article_urls:
        item_reviewed["appearance"] = [
            {"@type": "CreativeWork", "url": u} for u in article_urls
        ]

    blob: dict = {
        "@context": "https://schema.org",
        "@type": "ClaimReview",
        "datePublished": _format_date(
            fc.get("created_at") or fc.get("claim_date")
        ),
        "url": fc_url,
        "claimReviewed": _claim_text(fc),
        "author": _org_block(env),
        "reviewRating": rating,
        "itemReviewed": item_reviewed,
        "disclaimer": DISCLAIMER,
    }
    # Schema validators (e.g. Google's Rich Results test) reject NULL
    # values inside the JSON — drop None entries.
    return _drop_none(blob)


def _claim_text(fc: dict) -> str:
    """Pick the best human-readable claim text. Prefer public_summary
    (curator-friendly), fall back to claim (full)."""
    text = fc.get("public_summary") or fc.get("claim") or ""
    text = text.strip()
    # schema.org claimReviewed is a Text field; truncate at 700 chars to
    # stay within typical indexer caps.
    if len(text) > 700:
        text = text[:697] + "..."
    return text


def _rating_explanation(fc: dict) -> Optional[str]:
    """One-liner: category + verdict_label. Used by ?ratingExplanation."""
    cat = fc.get("category") or ""
    v = fc.get("verdict_label") or ""
    if not cat and not v:
        return None
    return f"{cat} — {v}".strip(" —")


def _humanize_truth_label(label: str) -> str:
    """PANTS_ON_FIRE → 'Pants on Fire'. Friendlier for indexer display."""
    return label.replace("_", " ").title()


def _resolve_source_article_urls(conn, source_article_ids_json,
                                   base_url: str) -> list[str]:
    """Parse fact_checks.source_article_ids (JSON int array) and resolve
    each to a public URL. Uses articles.reference if available (that's
    the presidency.gov.mv slug); otherwise constructs a local
    /article/{id} URL."""
    try:
        ids = json.loads(source_article_ids_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT id, reference FROM articles
            WHERE id IN ({placeholders}) AND language = 'EN'""",
        ids,
    ).fetchall()
    urls: list[str] = []
    for r in rows:
        ref = r[1] if not hasattr(r, "keys") else r["reference"]
        aid = r[0] if not hasattr(r, "keys") else r["id"]
        if ref and ref.startswith(("http://", "https://")):
            urls.append(ref)
        else:
            urls.append(f"{base_url}/article/{aid}")
    return urls


def _drop_none(obj):
    """Recursively strip None values + empty containers from a dict/list."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            v2 = _drop_none(v)
            if v2 is None:
                continue
            if isinstance(v2, (list, dict)) and not v2:
                continue
            out[k] = v2
        return out
    if isinstance(obj, list):
        return [_drop_none(x) for x in obj if x is not None]
    return obj


# ─────────────────────────────────────────────────────────────────
# Persistence + bulk regenerate
# ─────────────────────────────────────────────────────────────────

def cache_jsonld(conn: sqlite3.Connection, fact_check_id: int,
                  env: Optional[dict] = None) -> dict:
    """Build + persist a JSON-LD blob to fact_checks.claimreview_jsonld.
    Returns the dict. Idempotent — overwrites existing cache."""
    blob = build_jsonld(conn, fact_check_id, env=env)
    conn.execute(
        "UPDATE fact_checks SET claimreview_jsonld = ? WHERE id = ?",
        (json.dumps(blob, ensure_ascii=False), fact_check_id),
    )
    conn.commit()
    return blob


def regenerate_all(conn: sqlite3.Connection, *,
                    only_published: bool = True,
                    env: Optional[dict] = None,
                    progress_cb=None) -> dict:
    """Refresh the cached blob for every fact_check (or only published
    ones, the default). Returns counts.

    Only published fact_checks are emittable per ADR 0006; we still
    expose `only_published=False` for testing."""
    sql = "SELECT id FROM fact_checks"
    if only_published:
        sql += " WHERE published = 1"
    ids = [r[0] for r in conn.execute(sql)]

    for i, fcid in enumerate(ids):
        cache_jsonld(conn, fcid, env=env)
        if progress_cb and (i % 50 == 0 or i == len(ids) - 1):
            progress_cb(i + 1, len(ids))

    return {"regenerated": len(ids)}
