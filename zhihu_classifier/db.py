from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL UNIQUE,
    relative_path TEXT NOT NULL,
    title TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    read_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_articles_content_hash ON articles(content_hash);

CREATE TABLE IF NOT EXISTS classification_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    taxonomy_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_limit INTEGER,
    processed_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS classifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    run_id INTEGER NOT NULL REFERENCES classification_runs(id),
    content_hash TEXT NOT NULL,
    taxonomy_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    level1_id TEXT,
    level1_name TEXT,
    level2_name TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT,
    purpose TEXT,
    reason TEXT,
    confidence REAL,
    needs_review INTEGER NOT NULL DEFAULT 1,
    candidate_new_topic_json TEXT,
    raw_response_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_classifications_article ON classifications(article_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_classifications_review ON classifications(needs_review, id DESC);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    analysis_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_limit INTEGER,
    processed_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS article_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    run_id INTEGER REFERENCES analysis_runs(id),
    content_hash TEXT NOT NULL,
    analysis_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    summary_short TEXT,
    summary_detailed TEXT,
    primary_purpose TEXT,
    main_topics_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    key_points_json TEXT NOT NULL DEFAULT '[]',
    entities_json TEXT NOT NULL DEFAULT '[]',
    content_type TEXT,
    scope TEXT,
    actionability TEXT,
    time_sensitivity TEXT,
    candidate_new_topics_json TEXT NOT NULL DEFAULT '[]',
    raw_response_json TEXT,
    error TEXT,
    legacy_classification_id INTEGER UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_article_analyses_current
ON article_analyses(article_id, content_hash, id DESC);

CREATE TABLE IF NOT EXISTS assignment_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    taxonomy_version TEXT NOT NULL,
    assignment_prompt_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_limit INTEGER,
    processed_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS category_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    analysis_id INTEGER NOT NULL REFERENCES article_analyses(id),
    run_id INTEGER REFERENCES assignment_runs(id),
    taxonomy_version TEXT NOT NULL,
    assignment_prompt_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    source TEXT NOT NULL,
    level1_id TEXT,
    level1_name TEXT,
    level2_name TEXT,
    alternatives_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT,
    confidence REAL,
    needs_review INTEGER NOT NULL DEFAULT 1,
    candidate_new_topics_json TEXT NOT NULL DEFAULT '[]',
    raw_response_json TEXT,
    error TEXT,
    legacy_classification_id INTEGER UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_category_assignments_current
ON category_assignments(article_id, taxonomy_version, id DESC);
CREATE INDEX IF NOT EXISTS idx_category_assignments_review
ON category_assignments(needs_review, id DESC);

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phase TEXT NOT NULL,
    article_id INTEGER REFERENCES articles(id),
    source_table TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(source_table, source_id)
);

CREATE TABLE IF NOT EXISTS copy_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    analysis_run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    taxonomy_version TEXT NOT NULL,
    destination_root TEXT NOT NULL,
    requested_count INTEGER NOT NULL,
    copied_count INTEGER NOT NULL DEFAULT 0,
    existing_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS copy_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    copy_run_id INTEGER NOT NULL REFERENCES copy_runs(id),
    article_id INTEGER NOT NULL REFERENCES articles(id),
    analysis_id INTEGER NOT NULL REFERENCES article_analyses(id),
    assignment_id INTEGER NOT NULL REFERENCES category_assignments(id),
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    disposition TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(copy_run_id, article_id)
);

CREATE TABLE IF NOT EXISTS copy_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    copy_run_id INTEGER NOT NULL REFERENCES copy_runs(id),
    article_id INTEGER NOT NULL REFERENCES articles(id),
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    disposition TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(copy_run_id, source_path)
);

