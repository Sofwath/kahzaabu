# SPDX-License-Identifier: Apache-2.0
"""Claim and fact-check persistence — schema + CRUD on the same SQLite DB."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import db

CLAIMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    language TEXT NOT NULL,
    extraction_run_id INTEGER,
    type TEXT,
    subject TEXT,
    value TEXT,
    deadline TEXT,
    actor_credited TEXT,
    quote TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (article_id, language) REFERENCES articles(id, language)
);

CREATE INDEX IF NOT EXISTS idx_claims_article ON claims(article_id, language);
CREATE INDEX IF NOT EXISTS idx_claims_type    ON claims(type);
CREATE INDEX IF NOT EXISTS idx_claims_run     ON claims(extraction_run_id);

CREATE TABLE IF NOT EXISTS extraction_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    articles_processed INTEGER DEFAULT 0,
    claims_extracted INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    status TEXT DEFAULT 'running',
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS fact_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    claim_date TEXT NOT NULL,
    claim TEXT NOT NULL,
    what_actually_happened TEXT,
    type TEXT,
    source_article_ids TEXT NOT NULL,   -- JSON array
    evidence_quotes TEXT,                -- JSON array
    topic TEXT,
    confidence TEXT DEFAULT 'auto',      -- 'auto' | 'reviewed' | 'rejected'
    source TEXT,                         -- 'existing_master' | 'phase2' | 'phase4' | 'manual' | 'auto'
    curation_run_id INTEGER,
    fingerprint TEXT,                    -- dedupe key (category + date + first 100 chars of claim lower)
    created_at TEXT NOT NULL,
    UNIQUE(fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_fc_category ON fact_checks(category);
CREATE INDEX IF NOT EXISTS idx_fc_date     ON fact_checks(claim_date);
CREATE INDEX IF NOT EXISTS idx_fc_topic    ON fact_checks(topic);
CREATE INDEX IF NOT EXISTS idx_fc_status   ON fact_checks(confidence);

CREATE TABLE IF NOT EXISTS curation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    chunks_processed INTEGER DEFAULT 0,
    new_items INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    status TEXT DEFAULT 'running',
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS fact_check_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_check_id INTEGER NOT NULL,
    source_type TEXT,         -- 'web' | 'manual' | 'article'
    url TEXT,
    title TEXT,
    snippet TEXT,
    relevance TEXT,           -- 'confirms' | 'contradicts' | 'context' | 'unclear' | 'not_found'
    summary TEXT,             -- 1-2 sentence summary of what this evidence shows
    retrieved_at TEXT NOT NULL,
    verification_run_id INTEGER,
    FOREIGN KEY (fact_check_id) REFERENCES fact_checks(id)
);

CREATE INDEX IF NOT EXISTS idx_fce_fc ON fact_check_evidence(fact_check_id);
CREATE INDEX IF NOT EXISTS idx_fce_rel ON fact_check_evidence(relevance);

CREATE TABLE IF NOT EXISTS verification_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    items_processed INTEGER DEFAULT 0,
    evidence_collected INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    web_searches INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    status TEXT DEFAULT 'running',
    error_message TEXT
);

-- Phase 2: per-article inspector output
CREATE TABLE IF NOT EXISTS article_fact_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    language TEXT NOT NULL,
    summary TEXT,                       -- 2-3 sentence summary
    key_claims_json TEXT,               -- top 3-5 checkable claims (JSON array)
    history_check TEXT,                 -- "this contradicts X said on Y"
    web_evidence_json TEXT,             -- JSON array of {url,title,snippet,relevance,summary}
    severity TEXT,                       -- 'clean' | 'flag' | 'red_flag'
    visualization_spec_json TEXT,       -- Chart.js spec
    cost_usd REAL DEFAULT 0,
    inspection_run_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(article_id, language)
);
CREATE INDEX IF NOT EXISTS idx_card_article ON article_fact_cards(article_id, language);
CREATE INDEX IF NOT EXISTS idx_card_severity ON article_fact_cards(severity);

CREATE TABLE IF NOT EXISTS inspection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    articles_processed INTEGER DEFAULT 0,
    cards_generated INTEGER DEFAULT 0,
    flagged INTEGER DEFAULT 0,
    red_flagged INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    web_searches INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    status TEXT DEFAULT 'running',
    error_message TEXT
);

-- Phase 2: DV-EN translation diff
CREATE TABLE IF NOT EXISTS dv_en_inconsistencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    en_article_id INTEGER NOT NULL,
    dv_article_id INTEGER NOT NULL,
    severity TEXT,                       -- 'minor' | 'moderate' | 'serious'
    category TEXT,                       -- 'numeric_discrepancy' | 'omission' | 'softening' | 'embellishment' | 'other'
    en_quote TEXT,
    dv_quote TEXT,
    dv_translation_to_en TEXT,
    explanation TEXT,
    dv_compare_run_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dvenc_en ON dv_en_inconsistencies(en_article_id);
CREATE INDEX IF NOT EXISTS idx_dvenc_sev ON dv_en_inconsistencies(severity);

-- Track which pairs have been compared (even if zero inconsistencies found)
CREATE TABLE IF NOT EXISTS dv_compare_pairs (
    en_article_id INTEGER PRIMARY KEY,
    dv_article_id INTEGER NOT NULL,
    n_inconsistencies INTEGER DEFAULT 0,
    max_severity TEXT,
    compared_at TEXT NOT NULL,
    dv_compare_run_id INTEGER,
    cost_usd REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dvcp_sev ON dv_compare_pairs(max_severity);

CREATE TABLE IF NOT EXISTS dv_compare_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    pairs_processed INTEGER DEFAULT 0,
    pairs_with_issues INTEGER DEFAULT 0,
    inconsistencies_logged INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    status TEXT DEFAULT 'running',
    error_message TEXT
);

-- Phase 3: public/admin auth + publish workflow
CREATE TABLE IF NOT EXISTS web_users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,                 -- 'admin' | 'editor'
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_check_id INTEGER,
    article_id INTEGER,
    reporter_contact TEXT,                -- email or other contact (optional)
    body TEXT NOT NULL,
    status TEXT DEFAULT 'open',           -- 'open' | 'reviewed' | 'rejected'
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT,
    review_notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_corrections_status ON corrections(status);

-- Manifesto promises (extracted from Muizzu 2023 campaign manifesto PDF)
CREATE TABLE IF NOT EXISTS manifesto_promises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section TEXT,                          -- section/chapter heading (DV or EN)
    promise_text_dv TEXT,                  -- verbatim Dhivehi
    promise_text_en TEXT,                  -- LLM-translated English
    category TEXT,                         -- housing | infrastructure | economy | governance | ...
    subject TEXT,                          -- short subject for matching (e.g. "Vilimalé tertiary hospital")
    target_value TEXT,                     -- quantified target if present (e.g. "100 beds", "MVR 1B")
    deadline_stated TEXT,                  -- deadline if present ("year 1", "5 years", "before 2028")
    delivery_status TEXT DEFAULT 'unmentioned',
        -- 'delivered' | 'in_progress' | 'broken' | 'unmentioned' | 'modified' | 'abandoned'
    delivery_evidence_json TEXT,           -- JSON: linked article_ids + fact_check_ids + notes
    chunk_index INTEGER,                   -- which chunk of source text it came from
    extraction_run_id INTEGER,
    cross_ref_run_id INTEGER,
    published INTEGER DEFAULT 1,           -- gated by PUBLIC_MODE filter
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manifesto_status   ON manifesto_promises(delivery_status);
CREATE INDEX IF NOT EXISTS idx_manifesto_category ON manifesto_promises(category);
CREATE INDEX IF NOT EXISTS idx_manifesto_section  ON manifesto_promises(section);

-- Conversation memory for agentic kahzaabu_ask
CREATE TABLE IF NOT EXISTS qna_sessions (
    id TEXT PRIMARY KEY,                   -- uuid4 hex
    messages_json TEXT NOT NULL,           -- full Anthropic messages array
    total_cost_usd REAL DEFAULT 0,
    n_turns INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qna_last_used ON qna_sessions(last_used_at);

CREATE TABLE IF NOT EXISTS manifesto_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    kind TEXT NOT NULL,                    -- 'extract' | 'cross_ref'
    chunks_processed INTEGER DEFAULT 0,
    promises_extracted INTEGER DEFAULT 0,
    promises_cross_ref INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    status TEXT DEFAULT 'running',
    error_message TEXT
);
"""

