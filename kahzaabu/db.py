from __future__ import annotations

import json
import sqlite3
from typing import Optional, List
from datetime import datetime, timezone
from pathlib import Path

from .models import Article

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "kahzaabu.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER NOT NULL,
    language TEXT NOT NULL,
    paired_id INTEGER,
    category TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    body_text TEXT,
    body_html TEXT,
    reference TEXT,
    published_date TEXT,
    image_urls TEXT,
    scraped_at TEXT NOT NULL,
    raw_page_html TEXT,
    PRIMARY KEY (id, language)
);

CREATE INDEX IF NOT EXISTS idx_articles_category ON articles(category);
CREATE INDEX IF NOT EXISTS idx_articles_language ON articles(language);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_date);
CREATE INDEX IF NOT EXISTS idx_articles_paired ON articles(paired_id);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    category_id INTEGER NOT NULL,
    language TEXT NOT NULL,
    pages_scraped INTEGER DEFAULT 0,
    articles_scraped INTEGER DEFAULT 0,
    articles_new INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    error_message TEXT,
    resume_page INTEGER DEFAULT 1
);
"""


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def article_exists(conn: sqlite3.Connection, article_id: int, language: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM articles WHERE id = ? AND language = ?",
        (article_id, language),
    ).fetchone()
    return row is not None


def insert_article(conn: sqlite3.Connection, article: Article) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO articles
        (id, language, paired_id, category, category_id, title,
         body_text, body_html, reference, published_date,
         image_urls, scraped_at, raw_page_html)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            article.id,
            article.language,
            article.paired_id,
            article.category,
            article.category_id,
            article.title,
            article.body_text,
            article.body_html,
            article.reference,
            article.published_date,
            json.dumps(article.image_urls),
            now,
            article.raw_page_html,
        ),
    )
    conn.commit()


def start_scrape_run(
    conn: sqlite3.Connection, category_id: int, language: str, resume_page: int = 1
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO scrape_runs (started_at, category_id, language, resume_page)
        VALUES (?, ?, ?, ?)""",
        (now, category_id, language, resume_page),
    )
    conn.commit()
    return cursor.lastrowid


def update_scrape_run(conn: sqlite3.Connection, run_id: int, **kwargs) -> None:
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values())
    vals.append(run_id)
    conn.execute(f"UPDATE scrape_runs SET {sets} WHERE id = ?", vals)
    conn.commit()


def finish_scrape_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str = "completed",
    error_message: Optional[str] = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE scrape_runs SET finished_at = ?, status = ?, error_message = ? WHERE id = ?",
        (now, status, error_message, run_id),
    )
    conn.commit()


def get_last_run(
    conn: sqlite3.Connection, category_id: int, language: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM scrape_runs
        WHERE category_id = ? AND language = ?
        ORDER BY id DESC LIMIT 1""",
        (category_id, language),
    ).fetchone()


def get_stats(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        """SELECT category, language, COUNT(*) as count,
        MIN(published_date) as earliest, MAX(published_date) as latest
        FROM articles GROUP BY category, language
        ORDER BY category, language"""
    ).fetchall()


def search_articles(
    conn: sqlite3.Connection, query: str, limit: int = 50
) -> List[sqlite3.Row]:
    return conn.execute(
        """SELECT id, language, category, title, published_date,
        SUBSTR(body_text, 1, 200) as snippet
        FROM articles
        WHERE body_text LIKE ? OR title LIKE ?
        ORDER BY published_date DESC LIMIT ?""",
        (f"%{query}%", f"%{query}%", limit),
    ).fetchall()


def get_article(
    conn: sqlite3.Connection, article_id: int, language: str = "EN"
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM articles WHERE id = ? AND language = ?",
        (article_id, language),
    ).fetchone()


def export_articles(
    conn: sqlite3.Connection, category: Optional[str] = None, language: Optional[str] = None
) -> List[sqlite3.Row]:
    query = "SELECT id, language, paired_id, category, title, body_text, reference, published_date FROM articles WHERE 1=1"
    params = []
    if category:
        query += " AND category = ?"
        params.append(category)
    if language:
        query += " AND language = ?"
        params.append(language)
    query += " ORDER BY published_date DESC"
    return conn.execute(query, params).fetchall()
