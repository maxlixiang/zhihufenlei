from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .db import Database
from .deepseek import DeepSeekClient
from .metadata import validate_assignment
from .prompts import (
    analysis_system_prompt,
    analysis_user_prompt,
    remap_system_prompt,
    remap_user_prompt,
)
from .preprocess import prepare_article_for_model
from .semantic import (
    ANALYSIS_PROMPT_VERSION,
    ANALYSIS_VERSION,
    ASSIGNMENT_PROMPT_VERSION,
    ArticleAnalysis,
    validate_full_response,
)
from .taxonomy import Taxonomy


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_full_article(path: Path, max_chars: int) -> str:
    text = path.read_text(encoding="utf-8-sig")
    if len(text) > max_chars:
        raise ValueError(
            f"文章共 {len(text)} 字符，超过单次完整分析上限 {max_chars}；"
            "为保证全文只读一次，本次不截断，请提高 --max-chars 或实现分块分析。"
        )
    return text


def _analysis_dict(analysis: ArticleAnalysis) -> dict[str, Any]:
    return {
        "summary_short": analysis.summary_short,
        "summary_detailed": analysis.summary_detailed,
        "primary_purpose": analysis.primary_purpose,
        "main_topics": list(analysis.main_topics),
        "tags": list(analysis.tags),
        "key_points": list(analysis.key_points),
        "entities": list(analysis.entities),
        "content_type": analysis.content_type,
        "scope": analysis.scope,
        "actionability": analysis.actionability,
        "time_sensitivity": analysis.time_sensitivity,
        "candidate_new_topics": list(analysis.candidate_new_topics),
    }