# Phase 3: ALTER fact_checks to add publish workflow columns (idempotent helpers)
PUBLISH_MIGRATIONS = [
    "ALTER TABLE fact_checks ADD COLUMN published INTEGER DEFAULT 0",
    "ALTER TABLE fact_checks ADD COLUMN public_summary TEXT",
    "ALTER TABLE fact_checks ADD COLUMN reviewed_at TEXT",
    "ALTER TABLE fact_checks ADD COLUMN reviewed_by TEXT",
    "CREATE INDEX IF NOT EXISTS idx_fc_published ON fact_checks(published)",
    "ALTER TABLE article_fact_cards ADD COLUMN published INTEGER DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_card_published ON article_fact_cards(published)",
    "ALTER TABLE dv_compare_pairs ADD COLUMN published INTEGER DEFAULT 0",
]

# V2 Slice 1 — claims enrichment (ADR 0002):
#   polarity            stance label, 6-way (AFFIRM / DENY / PROMISE /
#                       DENIAL_OF_PROMISE / CLAIM_OF_FACT / NEUTRAL);
#                       NULL until enriched.
#   subject_normalized  entity-resolved subject (e.g. "the President" /
#                       "Muizzu" / "Dr Mohamed Muizzu" -> "President Muizzu");
#                       used by the contradiction finder to bucket claims.
#   is_checkable        1 = factual claim a verifier should look at;
#                       0 = rhetoric / opinion (PolitiFact's filter);
#                       NULL until enriched.
V2_SLICE1_MIGRATIONS = [
    "ALTER TABLE claims ADD COLUMN polarity TEXT",
    "ALTER TABLE claims ADD COLUMN subject_normalized TEXT",
    "ALTER TABLE claims ADD COLUMN is_checkable INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_claims_polarity   ON claims(polarity)",
    "CREATE INDEX IF NOT EXISTS idx_claims_subject_n  ON claims(subject_normalized)",
    "CREATE INDEX IF NOT EXISTS idx_claims_checkable  ON claims(is_checkable)",
]

# V2 Slice 2 — Q&A decomposition (ADR 0001, AVeriTeC-shaped).
#   Each checkable claim decomposes into 2-5 sub-questions a researcher
#   would need answered to verify it. Answers are filled later (Slice 5).
#   answer_type follows the AVeriTeC enum: Abstractive | Extractive |
#   Boolean | Unanswerable. source_medium tracks where the answer came
#   from (archive | web_search | constitution | manifesto).
V2_SLICE2_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    answer_type TEXT,
    source_url TEXT,
    source_medium TEXT,
    confidence REAL,
    decomposition_run_id INTEGER,
    answered_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (claim_id) REFERENCES claims(id)
);
CREATE INDEX IF NOT EXISTS idx_cq_claim       ON claim_questions(claim_id);
CREATE INDEX IF NOT EXISTS idx_cq_unanswered  ON claim_questions(claim_id)
    WHERE answer IS NULL;

CREATE TABLE IF NOT EXISTS decomposition_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    claims_processed INTEGER DEFAULT 0,
    questions_generated INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    model TEXT,
    status TEXT DEFAULT 'running',
    error_message TEXT
);
"""

VALID_ANSWER_TYPES = frozenset({
    "Abstractive", "Extractive", "Boolean", "Unanswerable",
})
VALID_SOURCE_MEDIUMS = frozenset({
    "archive", "web_search", "constitution", "manifesto",
})

# V2 Slice 3 — canonical claim matching (ADR 0003).
#   canonical_claim_id (FK on claims, self-referential) — claims that
#       say the same thing point back to the FIRST occurrence. The first
#       occurrence either points to itself (canonical_claim_id = id) or
#       has NULL (we use self-pointer convention for clarity).
#   claim_embeddings — side table storing the float32 embedding vector
#       per claim, packed as a BLOB. Used to short-list candidate
#       matches via cosine similarity before LLM tiebreaker.
V2_SLICE3_MIGRATIONS = [
    "ALTER TABLE claims ADD COLUMN canonical_claim_id INTEGER REFERENCES claims(id)",
    "CREATE INDEX IF NOT EXISTS idx_claims_canonical ON claims(canonical_claim_id)",
]

# V2 Slice 4 — contradiction finder (ADR 0004).
#   contradiction_pairs — explicit pairwise contradictions with 4-way
#   verdict. Each row carries a reasoning_chain (JSON) explaining the
#   verdict, so the system's call is defensible / re-verifiable.
V2_SLICE4_SCHEMA = """
CREATE TABLE IF NOT EXISTS contradiction_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_a_id INTEGER NOT NULL REFERENCES claims(id),
    claim_b_id INTEGER NOT NULL REFERENCES claims(id),
    subject TEXT NOT NULL,
    verdict TEXT NOT NULL
        CHECK(verdict IN ('CONTRADICTION', 'EVOLVING_POSITION',
                          'CONTEXT_CHANGED', 'NOT_CONTRADICTORY')),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    reasoning_chain TEXT NOT NULL,
    published INTEGER DEFAULT 0,
    reviewed_at TEXT,
    reviewed_by TEXT,
    finder_run_id INTEGER,
    detected_at TEXT NOT NULL,
    UNIQUE(claim_a_id, claim_b_id)
);
CREATE INDEX IF NOT EXISTS idx_contra_subject   ON contradiction_pairs(subject);
CREATE INDEX IF NOT EXISTS idx_contra_verdict   ON contradiction_pairs(verdict);
CREATE INDEX IF NOT EXISTS idx_contra_published ON contradiction_pairs(published);