CREATE TABLE IF NOT EXISTS copy_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    copy_run_id INTEGER NOT NULL REFERENCES copy_runs(id),
    article_id INTEGER NOT NULL REFERENCES articles(id),
    source_content_hash TEXT NOT NULL,
    output_content_hash TEXT NOT NULL,
    frontmatter_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(copy_run_id, article_id)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_legacy_classifications(connection)
            self._backfill_api_usage(connection)

    @staticmethod
    def _usage_values(raw_response_json: str | None) -> tuple[int, int, int, int, int] | None:
        if not raw_response_json:
            return None
        try:
            usage = json.loads(raw_response_json).get("usage") or {}
            return (
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("prompt_cache_hit_tokens") or 0),
                int(usage.get("prompt_cache_miss_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
            )
        except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
            return None

    @classmethod
    def _backfill_api_usage(cls, connection: sqlite3.Connection) -> None:
        sources = (
            (
                "legacy_full_analysis", "classifications",
                "SELECT id, article_id, provider, model, raw_response_json, created_at "
                "FROM classifications WHERE error IS NULL",
            ),
            (
                "full_analysis", "article_analyses",
                "SELECT id, article_id, provider, model, raw_response_json, created_at "
                "FROM article_analyses WHERE run_id IS NOT NULL",
            ),
            (
                "semantic_remap", "category_assignments",
                "SELECT id, article_id, provider, model, raw_response_json, created_at "
                "FROM category_assignments WHERE error IS NULL AND source = 'semantic_remap'",
            ),
        )
        for phase, table, query in sources:
            for row in connection.execute(query).fetchall():
                usage = cls._usage_values(row["raw_response_json"])
                if usage is None:
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO api_usage (
                        phase, article_id, source_table, source_id, provider, model,
                        prompt_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                        completion_tokens, total_tokens, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        phase, row["article_id"], table, row["id"], row["provider"],
                        row["model"], *usage, row["created_at"],
                    ),
                )

    @staticmethod
    def _migrate_legacy_classifications(connection: sqlite3.Connection) -> None:
        """把旧试跑成功结果迁移为语义档案和目录映射；不读取原文。"""
        rows = connection.execute(
            """
            SELECT c.* FROM classifications c
            WHERE c.error IS NULL
              AND c.id = (
                SELECT c2.id FROM classifications c2
                WHERE c2.article_id = c.article_id AND c2.error IS NULL
                ORDER BY c2.id DESC LIMIT 1
              )
              AND NOT EXISTS (
                SELECT 1 FROM article_analyses aa
                WHERE aa.legacy_classification_id = c.id
              )
            ORDER BY c.id
            """
        ).fetchall()
        for row in rows:
            candidate = row["candidate_new_topic_json"] or "[]"
            if candidate.strip().startswith("{"):
                candidate = "[" + candidate + "]"
            analysis_id = connection.execute(
                """
                INSERT INTO article_analyses (
                    article_id, content_hash, analysis_version, prompt_version,
                    provider, model, summary_short, summary_detailed, primary_purpose,
                    main_topics_json, tags_json, key_points_json, entities_json,
                    content_type, scope, actionability, time_sensitivity,
                    candidate_new_topics_json, raw_response_json,
                    legacy_classification_id, created_at
                ) VALUES (?, ?, 'legacy-1', ?, ?, ?, ?, ?, ?, '[]', ?, '[]', '[]',
                          '', '', '', '', ?, ?, ?, ?)
                """,
                (
                    row["article_id"], row["content_hash"], row["prompt_version"],
                    row["provider"], row["model"], row["summary"], row["summary"],
                    row["purpose"], row["tags_json"], candidate,
                    row["raw_response_json"], row["id"], row["created_at"],
                ),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO category_assignments (
                    article_id, analysis_id, taxonomy_version, assignment_prompt_version,
                    provider, model, source, level1_id, level1_name, level2_name,
                    alternatives_json, reason, confidence, needs_review,
                    candidate_new_topics_json, raw_response_json,
                    legacy_classification_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'legacy_migration', ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["article_id"], analysis_id, row["taxonomy_version"],
                    row["prompt_version"], row["provider"], row["model"],
                    row["level1_id"], row["level1_name"], row["level2_name"],
                    row["reason"], row["confidence"], row["needs_review"],
                    candidate, row["raw_response_json"], row["id"], row["created_at"],
                ),
            )

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            articles = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            history = connection.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
            analysis_base = """
                FROM article_analyses aa JOIN articles a ON a.id = aa.article_id
                WHERE aa.id = (
                    SELECT aa2.id FROM article_analyses aa2
                    WHERE aa2.article_id = aa.article_id
                      AND aa2.content_hash = a.content_hash
                    ORDER BY aa2.id DESC LIMIT 1
                )
            """
            analyses = connection.execute("SELECT COUNT(*) " + analysis_base).fetchone()[0]
            analysis_errors = connection.execute(
                "SELECT COUNT(*) " + analysis_base + " AND aa.error IS NOT NULL"
            ).fetchone()[0]
            assignment_base = """
                FROM category_assignments ca
                WHERE ca.id = (
                    SELECT ca2.id FROM category_assignments ca2
                    WHERE ca2.article_id = ca.article_id
                    ORDER BY ca2.id DESC LIMIT 1
                )
            """
            assignments = connection.execute("SELECT COUNT(*) " + assignment_base).fetchone()[0]
            review = connection.execute(
                "SELECT COUNT(*) " + assignment_base + " AND ca.needs_review = 1"
            ).fetchone()[0]
            assignment_errors = connection.execute(
                "SELECT COUNT(*) " + assignment_base + " AND ca.error IS NOT NULL"
            ).fetchone()[0]
            usage = connection.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(prompt_cache_hit_tokens), 0) AS cache_hit,
                       COALESCE(SUM(prompt_cache_miss_tokens), 0) AS cache_miss,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM api_usage
                """
            ).fetchone()
        return {
            "articles": articles,
            "current_analyses": analyses,
            "pending_full_text_analysis": articles - analyses,
            "current_analysis_errors": analysis_errors,
            "latest_assignments": assignments,
            "latest_needs_review": review,
            "latest_assignment_errors": assignment_errors,
            "legacy_classification_history_records": history,
            "api_calls_recorded": usage["calls"],
            "api_prompt_tokens": usage["prompt_tokens"],
            "api_cache_hit_tokens": usage["cache_hit"],
            "api_cache_miss_tokens": usage["cache_miss"],
            "api_completion_tokens": usage["completion_tokens"],
            "api_total_tokens": usage["total_tokens"],
        }