def _insert_assignment(
    connection,
    *,
    article_id: int,
    analysis_id: int,
    run_id: int | None,
    taxonomy: Taxonomy,
    client: DeepSeekClient,
    source: str,
    assignment,
    raw_response: dict[str, Any] | None,
    created_at: str,
) -> int:
    return connection.execute(
        """
        INSERT INTO category_assignments (
            article_id, analysis_id, run_id, taxonomy_version,
            assignment_prompt_version, provider, model, source,
            level1_id, level1_name, level2_name, alternatives_json,
            reason, confidence, needs_review, candidate_new_topics_json,
            raw_response_json, created_at
        ) VALUES (?, ?, ?, ?, ?, 'deepseek', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article_id, analysis_id, run_id, taxonomy.version,
            ASSIGNMENT_PROMPT_VERSION, client.model, source,
            assignment.level1_id, assignment.level1_name, assignment.level2_name,
            json.dumps(assignment.alternatives, ensure_ascii=False), assignment.reason,
            assignment.confidence, int(assignment.needs_review),
            json.dumps(assignment.candidate_new_topics, ensure_ascii=False),
            json.dumps(raw_response, ensure_ascii=False) if raw_response else None,
            created_at,
        ),
    ).lastrowid


def _record_usage(
    connection,
    *,
    phase: str,
    article_id: int,
    source_table: str,
    source_id: int,
    client: DeepSeekClient,
    raw_response: dict[str, Any] | None,
    created_at: str,
) -> None:
    if not raw_response or not isinstance(raw_response.get("usage"), dict):
        return
    usage = raw_response["usage"]
    connection.execute(
        """
        INSERT OR IGNORE INTO api_usage (
            phase, article_id, source_table, source_id, provider, model,
            prompt_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens,
            completion_tokens, total_tokens, created_at
        ) VALUES (?, ?, ?, ?, 'deepseek', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            phase, article_id, source_table, source_id, client.model,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("prompt_cache_hit_tokens") or 0),
            int(usage.get("prompt_cache_miss_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            int(usage.get("total_tokens") or 0), created_at,
        ),
    )


def analyze_pending(
    database: Database,
    taxonomy: Taxonomy,
    client: DeepSeekClient,
    *,
    limit: int,
    max_chars: int,
) -> dict[str, int]:
    processed = 0
    errors = 0
    with database.connect() as connection:
        run_id = connection.execute(
            """
            INSERT INTO analysis_runs (
                started_at, analysis_version, prompt_version, provider,
                model, status, requested_limit
            ) VALUES (?, ?, ?, 'deepseek', ?, 'running', ?)
            """,
            (utc_now(), ANALYSIS_VERSION, ANALYSIS_PROMPT_VERSION, client.model, limit),
        ).lastrowid
        connection.commit()
        rows = connection.execute(
            """
            SELECT a.* FROM articles a
            WHERE a.read_error IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM article_analyses aa
                WHERE aa.article_id = a.id
                  AND aa.content_hash = a.content_hash
                  AND aa.error IS NULL
              )
            ORDER BY a.id LIMIT ?
            """,
            (limit,),
        ).fetchall()
        system = analysis_system_prompt(taxonomy)
        for article in rows:
            created_at = utc_now()
            raw_envelope = None
            try:
                content = prepare_article_for_model(
                    _read_full_article(Path(article["source_path"]), max_chars)
                )
                payload, raw_envelope = client.complete_json(
                    system_prompt=system,
                    user_prompt=analysis_user_prompt(
                        article["title"], article["relative_path"], content
                    ),
                )
                analysis, assignment = validate_full_response(payload, taxonomy)
                values = _analysis_dict(analysis)
                analysis_id = connection.execute(
                    """
                    INSERT INTO article_analyses (
                        article_id, run_id, content_hash, analysis_version,
                        prompt_version, provider, model, summary_short,
                        summary_detailed, primary_purpose, main_topics_json,
                        tags_json, key_points_json, entities_json, content_type,
                        scope, actionability, time_sensitivity,
                        candidate_new_topics_json, raw_response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'deepseek', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        article["id"], run_id, article["content_hash"], ANALYSIS_VERSION,
                        ANALYSIS_PROMPT_VERSION, client.model, values["summary_short"],
                        values["summary_detailed"], values["primary_purpose"],
                        json.dumps(values["main_topics"], ensure_ascii=False),
                        json.dumps(values["tags"], ensure_ascii=False),
                        json.dumps(values["key_points"], ensure_ascii=False),
                        json.dumps(values["entities"], ensure_ascii=False),
                        values["content_type"], values["scope"], values["actionability"],
                        values["time_sensitivity"],
                        json.dumps(values["candidate_new_topics"], ensure_ascii=False),
                        json.dumps(raw_envelope, ensure_ascii=False), created_at,
                    ),
                ).lastrowid
                _insert_assignment(
                    connection, article_id=article["id"], analysis_id=analysis_id,
                    run_id=None, taxonomy=taxonomy, client=client, source="initial_analysis",
                    assignment=assignment, raw_response=raw_envelope, created_at=created_at,
                )
                _record_usage(
                    connection, phase="full_analysis", article_id=article["id"],
                    source_table="article_analyses", source_id=analysis_id,
                    client=client, raw_response=raw_envelope, created_at=created_at,
                )
                processed += 1
            except Exception as exc:
                errors += 1
                failed_analysis_id = connection.execute(
                    """
                    INSERT INTO article_analyses (
                        article_id, run_id, content_hash, analysis_version,
                        prompt_version, provider, model, raw_response_json,
                        error, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'deepseek', ?, ?, ?, ?)
                    """,
                    (
                        article["id"], run_id, article["content_hash"], ANALYSIS_VERSION,
                        ANALYSIS_PROMPT_VERSION, client.model,
                        json.dumps(raw_envelope, ensure_ascii=False) if raw_envelope else None,
                        str(exc), created_at,
                    ),
                ).lastrowid
                _record_usage(
                    connection, phase="full_analysis", article_id=article["id"],
                    source_table="article_analyses", source_id=failed_analysis_id,
                    client=client, raw_response=raw_envelope, created_at=created_at,
                )
            connection.commit()
        connection.execute(
            """
            UPDATE analysis_runs SET completed_at=?, status=?, processed_count=?, error_count=?
            WHERE id=?
            """,
            (utc_now(), "completed_with_errors" if errors else "completed", processed, errors, run_id),
        )
    return {"run_id": run_id, "selected": len(rows), "processed": processed, "errors": errors}


def _row_to_profile(row) -> dict[str, Any]:
    return {
        "summary_short": row["summary_short"],
        "summary_detailed": row["summary_detailed"],
        "primary_purpose": row["primary_purpose"],
        "main_topics": json.loads(row["main_topics_json"] or "[]"),
        "tags": json.loads(row["tags_json"] or "[]"),
        "key_points": json.loads(row["key_points_json"] or "[]"),
        "entities": json.loads(row["entities_json"] or "[]"),
        "content_type": row["content_type"],
        "scope": row["scope"],
        "actionability": row["actionability"],
        "time_sensitivity": row["time_sensitivity"],
        "candidate_new_topics": json.loads(row["candidate_new_topics_json"] or "[]"),
        "analysis_version": row["analysis_version"],
    }


def remap_pending(
    database: Database,
    taxonomy: Taxonomy,
    client: DeepSeekClient,
    *,
    limit: int,
) -> dict[str, int]:
    processed = 0
    errors = 0
    with database.connect() as connection:
        run_id = connection.execute(
            """
            INSERT INTO assignment_runs (
                started_at, taxonomy_version, assignment_prompt_version,
                provider, model, source, status, requested_limit
            ) VALUES (?, ?, ?, 'deepseek', ?, 'semantic_remap', 'running', ?)
            """,
            (utc_now(), taxonomy.version, ASSIGNMENT_PROMPT_VERSION, client.model, limit),
        ).lastrowid
        connection.commit()
        rows = connection.execute(
            """
            SELECT aa.*, a.title FROM article_analyses aa
            JOIN articles a ON a.id = aa.article_id
            WHERE aa.error IS NULL
              AND aa.content_hash = a.content_hash
              AND aa.id = (
                SELECT aa2.id FROM article_analyses aa2
                WHERE aa2.article_id = aa.article_id
                  AND aa2.content_hash = a.content_hash
                  AND aa2.error IS NULL
                ORDER BY aa2.id DESC LIMIT 1
              )
              AND NOT EXISTS (
                SELECT 1 FROM category_assignments ca
                WHERE ca.analysis_id = aa.id
                  AND ca.taxonomy_version = ?
                  AND ca.assignment_prompt_version = ?
                  AND ca.error IS NULL
              )
            ORDER BY aa.article_id LIMIT ?
            """,
            (taxonomy.version, ASSIGNMENT_PROMPT_VERSION, limit),
        ).fetchall()
        system = remap_system_prompt(taxonomy)
        for row in rows:
            created_at = utc_now()
            raw_envelope = None
            try:
                profile_json = json.dumps(_row_to_profile(row), ensure_ascii=False, indent=2)
                payload, raw_envelope = client.complete_json(
                    system_prompt=system,
                    user_prompt=remap_user_prompt(row["title"], profile_json),
                )
                assignment = validate_assignment(payload, taxonomy)
                assignment_id = _insert_assignment(
                    connection, article_id=row["article_id"], analysis_id=row["id"],
                    run_id=run_id, taxonomy=taxonomy, client=client, source="semantic_remap",
                    assignment=assignment, raw_response=raw_envelope, created_at=created_at,
                )
                _record_usage(
                    connection, phase="semantic_remap", article_id=row["article_id"],
                    source_table="category_assignments", source_id=assignment_id,
                    client=client, raw_response=raw_envelope, created_at=created_at,
                )
                processed += 1
            except Exception as exc:
                errors += 1
                connection.execute(
                    """
                    INSERT INTO category_assignments (
                        article_id, analysis_id, run_id, taxonomy_version,
                        assignment_prompt_version, provider, model, source,
                        needs_review, raw_response_json, error, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'deepseek', ?, 'semantic_remap', 1, ?, ?, ?)
                    """,
                    (
                        row["article_id"], row["id"], run_id, taxonomy.version,
                        ASSIGNMENT_PROMPT_VERSION, client.model,
                        json.dumps(raw_envelope, ensure_ascii=False) if raw_envelope else None,
                        str(exc), created_at,
                    ),
                )
            connection.commit()
        connection.execute(
            """
            UPDATE assignment_runs SET completed_at=?, status=?, processed_count=?, error_count=?
            WHERE id=?
            """,
            (utc_now(), "completed_with_errors" if errors else "completed", processed, errors, run_id),
        )
    return {"selected": len(rows), "processed": processed, "errors": errors}