CREATE TABLE IF NOT EXISTS contradiction_finder_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    candidates_shortlisted INTEGER DEFAULT 0,
    pairs_classified INTEGER DEFAULT 0,
    contradictions INTEGER DEFAULT 0,
    evolving INTEGER DEFAULT 0,
    context_changed INTEGER DEFAULT 0,
    not_contradictory INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    model TEXT,
    status TEXT DEFAULT 'running',
    error_message TEXT
);
"""

VALID_CONTRADICTION_VERDICTS = frozenset({
    "CONTRADICTION", "EVOLVING_POSITION",
    "CONTEXT_CHANGED", "NOT_CONTRADICTORY",
})

# V2 Slice 5 — AVeriTeC verdict labels + PolitiFact Truth-O-Meter +
# RAGAR reasoning chains on fact_checks (ADR 0005).
V2_SLICE5_MIGRATIONS = [
    "ALTER TABLE fact_checks ADD COLUMN verdict_label TEXT",
    "ALTER TABLE fact_checks ADD COLUMN truth_score INTEGER",
    "ALTER TABLE fact_checks ADD COLUMN truth_score_label TEXT",
    "ALTER TABLE fact_checks ADD COLUMN reasoning_chain TEXT",
    "ALTER TABLE fact_checks ADD COLUMN contradiction_pair_id INTEGER REFERENCES contradiction_pairs(id)",
    "ALTER TABLE fact_checks ADD COLUMN speaker TEXT DEFAULT 'Mohamed Muizzu'",
    "ALTER TABLE fact_checks ADD COLUMN canonical_url TEXT",
    "CREATE INDEX IF NOT EXISTS idx_fc_verdict_label  ON fact_checks(verdict_label)",
    "CREATE INDEX IF NOT EXISTS idx_fc_truth_score    ON fact_checks(truth_score)",
    "CREATE INDEX IF NOT EXISTS idx_fc_contradiction  ON fact_checks(contradiction_pair_id)",
]

# V2 Slice 6 — ClaimReview JSON-LD storage (ADR 0006).
# Cached schema.org blob; regenerated on demand or via the
# `kahzaabu export-claimreview` CLI.
V2_SLICE6_MIGRATIONS = [
    "ALTER TABLE fact_checks ADD COLUMN claimreview_jsonld TEXT",
]

# V2 Slice 11.5 — Authoritative external-reference registry (ADR 0011).
# When a fact_check_evidence row's URL hostname matches a registered
# .gov.mv (or similar) domain in data/registry/, we tag it with the
# entity_id here. Used by the verifier to weight evidence and by the
# web UI to render a trust badge. Nullable: evidence on non-registered
# domains remains unaffected.
V2_SLICE_REGISTRY_MIGRATIONS = [
    "ALTER TABLE fact_check_evidence ADD COLUMN authoritative_entity_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_fce_auth_entity "
        "ON fact_check_evidence(authoritative_entity_id)",
]

# V2 Slice 12 — Reproducibility manifest (ADR 0010).
# Stamps the git commit hash on each fact-check at publication time so
# reproducibility.json can return the exact code revision that produced
# the verdict. Nullable: pre-Slice-12 fact-checks retain NULL.
V2_SLICE12_MIGRATIONS = [
    "ALTER TABLE fact_checks ADD COLUMN git_sha_at_publication TEXT",
]

# Slice 14 — Hermes plugin ambient-hook sticky-session state.
# Cross-process persistence for the pre_llm_call hook. Scoped per-
# user-install (UUID session_ids; multi-tenant is an explicit non-
# goal per ADR 0013). Full design + alternatives + regression
# guards in docs/adr/0014-hermes-ambient-hook.md.
V2_SLICE14_AMBIENT_HOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS ambient_hot_sessions (
    session_id TEXT PRIMARY KEY,
    hot_until  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ambient_hot_until
    ON ambient_hot_sessions(hot_until);
"""

# Slice 15 — Article-revision tracking (ADR 0015).
#
# Detects when presidency.gov.mv edits an already-archived article
# after we've scraped it ("4 → 1" edits, photo swaps, claim
# rewordings). Each scrape recomputes a content hash; on mismatch
# the scraper archives the old row to article_revisions BEFORE
# the upsert overwrites it. Full design in
# docs/adr/0015-article-revisions.md.
V2_SLICE15_REVISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS article_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id      INTEGER NOT NULL,
    language        TEXT    NOT NULL,
    content_hash    TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    body_text       TEXT,
    body_html       TEXT,
    image_urls      TEXT,
    reference       TEXT,
    published_date  TEXT,
    observed_at     TEXT    NOT NULL,
    replaced_at     TEXT    NOT NULL,
    diff_summary    TEXT
);
CREATE INDEX IF NOT EXISTS idx_article_revisions
    ON article_revisions(article_id, language);
"""

V2_SLICE15_MIGRATIONS = [
    # Additive: existing rows get NULL content_hash until next scrape;
    # the scraper treats NULL == "we don't know" and stores the hash
    # without flagging a change (no false-positive on first observation).
    "ALTER TABLE articles ADD COLUMN content_hash TEXT",
    # Slice 15B: when a source article gets revised, every fact_check
    # whose source_article_ids includes it gets this timestamp set so
    # an operator can list fact-checks pointing at potentially-stale
    # source text. NULL = source unchanged since publication.
    "ALTER TABLE fact_checks ADD COLUMN source_changed_at TEXT",
]

# Slice 16 — Press-office-style EN ↔ DV translation (ADR 0016).
#
# - translation_glossary: precomputed EN↔DV term pairs mined from the
#   paired-articles corpus via a one-shot LLM extraction job. The
#   translator injects relevant rows into every translation prompt so
#   technical/political terminology stays consistent.
# - translation_runs: every translation invocation persists here.
#   Doubles as the LRU-cache backing store (same input + target_lang
#   → fast return without a fresh LLM call). Also the audit trail.
V2_SLICE16_TRANSLATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS translation_glossary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    en_term         TEXT NOT NULL,
    dv_term         TEXT NOT NULL,
    domain          TEXT,
    freq            INTEGER NOT NULL,
    confidence      REAL,
    sample_en_ids   TEXT,
    extracted_at    TEXT NOT NULL,
    extracted_by    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_glossary_en
    ON translation_glossary(en_term);
CREATE INDEX IF NOT EXISTS idx_glossary_dv
    ON translation_glossary(dv_term);

CREATE TABLE IF NOT EXISTS translation_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_lang         TEXT NOT NULL,
    target_lang         TEXT NOT NULL,
    input_text          TEXT NOT NULL,
    output_text         TEXT NOT NULL,
    exemplar_ids        TEXT,
    glossary_terms_used INTEGER,
    model               TEXT NOT NULL,
    cost_usd            REAL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_translation_runs_cache
    ON translation_runs(target_lang, created_at);
"""

V2_SLICE16_MIGRATIONS: list = []  # no ALTERs needed; pure CREATE TABLE

VALID_VERDICT_LABELS = frozenset({
    "SUPPORTED", "REFUTED", "NOT_ENOUGH_EVIDENCE", "CONFLICTING_EVIDENCE",
})

