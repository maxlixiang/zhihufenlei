from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .db import Database
from .deepseek import DeepSeekClient
from .metadata import validate_metadata
from .prompts import PROMPT_VERSION, system_prompt, user_prompt
from .taxonomy import Taxonomy


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_article(path: Path, max_chars: int) -> str:
    text = path.read_text(encoding="utf-8-sig")
    if len(text) <= max_chars:
        return text
    head = text[: int(max_chars * 0.75)]
    tail = text[-int(max_chars * 0.25) :]
    return head + "\n\n[正文过长，中间部分在本次请求中省略]\n\n" + tail


def classify_pending(
    database: Database,
    taxonomy: Taxonomy,
    client: DeepSeekClient,
    *,
    limit: int,
    max_chars: int,
) -> dict[str, int]:
    now = utc_now()
    processed = 0
    errors = 0
    with database.connect() as connection:
        run_id = connection.execute(
            """
            INSERT INTO classification_runs (
                started_at, taxonomy_version, prompt_version, provider, model,
                status, requested_limit
            ) VALUES (?, ?, ?, 'deepseek', ?, 'running', ?)
            """,
            (now, taxonomy.version, PROMPT_VERSION, client.model, limit),
        ).lastrowid
        connection.commit()

        rows = connection.execute(
            """
            SELECT a.* FROM articles a
            WHERE a.read_error IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM classifications c
                WHERE c.article_id = a.id
                  AND c.content_hash = a.content_hash
                  AND c.taxonomy_version = ?
                  AND c.prompt_version = ?
                  AND c.model = ?
                  AND c.error IS NULL
              )
            ORDER BY a.id
            LIMIT ?
            """,
            (taxonomy.version, PROMPT_VERSION, client.model, limit),
        ).fetchall()

        system = system_prompt(taxonomy)
        for article in rows:
            created_at = utc_now()
            raw_envelope: dict[str, Any] | None = None
            try:
                content = _read_article(Path(article["source_path"]), max_chars)
                payload, raw_envelope = client.classify(
                    system_prompt=system,
                    user_prompt=user_prompt(article["title"], article["relative_path"], content),
                )
                metadata = validate_metadata(payload, taxonomy)
                connection.execute(
                    """
                    INSERT INTO classifications (
                        article_id, run_id, content_hash, taxonomy_version, prompt_version,
                        provider, model, level1_id, level1_name, level2_name, tags_json,
                        summary, purpose, reason, confidence, needs_review,
                        candidate_new_topic_json, raw_response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'deepseek', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        article["id"], run_id, article["content_hash"], taxonomy.version,
                        PROMPT_VERSION, client.model, metadata.level1_id, metadata.level1_name,
                        metadata.level2_name, json.dumps(metadata.tags, ensure_ascii=False),
                        metadata.summary, metadata.purpose, metadata.reason, metadata.confidence,
                        int(metadata.needs_review),
                        json.dumps(metadata.candidate_new_topic, ensure_ascii=False)
                        if metadata.candidate_new_topic is not None else None,
                        json.dumps(raw_envelope, ensure_ascii=False), created_at,
                    ),
                )
                processed += 1
            except Exception as exc:
                errors += 1
                connection.execute(
                    """
                    INSERT INTO classifications (
                        article_id, run_id, content_hash, taxonomy_version, prompt_version,
                        provider, model, needs_review, raw_response_json, error, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'deepseek', ?, 1, ?, ?, ?)
                    """,
                    (
                        article["id"], run_id, article["content_hash"], taxonomy.version,
                        PROMPT_VERSION, client.model,
                        json.dumps(raw_envelope, ensure_ascii=False) if raw_envelope else None,
                        str(exc), created_at,
                    ),
                )
            connection.commit()

        status = "completed_with_errors" if errors else "completed"
        connection.execute(
            """
            UPDATE classification_runs
            SET completed_at=?, status=?, processed_count=?, error_count=? WHERE id=?
            """,
            (utc_now(), status, processed, errors, run_id),
        )
    return {"selected": len(rows), "processed": processed, "errors": errors}