VALID_TRUTH_LABELS = frozenset({
    "TRUE", "MOSTLY_TRUE", "HALF_TRUE",
    "MOSTLY_FALSE", "FALSE", "PANTS_ON_FIRE",
})
V2_SLICE3_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_embeddings (
    claim_id INTEGER PRIMARY KEY REFERENCES claims(id),
    vector BLOB NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matching_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    claims_embedded INTEGER DEFAULT 0,
    pairs_compared INTEGER DEFAULT 0,
    pairs_matched INTEGER DEFAULT 0,
    llm_tiebreakers INTEGER DEFAULT 0,
    embed_tokens INTEGER DEFAULT 0,
    embed_cost_usd REAL DEFAULT 0,
    llm_cost_usd REAL DEFAULT 0,
    embed_model TEXT,
    llm_model TEXT,
    status TEXT DEFAULT 'running',
    error_message TEXT
);
"""


def init_claims_schema(conn: sqlite3.Connection) -> None:
    """Bootstrap the full kahzaabu schema on `conn`. Idempotent.

    Despite the historic name, this is the canonical schema-init entrypoint
    — it covers fact_checks, fact_check_evidence, qna_sessions,
    manifesto_promises, manifesto_runs, scrape_runs, the *_runs audit
    tables for each pipeline stage, web_users / corrections, plus the
    article_fact_cards + dv_en_inconsistencies side tables. As of
    6394848 it also calls `kahzaabu.constitution.init_constitution_schema`
    so the `constitution_articles` table (and FTS5 index when available)
    exists everywhere a DB is first touched.

    If you want a schema-init that skips the constitution piece (e.g. a
    minimal worker), call the smaller sub-functions directly instead.

    Safe to call repeatedly: every CREATE uses IF NOT EXISTS, every ALTER
    is wrapped in try/except OperationalError.
    """
    conn.executescript(CLAIMS_SCHEMA)
    conn.executescript(V2_SLICE2_SCHEMA)
    conn.executescript(V2_SLICE3_SCHEMA)
    conn.executescript(V2_SLICE4_SCHEMA)
    conn.executescript(V2_SLICE14_AMBIENT_HOOK_SCHEMA)
    conn.executescript(V2_SLICE15_REVISIONS_SCHEMA)
    conn.executescript(V2_SLICE16_TRANSLATION_SCHEMA)
    # Apply phase-3 ALTERs + V2 migrations idempotently
    for sql in (PUBLISH_MIGRATIONS + V2_SLICE1_MIGRATIONS
                + V2_SLICE3_MIGRATIONS + V2_SLICE5_MIGRATIONS
                + V2_SLICE6_MIGRATIONS + V2_SLICE_REGISTRY_MIGRATIONS
                + V2_SLICE12_MIGRATIONS
                + V2_SLICE15_MIGRATIONS
                + V2_SLICE16_MIGRATIONS):
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column/index already exists
    conn.commit()
    # Constitution piece — lazy import to avoid a top-of-module cycle.
    try:
        from kahzaabu.constitution import init_constitution_schema
        init_constitution_schema(conn)
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(
            "constitution schema init skipped: %s", e)
    # fact_checks FTS5 piece — same pattern. If the FTS table was
    # just created (or empty), backfill from the existing rows so a
    # search works immediately without waiting for the next pipeline
    # run. The trigger keeps it in sync after that.
    try:
        from kahzaabu.factcheck_search import (
            init_fact_checks_fts, backfill_fact_checks_fts,
        )
        if init_fact_checks_fts(conn):
            cnt = conn.execute(
                "SELECT COUNT(*) FROM fact_checks_fts").fetchone()[0]
            if cnt == 0:
                backfill_fact_checks_fts(conn)
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(
            "fact_checks FTS5 init skipped: %s", e)
    # articles FTS5 (Slice 16) — same pattern. Used by the press-
    # office-style translator's few-shot exemplar selection.
    # Backfill on first init only (the 20k-article scan takes ~30s
    # on a fresh DB; on subsequent boots the table is already there).
    try:
        from kahzaabu.articles_fts import (
            init_articles_fts, backfill_articles_fts,
        )
        if init_articles_fts(conn):
            cnt = conn.execute(
                "SELECT COUNT(*) FROM articles_fts").fetchone()[0]
            if cnt == 0:
                backfill_articles_fts(conn)
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(
            "articles FTS5 init skipped: %s", e)


def init_full_schema(conn: sqlite3.Connection) -> None:
    """Single entry point that brings up the entire kahzaabu schema —
    V1 tables (articles, scrape/extraction/curation/verification/
    inspection runs, manifesto, web users, corrections, etc.) plus
    every V2 enrichment table and migration plus the constitution.

    Consolidates `kahzaabu.db.init_db(conn)` + `init_claims_schema(conn)`
    into one call so tests, scripts, and new modules can bring up a
    complete schema without remembering the historical split.

    Both sub-functions are idempotent. Existing callers of either
    legacy entry point continue to work.
    """
    # Lazy import: kahzaabu.db imports this module, so a top-level
    # import would create a cycle.
    from kahzaabu import db as _db
    _db.init_db(conn)
    init_claims_schema(conn)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- extraction runs ----------

def start_extraction_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO extraction_runs (started_at) VALUES (?)", (now_iso(),)
    )
    conn.commit()
    return cur.lastrowid


def finish_extraction_run(conn: sqlite3.Connection, run_id: int, *,
                          articles_processed: int = 0, claims_extracted: int = 0,
                          errors: int = 0, tokens_in: int = 0, tokens_out: int = 0,
                          cost_usd: float = 0.0, status: str = "completed",
                          error_message: Optional[str] = None) -> None:
    conn.execute(
        """UPDATE extraction_runs
           SET finished_at = ?, articles_processed = ?, claims_extracted = ?,
               errors = ?, tokens_in = ?, tokens_out = ?, cost_usd = ?,
               status = ?, error_message = ?
           WHERE id = ?""",
        (now_iso(), articles_processed, claims_extracted, errors,
         tokens_in, tokens_out, cost_usd, status, error_message, run_id),
    )
    conn.commit()


# ---------- claims ----------

VALID_POLARITIES = frozenset({
    "AFFIRM", "DENY", "PROMISE", "DENIAL_OF_PROMISE",
    "CLAIM_OF_FACT", "NEUTRAL",
})


def insert_claims(conn: sqlite3.Connection, run_id: int, article_id: int,
                  language: str, claims: list[dict]) -> int:
    """Insert claims. Backward-compatible — V2 fields (polarity,
    subject_normalized, is_checkable) are optional; missing keys store
    NULL. Per ADR 0002, polarity values are validated against
    VALID_POLARITIES — invalid labels are coerced to NULL rather than
    silently stored, so a regressed LLM doesn't poison the table.
    """
    now = now_iso()

    def _polarity(c):
        p = c.get("polarity")
        if p is None:
            return None
        p = str(p).strip().upper().replace(" ", "_")
        return p if p in VALID_POLARITIES else None

    def _is_checkable(c):
        v = c.get("is_checkable")
        if v is None:
            return None
        # accept bool, int, or str ("true"/"false"); coerce to 0/1
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, int):
            return 1 if v else 0
        if isinstance(v, str):
            return 1 if v.lower() in ("true", "yes", "1") else 0
        return None

    rows = [
        (article_id, language, run_id, c.get("type"), c.get("subject"),
         c.get("value"), c.get("deadline"), c.get("actor_credited"),
         c.get("quote"), now,
         _polarity(c), c.get("subject_normalized"), _is_checkable(c))
        for c in claims
    ]
    conn.executemany(
        """INSERT INTO claims
           (article_id, language, extraction_run_id, type, subject, value,
            deadline, actor_credited, quote, created_at,
            polarity, subject_normalized, is_checkable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,  ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def article_has_claims(conn: sqlite3.Connection, article_id: int, language: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM claims WHERE article_id = ? AND language = ? LIMIT 1",
        (article_id, language),
    ).fetchone()
    return r is not None


# ──────────────────────────────────────────────────────────────────────────
# V2 Slice 2 — Q&A decomposition CRUD
# ──────────────────────────────────────────────────────────────────────────

def start_decomposition_run(conn: sqlite3.Connection, model: str) -> int:
    cur = conn.execute(
        "INSERT INTO decomposition_runs (started_at, model) VALUES (?, ?)",
        (now_iso(), model),
    )
    conn.commit()
    return cur.lastrowid


def finish_decomposition_run(conn: sqlite3.Connection, run_id: int, *,
                              claims_processed: int, questions_generated: int,
                              errors: int, tokens_in: int, tokens_out: int,
                              cost_usd: float,
                              status: str = "completed",
                              error_message: Optional[str] = None) -> None:
    conn.execute(
        """UPDATE decomposition_runs SET finished_at=?, claims_processed=?,
           questions_generated=?, errors=?, tokens_in=?, tokens_out=?,
           cost_usd=?, status=?, error_message=? WHERE id=?""",
        (now_iso(), claims_processed, questions_generated, errors,
         tokens_in, tokens_out, cost_usd, status, error_message, run_id),
    )
    conn.commit()


def insert_claim_questions(conn: sqlite3.Connection, run_id: int,
                            claim_id: int, questions: list[dict]) -> int:
    """Persist decomposed sub-questions for a claim. Each `questions`
    item must have at minimum `question` (string). All other fields are
    optional and will be NULL until verification fills them."""
    now = now_iso()
    rows = []
    for q in questions:
        if not q.get("question"):
            continue
        at = q.get("answer_type")
        if at is not None and at not in VALID_ANSWER_TYPES:
            at = None
        sm = q.get("source_medium")
        if sm is not None and sm not in VALID_SOURCE_MEDIUMS:
            sm = None
        rows.append((
            claim_id, q["question"], q.get("answer"), at,
            q.get("source_url"), sm, q.get("confidence"),
            run_id, q.get("answered_at"), now,
        ))
    if not rows:
        return 0
    conn.executemany(
        """INSERT INTO claim_questions
           (claim_id, question, answer, answer_type, source_url,
            source_medium, confidence, decomposition_run_id,
            answered_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def start_matching_run(conn: sqlite3.Connection, *,
                        embed_model: str, llm_model: str) -> int:
    cur = conn.execute(
        "INSERT INTO matching_runs (started_at, embed_model, llm_model) "
        "VALUES (?, ?, ?)",
        (now_iso(), embed_model, llm_model),
    )
    conn.commit()
    return cur.lastrowid


def finish_matching_run(conn: sqlite3.Connection, run_id: int, *,
                         claims_embedded: int = 0, pairs_compared: int = 0,
                         pairs_matched: int = 0, llm_tiebreakers: int = 0,
                         embed_tokens: int = 0, embed_cost_usd: float = 0.0,
                         llm_cost_usd: float = 0.0,
                         status: str = "completed",
                         error_message: Optional[str] = None) -> None:
    conn.execute(
        """UPDATE matching_runs
           SET finished_at=?, claims_embedded=?, pairs_compared=?,
               pairs_matched=?, llm_tiebreakers=?,
               embed_tokens=?, embed_cost_usd=?, llm_cost_usd=?,
               status=?, error_message=?
           WHERE id=?""",
        (now_iso(), claims_embedded, pairs_compared, pairs_matched,
         llm_tiebreakers, embed_tokens, embed_cost_usd, llm_cost_usd,
         status, error_message, run_id),
    )
    conn.commit()


def get_claim_embedding(conn: sqlite3.Connection,
                         claim_id: int) -> Optional[tuple]:
    """Return (vector_bytes, model, dim) or None."""
    r = conn.execute(
        "SELECT vector, model, dim FROM claim_embeddings WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()
    if r is None:
        return None
    return (r["vector"] if hasattr(r, "keys") else r[0],
            r["model"] if hasattr(r, "keys") else r[1],
            r["dim"] if hasattr(r, "keys") else r[2])


def upsert_claim_embedding(conn: sqlite3.Connection, claim_id: int,
                            vector_bytes: bytes, model: str, dim: int) -> None:
    conn.execute(
        """INSERT INTO claim_embeddings
           (claim_id, vector, model, dim, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(claim_id) DO UPDATE SET
               vector=excluded.vector, model=excluded.model,
               dim=excluded.dim, created_at=excluded.created_at""",
        (claim_id, vector_bytes, model, dim, now_iso()),
    )
    conn.commit()


def set_canonical(conn: sqlite3.Connection, claim_id: int,
                   canonical_id: int) -> None:
    """Point claim_id at canonical_id. The canonical claim points at
    itself (canonical_claim_id = id)."""
    conn.execute(
        "UPDATE claims SET canonical_claim_id = ? WHERE id = ?",
        (canonical_id, claim_id),
    )
    conn.commit()


def claims_missing_embedding(conn: sqlite3.Connection, *,
                              language: str = "EN",
                              limit: Optional[int] = None
                              ) -> list[sqlite3.Row]:
    """Checkable, non-sentinel claims that don't yet have an embedding."""
    sql = """
        SELECT c.id, c.quote, c.subject, c.subject_normalized,
               c.polarity, c.type
        FROM claims c
        LEFT JOIN claim_embeddings ce ON ce.claim_id = c.id
        WHERE c.language = ?
          AND ce.claim_id IS NULL
          AND c.type IS NOT NULL
          AND c.type != 'no_specific_claims'
          AND c.quote IS NOT NULL
        ORDER BY c.id
    """
    params: list = [language]
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql, params))


def claims_missing_decomposition(conn: sqlite3.Connection, *,
                                  language: str = "EN",
                                  is_checkable: Optional[int] = None,
                                  limit: Optional[int] = None
                                  ) -> list[sqlite3.Row]:
    """Claims that have no claim_questions row yet. Filter to checkable
    only when desired (NULL is_checkable means 'not yet enriched' — we
    still decompose them since legacy claims pre-date the enrichment
    pass)."""
    sql = """
        SELECT c.id, c.article_id, c.language, c.type, c.subject,
               c.value, c.deadline, c.quote, c.polarity,
               c.subject_normalized, c.is_checkable,
               a.title, a.published_date, a.category
        FROM claims c
        JOIN articles a ON a.id = c.article_id AND a.language = c.language
        LEFT JOIN claim_questions cq ON cq.claim_id = c.id
        WHERE c.language = ?
          AND cq.id IS NULL
          AND c.type IS NOT NULL
          AND c.type != 'no_specific_claims'
    """
    params: list = [language]
    if is_checkable is not None:
        sql += " AND (c.is_checkable IS NULL OR c.is_checkable = ?)"
        params.append(is_checkable)
    sql += " GROUP BY c.id ORDER BY c.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql, params))


def articles_missing_claims(conn: sqlite3.Connection, *, category_in: tuple[str, ...] = ("press_release", "speech", "vp_speech"),
                            language: str = "EN", since_date: Optional[str] = None,
                            limit: Optional[int] = None) -> list[sqlite3.Row]:
    """Return article rows that have NO claims row yet."""
    placeholders = ",".join("?" * len(category_in))
    params: list[Any] = list(category_in) + [language]
    sql = f"""
        SELECT a.id, a.category, a.title, a.body_text, a.published_date, a.language
        FROM articles a
        LEFT JOIN claims c ON c.article_id = a.id AND c.language = a.language
        WHERE a.category IN ({placeholders})
          AND a.language = ?
          AND a.body_text IS NOT NULL AND a.body_text != ''
          AND c.id IS NULL
    """
    if since_date:
        sql += " AND a.published_date >= ?"
        params.append(since_date)
    sql += " ORDER BY a.published_date, a.id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_claims_for_article(conn: sqlite3.Connection, article_id: int,
                           language: str = "EN") -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM claims WHERE article_id = ? AND language = ? ORDER BY id",
        (article_id, language),
    ).fetchall()


def get_claims_since(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM claims WHERE created_at >= ? ORDER BY created_at, id",
        (since_iso,),
    ).fetchall()


# ---------- fact_checks ----------

def make_fingerprint(category: str, claim_date: str, claim: str) -> str:
    return f"{category}|{claim_date[:10]}|{claim.lower()[:100]}".strip()


def start_curation_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO curation_runs (started_at) VALUES (?)", (now_iso(),)
    )
    conn.commit()
    return cur.lastrowid


def finish_curation_run(conn: sqlite3.Connection, run_id: int, *,
                        chunks_processed: int = 0, new_items: int = 0,
                        tokens_in: int = 0, tokens_out: int = 0,
                        cost_usd: float = 0.0, status: str = "completed",
                        error_message: Optional[str] = None) -> None:
    conn.execute(
        """UPDATE curation_runs
           SET finished_at = ?, chunks_processed = ?, new_items = ?,
               tokens_in = ?, tokens_out = ?, cost_usd = ?, status = ?, error_message = ?
           WHERE id = ?""",
        (now_iso(), chunks_processed, new_items, tokens_in, tokens_out,
         cost_usd, status, error_message, run_id),
    )
    conn.commit()


def insert_fact_check(conn: sqlite3.Connection, item: dict, *,
                      run_id: Optional[int] = None, source: str = "auto") -> Optional[int]:
    """Insert a fact-check; returns inserted id or None if duplicate (fingerprint UNIQUE)."""
    category = item.get("category") or "UNCLASSIFIED"
    claim_date = (item.get("claim_date") or item.get("date") or "")[:10]
    claim = item.get("claim") or ""
    fp = make_fingerprint(category, claim_date, claim)
    src_ids = item.get("source_article_ids") or []
    quotes = item.get("evidence_quotes") or []
    try:
        cur = conn.execute(
            """INSERT INTO fact_checks
               (category, claim_date, claim, what_actually_happened, type,
                source_article_ids, evidence_quotes, topic, confidence,
                source, curation_run_id, fingerprint, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                category, claim_date, claim, item.get("what_actually_happened"),
                item.get("type"),
                json.dumps(src_ids), json.dumps(quotes),
                item.get("topic") or item.get("_topic"),
                item.get("confidence") or "auto",
                source, run_id, fp, now_iso(),
            ),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # duplicate fingerprint


def all_fact_checks(conn: sqlite3.Connection, *, confidence: Optional[str] = None,
                    category: Optional[str] = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM fact_checks WHERE 1=1"
    params: list[Any] = []
    if confidence:
        sql += " AND confidence = ?"
        params.append(confidence)
    if category:
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY claim_date DESC, id"
    return conn.execute(sql, params).fetchall()


def fact_check_by_fingerprint(conn: sqlite3.Connection, fingerprint: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM fact_checks WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()


# ---------- budget / stats ----------

def total_cost_since(conn: sqlite3.Connection, since_iso: str) -> float:
    total = 0.0
    for tbl in ("extraction_runs", "curation_runs", "verification_runs",
                "inspection_runs", "dv_compare_runs", "manifesto_runs"):
        try:
            total += conn.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0) FROM {tbl} WHERE started_at >= ?",
                (since_iso,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass  # table may not exist yet
    return float(total)


def daily_spend(conn: sqlite3.Connection, day_iso: Optional[str] = None) -> float:
    """Sum cost_usd from runs started today (UTC) — used for budget cap."""
    if day_iso is None:
        day_iso = datetime.now(timezone.utc).date().isoformat()
    start = f"{day_iso}T00:00:00+00:00"
    end = f"{day_iso}T23:59:59+00:00"
    total = 0.0
    for tbl in ("extraction_runs", "curation_runs", "verification_runs",
                "inspection_runs", "dv_compare_runs", "manifesto_runs"):
        try:
            total += conn.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0) FROM {tbl} WHERE started_at BETWEEN ? AND ?",
                (start, end),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
    return float(total)


# ---------- verification runs ----------

def start_verification_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO verification_runs (started_at) VALUES (?)", (now_iso(),)
    )
    conn.commit()
    return cur.lastrowid


def finish_verification_run(conn: sqlite3.Connection, run_id: int, *,
                            items_processed: int = 0, evidence_collected: int = 0,
                            tokens_in: int = 0, tokens_out: int = 0,
                            web_searches: int = 0, cost_usd: float = 0.0,
                            status: str = "completed",
                            error_message: Optional[str] = None) -> None:
    conn.execute(
        """UPDATE verification_runs
           SET finished_at = ?, items_processed = ?, evidence_collected = ?,
               tokens_in = ?, tokens_out = ?, web_searches = ?, cost_usd = ?,
               status = ?, error_message = ?
           WHERE id = ?""",
        (now_iso(), items_processed, evidence_collected, tokens_in, tokens_out,
         web_searches, cost_usd, status, error_message, run_id),
    )
    conn.commit()


def insert_evidence(conn: sqlite3.Connection, fact_check_id: int, *,
                    source_type: str = "web", url: Optional[str] = None,
                    title: Optional[str] = None, snippet: Optional[str] = None,
                    relevance: str = "unclear", summary: Optional[str] = None,
                    verification_run_id: Optional[int] = None) -> int:
    # ADR 0011: auto-tag evidence whose URL is on a registered
    # public-sector domain. Lazy import keeps the DB module
    # standalone for tests that don't need the registry.
    authoritative_entity_id: Optional[str] = None
    if url:
        try:
            from kahzaabu.registry import entity_for_url
            ent = entity_for_url(url)
            if ent:
                authoritative_entity_id = ent["entity_id"]
        except Exception:
            authoritative_entity_id = None
    cur = conn.execute(
        """INSERT INTO fact_check_evidence
           (fact_check_id, source_type, url, title, snippet, relevance, summary,
            retrieved_at, verification_run_id, authoritative_entity_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (fact_check_id, source_type, url, title, snippet, relevance, summary,
         now_iso(), verification_run_id, authoritative_entity_id),
    )
    conn.commit()
    return cur.lastrowid


def fact_checks_needing_verification(conn: sqlite3.Connection, *,
                                     categories: tuple[str, ...] = (
                                         "LIE", "CONTRADICTION",
                                         "SHIFTING NUMBERS", "CREDIT THEFT",
                                     ),
                                     limit: Optional[int] = None) -> list[sqlite3.Row]:
    """Return fact_checks in given categories that have no evidence rows yet."""
    placeholders = ",".join("?" * len(categories))
    sql = f"""
        SELECT f.*
        FROM fact_checks f
        LEFT JOIN fact_check_evidence e ON e.fact_check_id = f.id
        WHERE f.category IN ({placeholders})
          AND e.id IS NULL
        ORDER BY f.claim_date DESC, f.id
    """
    params: list = list(categories)
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def evidence_for(conn: sqlite3.Connection, fact_check_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM fact_check_evidence WHERE fact_check_id = ? ORDER BY id",
        (fact_check_id,),
    ).fetchall()


# ---------- inspection runs / article_fact_cards ----------

def start_inspection_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO inspection_runs (started_at) VALUES (?)", (now_iso(),)
    )
    conn.commit()
    return cur.lastrowid


def finish_inspection_run(conn: sqlite3.Connection, run_id: int, **kwargs) -> None:
    kwargs["finished_at"] = now_iso()
    kwargs.setdefault("status", "completed")
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    conn.execute(f"UPDATE inspection_runs SET {cols} WHERE id = ?",
                 list(kwargs.values()) + [run_id])
    conn.commit()


def upsert_fact_card(conn: sqlite3.Connection, *, article_id: int, language: str,
                     summary: Optional[str], key_claims: Optional[list],
                     history_check: Optional[str], web_evidence: Optional[list],
                     severity: Optional[str], visualization_spec: Optional[dict],
                     cost_usd: float = 0.0, run_id: Optional[int] = None) -> int:
    """Insert or replace a fact card for a given article+language."""
    conn.execute(
        """INSERT INTO article_fact_cards
           (article_id, language, summary, key_claims_json, history_check,
            web_evidence_json, severity, visualization_spec_json,
            cost_usd, inspection_run_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(article_id, language) DO UPDATE SET
             summary = excluded.summary,
             key_claims_json = excluded.key_claims_json,
             history_check = excluded.history_check,
             web_evidence_json = excluded.web_evidence_json,
             severity = excluded.severity,
             visualization_spec_json = excluded.visualization_spec_json,
             cost_usd = excluded.cost_usd,
             inspection_run_id = excluded.inspection_run_id,
             created_at = excluded.created_at""",
        (
            article_id, language, summary,
            json.dumps(key_claims or [], ensure_ascii=False),
            history_check,
            json.dumps(web_evidence or [], ensure_ascii=False),
            severity,
            json.dumps(visualization_spec or {}, ensure_ascii=False),
            cost_usd, run_id, now_iso(),
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM article_fact_cards WHERE article_id = ? AND language = ?",
        (article_id, language),
    ).fetchone()
    return row[0] if row else 0


def get_fact_card(conn: sqlite3.Connection, article_id: int, language: str = "EN") -> Optional[dict]:
    r = conn.execute(
        "SELECT * FROM article_fact_cards WHERE article_id = ? AND language = ?",
        (article_id, language),
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("key_claims_json", "web_evidence_json", "visualization_spec_json"):
        try:
            d[k.replace("_json", "")] = json.loads(d.get(k) or ("[]" if k != "visualization_spec_json" else "{}"))
        except Exception:
            d[k.replace("_json", "")] = [] if k != "visualization_spec_json" else {}
    return d


def articles_missing_fact_card(conn: sqlite3.Connection, *,
                                category_in: tuple = ("press_release", "speech", "vp_speech"),
                                language: str = "EN",
                                since_date: Optional[str] = "2023-11-17",
                                limit: Optional[int] = None) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(category_in))
    params: list[Any] = list(category_in) + [language]
    sql = f"""
        SELECT a.id, a.category, a.title, a.body_text, a.published_date, a.language
        FROM articles a
        LEFT JOIN article_fact_cards fc
          ON fc.article_id = a.id AND fc.language = a.language
        WHERE a.category IN ({placeholders})
          AND a.language = ?
          AND a.body_text IS NOT NULL AND a.body_text != ''
          AND fc.id IS NULL
    """
    if since_date:
        sql += " AND a.published_date >= ?"
        params.append(since_date)
    sql += " ORDER BY a.published_date DESC, a.id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


# ---------- dv_compare runs / inconsistencies ----------

def start_dv_compare_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO dv_compare_runs (started_at) VALUES (?)", (now_iso(),)
    )
    conn.commit()
    return cur.lastrowid


def finish_dv_compare_run(conn: sqlite3.Connection, run_id: int, **kwargs) -> None:
    kwargs["finished_at"] = now_iso()
    kwargs.setdefault("status", "completed")
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    conn.execute(f"UPDATE dv_compare_runs SET {cols} WHERE id = ?",
                 list(kwargs.values()) + [run_id])
    conn.commit()


def insert_dv_inconsistency(conn: sqlite3.Connection, *, en_article_id: int,
                            dv_article_id: int, severity: str, category: str,
                            en_quote: Optional[str], dv_quote: Optional[str],
                            dv_translation_to_en: Optional[str],
                            explanation: Optional[str],
                            run_id: Optional[int] = None) -> int:
    cur = conn.execute(
        """INSERT INTO dv_en_inconsistencies
           (en_article_id, dv_article_id, severity, category, en_quote, dv_quote,
            dv_translation_to_en, explanation, dv_compare_run_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (en_article_id, dv_article_id, severity, category, en_quote, dv_quote,
         dv_translation_to_en, explanation, run_id, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def record_dv_pair(conn: sqlite3.Connection, *, en_article_id: int, dv_article_id: int,
                   n_inconsistencies: int, max_severity: Optional[str],
                   run_id: Optional[int], cost_usd: float) -> None:
    conn.execute(
        """INSERT INTO dv_compare_pairs
           (en_article_id, dv_article_id, n_inconsistencies, max_severity,
            compared_at, dv_compare_run_id, cost_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(en_article_id) DO UPDATE SET
             dv_article_id = excluded.dv_article_id,
             n_inconsistencies = excluded.n_inconsistencies,
             max_severity = excluded.max_severity,
             compared_at = excluded.compared_at,
             dv_compare_run_id = excluded.dv_compare_run_id,
             cost_usd = excluded.cost_usd""",
        (en_article_id, dv_article_id, n_inconsistencies, max_severity,
         now_iso(), run_id, cost_usd),
    )
    conn.commit()


# ---------- Publish workflow (CLI-only; no web admin) ----------
#
# The legacy web_users table CREATE remains in CLAIMS_SCHEMA for
# backwards-compat with existing DBs that already have it. No code
# reads or writes the table anymore. `create_user` / `get_user` /
# `update_user_password` were removed when the password-based admin
# auth was deleted; operator actions now happen via this CLI on the
# operator's filesystem, gated by OS permissions rather than an
# in-app credential.


def set_fact_check_published(conn: sqlite3.Connection, fc_id: int, *,
                             published: bool, reviewed_by: Optional[str],
                             public_summary: Optional[str] = None) -> int:
    sets = ["published = ?", "reviewed_at = ?", "reviewed_by = ?"]
    params: list[Any] = [1 if published else 0, now_iso(), reviewed_by]
    if public_summary is not None:
        sets.append("public_summary = ?")
        params.append(public_summary)
    params.append(fc_id)
    cur = conn.execute(
        f"UPDATE fact_checks SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    conn.commit()
    return cur.rowcount


# ---------- qna sessions (agentic conversation memory) ----------

def get_qna_session(conn: sqlite3.Connection, session_id: str) -> Optional[dict]:
    r = conn.execute(
        "SELECT * FROM qna_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["messages"] = json.loads(d["messages_json"] or "[]")
    except Exception:
        d["messages"] = []
    return d


def most_recent_session_id(conn: sqlite3.Connection,
                            max_age_hours: float = 24.0) -> Optional[str]:
    """Return the id of the qna_session most recently updated within
    max_age_hours (default 24h). Used by `--continue` style affordances —
    CLI and the /kahzaabu slash command — to pick up the last conversation
    automatically. Returns None if no recent session exists.
    """
    try:
        row = conn.execute(
            "SELECT id FROM qna_sessions "
            "WHERE last_used_at >= datetime('now', ?) "
            "ORDER BY last_used_at DESC LIMIT 1",
            (f"-{max_age_hours} hours",),
        ).fetchone()
        return row["id"] if row else None
    except sqlite3.OperationalError:
        return None


def save_qna_session(conn: sqlite3.Connection, session_id: str, messages: list,
                      cost_usd: float, n_turns: Optional[int] = None) -> None:
    now = now_iso()
    existing = conn.execute(
        "SELECT id, total_cost_usd, n_turns FROM qna_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if existing:
        new_cost = float(existing["total_cost_usd"] or 0) + cost_usd
        new_turns = (existing["n_turns"] or 0) + (n_turns or 1)
        conn.execute(
            """UPDATE qna_sessions SET messages_json = ?, total_cost_usd = ?,
                                        n_turns = ?, last_used_at = ?
               WHERE id = ?""",
            (json.dumps(messages, ensure_ascii=False), new_cost, new_turns, now,
             session_id),
        )
    else:
        conn.execute(
            """INSERT INTO qna_sessions (id, messages_json, total_cost_usd, n_turns,
                                          created_at, last_used_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, json.dumps(messages, ensure_ascii=False), cost_usd,
             n_turns or 1, now, now),
        )
    conn.commit()


def prune_old_qna_sessions(conn: sqlite3.Connection, days: int = 30) -> int:
    """Delete sessions not touched in N days."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cur = conn.execute("DELETE FROM qna_sessions WHERE last_used_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def insert_correction(conn: sqlite3.Connection, *, body: str,
                      fact_check_id: Optional[int] = None,
                      article_id: Optional[int] = None,
                      reporter_contact: Optional[str] = None) -> int:
    cur = conn.execute(
        """INSERT INTO corrections
           (fact_check_id, article_id, reporter_contact, body, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (fact_check_id, article_id, reporter_contact, body, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def pairs_missing_dv_compare(conn: sqlite3.Connection, *,
                             since_date: Optional[str] = "2024-01-01",
                             require_claims: bool = True,
                             limit: Optional[int] = None) -> list[sqlite3.Row]:
    """Find EN articles with paired DV (both bodies) not yet compared.

    If require_claims=True, only include EN articles that have extracted claims
    (i.e. claim-bearing — most informative).
    """
    sql = """
        SELECT en.id  AS en_article_id, en.title AS en_title, en.published_date,
               dv.id  AS dv_article_id, en.body_text AS en_body, dv.body_text AS dv_body
        FROM articles en
        JOIN articles dv ON en.paired_id = dv.id
        LEFT JOIN dv_compare_pairs p ON p.en_article_id = en.id
        WHERE en.language = 'EN' AND dv.language = 'DV'
          AND en.body_text IS NOT NULL AND en.body_text != ''
          AND dv.body_text IS NOT NULL AND dv.body_text != ''
          AND en.category = 'press_release'
          AND p.en_article_id IS NULL
    """
    params: list = []
    if since_date:
        sql += " AND en.published_date >= ?"
        params.append(since_date)
    if require_claims:
        sql += """ AND EXISTS (
            SELECT 1 FROM claims c
            WHERE c.article_id = en.id AND c.language = 'EN'
              AND c.type != 'no_specific_claims'
        )"""
    sql += " ORDER BY en.published_date DESC, en.id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def last_scrape_time(conn: sqlite3.Connection) -> Optional[str]:
    """Most recent successful scrape's finished_at, or fall back to articles.scraped_at."""
    r = conn.execute(
        """SELECT MAX(finished_at) FROM scrape_runs
           WHERE status = 'completed' AND finished_at IS NOT NULL"""
    ).fetchone()
    if r and r[0]:
        return r[0]
    # Fallback: latest article scraped_at
    r = conn.execute(
        "SELECT MAX(scraped_at) FROM articles WHERE scraped_at IS NOT NULL"
    ).fetchone()
    return r[0] if r and r[0] else None


def freshness(conn: sqlite3.Connection, stale_hours: float = 24.0) -> dict:
    """Returns last-scrape timestamp + hours-since + is_stale flag."""
    last = last_scrape_time(conn)
    out = {
        "last_scrape_at": last,
        "hours_since": None,
        "is_stale": False,
        "threshold_hours": stale_hours,
    }
    if not last:
        out["is_stale"] = True
        return out
    try:
        # Tolerate Z, +00:00, fractional seconds
        s = last.replace("Z", "+00:00")
        ts = datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        hours = (now - ts).total_seconds() / 3600
        out["hours_since"] = round(hours, 2)
        out["is_stale"] = hours >= stale_hours
    except Exception:
        out["is_stale"] = True
    return out


def stats(conn: sqlite3.Connection) -> dict:
    n_claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    n_fc = conn.execute("SELECT COUNT(*) FROM fact_checks").fetchone()[0]
    n_articles_with_claims = conn.execute(
        "SELECT COUNT(DISTINCT article_id || '|' || language) FROM claims"
    ).fetchone()[0]
    n_articles_total = conn.execute(
        """SELECT COUNT(*) FROM articles
           WHERE language='EN' AND body_text IS NOT NULL AND body_text != ''
             AND category IN ('press_release','speech','vp_speech')
             AND published_date >= '2023-11-17'"""
    ).fetchone()[0]
    last_ext = conn.execute(
        "SELECT * FROM extraction_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_cur = conn.execute(
        "SELECT * FROM curation_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "n_claims": n_claims,
        "n_articles_with_claims": n_articles_with_claims,
        "n_articles_muizzu_total": n_articles_total,
        "coverage_pct": round(n_articles_with_claims / n_articles_total * 100, 1) if n_articles_total else 0,
        "n_fact_checks": n_fc,
        "last_extraction": dict(last_ext) if last_ext else None,
        "last_curation": dict(last_cur) if last_cur else None,
    }
