from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .db import Database
from .deepseek import DeepSeekClient
from .metadata import validate_assignment
from .prompts import (
    analysis_repair_system_prompt,
    analysis_repair_user_prompt,
    analysis_system_prompt,
    analysis_user_prompt,
    candidate_recheck_system_prompt,
    candidate_recheck_user_prompt,
    remap_system_prompt,
    remap_user_prompt,
)
from .preprocess import prepare_article_for_model
from .semantic import (
    ANALYSIS_PROMPT_VERSION,
    ANALYSIS_REPAIR_PROMPT_VERSION,
    ANALYSIS_VERSION,
    ASSIGNMENT_PROMPT_VERSION,
    CANDIDATE_RECHECK_PROMPT_VERSION,
    LOCAL_STRUCTURE_REPAIR_VERSION,
    ArticleAnalysis,
    validate_full_response,
)
from .taxonomy import Taxonomy

ProgressCallback = Callable[[dict[str, Any]], None]
HUMAN_CARRY_FORWARD_VERSION = "1"

_ANALYSIS_FIELDS = {
    "summary_short",
    "summary_detailed",
    "primary_purpose",
    "main_topics",
    "tags",
    "key_points",
    "entities",
    "content_type",
    "scope",
    "actionability",
    "time_sensitivity",
    "candidate_new_topics",
}


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


def _normalize_saved_full_response(payload: Any) -> dict[str, Any]:
    """只修正已保存响应的对象层级，不生成、删减或改写语义内容。"""
    if not isinstance(payload, dict):
        raise ValueError("已保存响应的 content 不是 JSON 对象")
    analysis_raw = payload.get("analysis")
    if not isinstance(analysis_raw, dict):
        raise ValueError("已保存响应缺少 analysis 对象")
    analysis = dict(analysis_raw)
    assignment = payload.get("assignment")
    if not isinstance(assignment, dict):
        nested_assignment = analysis.pop("assignment", None)
        if isinstance(nested_assignment, dict):
            assignment = nested_assignment
    for field in _ANALYSIS_FIELDS:
        if field not in analysis and field in payload:
            analysis[field] = payload[field]
    return {"analysis": analysis, "assignment": assignment}


def _normalize_saved_assignment_response(
    payload: Any, taxonomy: Taxonomy
) -> dict[str, Any]:
    """只修正可唯一确定的字段名和目录层级，不猜测新的语义判断。"""
    if not isinstance(payload, dict):
        raise ValueError("已保存映射响应的 content 不是 JSON 对象")
    normalized = dict(payload)

    if "confidence" not in normalized:
        confidence_aliases = [
            key
            for key in normalized
            if str(key).strip().lstrip(",，").strip() == "confidence"
        ]
        if len(confidence_aliases) == 1:
            normalized["confidence"] = normalized[confidence_aliases[0]]

    category_raw = normalized.get("category")
    if not isinstance(category_raw, dict):
        return normalized
    category = dict(category_raw)
    level2_name = str(category.get("level2_name", "")).strip()
    current = taxonomy.category_by_id(str(category.get("level1_id", "")).strip())
    current_contains_level2 = current is not None and level2_name in current.second_level
    if level2_name and not current_contains_level2:
        owners = [item for item in taxonomy.categories if level2_name in item.second_level]
        if len(owners) == 1:
            owner = owners[0]
            category["level1_id"] = owner.id
            category["level1_name"] = owner.name
            normalized["category"] = category
    return normalized


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


def _emit_progress(
    callback: ProgressCallback | None,
    *,
    phase: str,
    current: int,
    total: int,
    processed: int,
    errors: int,
    title: str,
) -> None:
    if callback is None:
        return
    try:
        callback(
            {
                "phase": phase,
                "current": current,
                "total": total,
                "processed": processed,
                "errors": errors,
                "title": title,
            }
        )
    except Exception:
        # 进度显示属于辅助功能，终端关闭或输出失败不能中断昂贵的模型任务。
        return


def analyze_pending(
    database: Database,
    taxonomy: Taxonomy,
    client: DeepSeekClient,
    *,
    limit: int,
    max_chars: int,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    reused = reuse_duplicate_analyses(database, taxonomy)
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
        for index, article in enumerate(rows, start=1):
            created_at = utc_now()
            raw_envelope = None
            try:
                # 同批不同路径的相同内容，也复用刚生成的语义档案。
                if reuse_duplicate_analyses(database, taxonomy, article_id=article["id"]):
                    reused += 1
                    _emit_progress(progress, phase="full_analysis", current=index,
                                   total=len(rows), processed=processed, errors=errors,
                                   title=article["title"])
                    continue
                if hashlib.sha256(Path(article["source_path"]).read_bytes()).hexdigest() != article["content_hash"]:
                    raise ValueError("源文件在扫描后发生变化，请重新扫描")
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
            _emit_progress(
                progress,
                phase="full_analysis",
                current=index,
                total=len(rows),
                processed=processed,
                errors=errors,
                title=article["title"],
            )
        connection.execute(
            """
            UPDATE analysis_runs SET completed_at=?, status=?, processed_count=?, error_count=?
            WHERE id=?
            """,
            (utc_now(), "completed_with_errors" if errors else "completed", processed, errors, run_id),
        )
    return {"run_id": run_id, "selected": len(rows), "processed": processed, "errors": errors, "reused": reused}


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


def repair_failed_analyses(
    database: Database,
    taxonomy: Taxonomy,
    client: DeepSeekClient,
    *,
    limit: int,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """只用已保存的模型响应修复结构错误，不重新读取文章正文。"""
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
            (
                utc_now(),
                ANALYSIS_VERSION,
                f"repair-{ANALYSIS_REPAIR_PROMPT_VERSION}",
                client.model,
                limit,
            ),
        ).lastrowid
        connection.commit()
        rows = connection.execute(
            """
            SELECT aa.*, a.title
            FROM article_analyses aa
            JOIN articles a ON a.id = aa.article_id
            WHERE aa.id = (
                SELECT aa2.id
                FROM article_analyses aa2
                WHERE aa2.article_id = aa.article_id
                  AND aa2.content_hash = a.content_hash
                ORDER BY aa2.id DESC
                LIMIT 1
            )
              AND aa.error IS NOT NULL
              AND aa.raw_response_json IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM article_analyses ok
                WHERE ok.article_id = aa.article_id
                  AND ok.content_hash = a.content_hash
                  AND ok.error IS NULL
              )
            ORDER BY aa.article_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        system = analysis_repair_system_prompt(taxonomy)
        for index, row in enumerate(rows, start=1):
            created_at = utc_now()
            raw_envelope = None
            previous_envelope = None
            try:
                previous_envelope = json.loads(row["raw_response_json"])
                previous_payload = previous_envelope.get("content")
                if not isinstance(previous_payload, dict):
                    raise ValueError("已保存响应缺少可修复的 content JSON")
                payload, raw_envelope = client.complete_json(
                    system_prompt=system,
                    user_prompt=analysis_repair_user_prompt(
                        row["title"],
                        row["error"],
                        json.dumps(previous_payload, ensure_ascii=False, indent=2),
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
                        row["article_id"], run_id, row["content_hash"], ANALYSIS_VERSION,
                        f"repair-{ANALYSIS_REPAIR_PROMPT_VERSION}", client.model,
                        values["summary_short"], values["summary_detailed"],
                        values["primary_purpose"],
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
                    connection,
                    article_id=row["article_id"],
                    analysis_id=analysis_id,
                    run_id=None,
                    taxonomy=taxonomy,
                    client=client,
                    source="analysis_repair",
                    assignment=assignment,
                    raw_response=raw_envelope,
                    created_at=created_at,
                )
                _record_usage(
                    connection,
                    phase="analysis_repair",
                    article_id=row["article_id"],
                    source_table="article_analyses",
                    source_id=analysis_id,
                    client=client,
                    raw_response=raw_envelope,
                    created_at=created_at,
                )
                processed += 1
            except Exception as exc:
                errors += 1
                failed_id = connection.execute(
                    """
                    INSERT INTO article_analyses (
                        article_id, run_id, content_hash, analysis_version,
                        prompt_version, provider, model, raw_response_json,
                        error, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'deepseek', ?, ?, ?, ?)
                    """,
                    (
                        row["article_id"], run_id, row["content_hash"], ANALYSIS_VERSION,
                        f"repair-{ANALYSIS_REPAIR_PROMPT_VERSION}", client.model,
                        json.dumps(
                            raw_envelope or previous_envelope,
                            ensure_ascii=False,
                        )
                        if raw_envelope or previous_envelope
                        else None,
                        str(exc), created_at,
                    ),
                ).lastrowid
                _record_usage(
                    connection,
                    phase="analysis_repair",
                    article_id=row["article_id"],
                    source_table="article_analyses",
                    source_id=failed_id,
                    client=client,
                    raw_response=raw_envelope,
                    created_at=created_at,
                )
            connection.commit()
            _emit_progress(
                progress,
                phase="analysis_repair",
                current=index,
                total=len(rows),
                processed=processed,
                errors=errors,
                title=row["title"],
            )
        connection.execute(
            """
            UPDATE analysis_runs
            SET completed_at=?, status=?, processed_count=?, error_count=?
            WHERE id=?
            """,
            (
                utc_now(),
                "completed_with_errors" if errors else "completed",
                processed,
                errors,
                run_id,
            ),
        )
    return {"run_id": run_id, "selected": len(rows), "processed": processed, "errors": errors}


def repair_saved_analyses_locally(
    database: Database,
    taxonomy: Taxonomy,
    *,
    limit: int,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """从历史响应中选取可验证版本并校正 JSON 层级；不读取正文、不调用 API。"""
    processed = 0
    errors = 0
    model_name = "saved-deepseek-response"
    with database.connect() as connection:
        all_rows = connection.execute(
            """
            SELECT aa.*, a.title
            FROM article_analyses aa
            JOIN articles a ON a.id = aa.article_id
            WHERE aa.error IS NOT NULL
              AND aa.raw_response_json IS NOT NULL
              AND aa.content_hash = a.content_hash
              AND NOT EXISTS (
                SELECT 1 FROM article_analyses ok
                WHERE ok.article_id = aa.article_id
                  AND ok.content_hash = a.content_hash
                  AND ok.error IS NULL
              )
            ORDER BY aa.article_id, aa.id DESC
            """
        ).fetchall()
        grouped: dict[int, list[Any]] = {}
        for row in all_rows:
            grouped.setdefault(row["article_id"], []).append(row)
        article_rows = list(grouped.values())[:limit]
        run_id = connection.execute(
            """
            INSERT INTO analysis_runs (
                started_at, analysis_version, prompt_version, provider,
                model, status, requested_limit
            ) VALUES (?, ?, ?, 'local', ?, 'running', ?)
            """,
            (
                utc_now(),
                ANALYSIS_VERSION,
                f"local-structure-repair-{LOCAL_STRUCTURE_REPAIR_VERSION}",
                model_name,
                limit,
            ),
        ).lastrowid
        connection.commit()

        for index, candidates in enumerate(article_rows, start=1):
            first = candidates[0]
            created_at = utc_now()
            last_error: Exception | None = None
            repaired = False
            for row in candidates:
                try:
                    raw_envelope = json.loads(row["raw_response_json"])
                    normalized = _normalize_saved_full_response(raw_envelope.get("content"))
                    analysis, assignment = validate_full_response(normalized, taxonomy)
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
                            row["article_id"], run_id, row["content_hash"], ANALYSIS_VERSION,
                            f"local-structure-repair-{LOCAL_STRUCTURE_REPAIR_VERSION}",
                            raw_envelope.get("model") or model_name,
                            values["summary_short"], values["summary_detailed"],
                            values["primary_purpose"],
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

                    class SavedResponseClient:
                        model = raw_envelope.get("model") or model_name

                    _insert_assignment(
                        connection,
                        article_id=row["article_id"],
                        analysis_id=analysis_id,
                        run_id=None,
                        taxonomy=taxonomy,
                        client=SavedResponseClient(),
                        source="local_structure_repair",
                        assignment=assignment,
                        raw_response=raw_envelope,
                        created_at=created_at,
                    )
                    processed += 1
                    repaired = True
                    break
                except Exception as exc:
                    last_error = exc
            if not repaired:
                errors += 1
                connection.execute(
                    """
                    INSERT INTO article_analyses (
                        article_id, run_id, content_hash, analysis_version,
                        prompt_version, provider, model, error, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'local', ?, ?, ?)
                    """,
                    (
                        first["article_id"], run_id, first["content_hash"], ANALYSIS_VERSION,
                        f"local-structure-repair-{LOCAL_STRUCTURE_REPAIR_VERSION}",
                        model_name, str(last_error or "没有可验证的已保存响应"), created_at,
                    ),
                )
            connection.commit()
            _emit_progress(
                progress,
                phase="local_structure_repair",
                current=index,
                total=len(article_rows),
                processed=processed,
                errors=errors,
                title=first["title"],
            )
        connection.execute(
            """
            UPDATE analysis_runs
            SET completed_at=?, status=?, processed_count=?, error_count=?
            WHERE id=?
            """,
            (
                utc_now(),
                "completed_with_errors" if errors else "completed",
                processed,
                errors,
                run_id,
            ),
        )
    return {
        "run_id": run_id,
        "selected": len(article_rows),
        "processed": processed,
        "errors": errors,
    }


def repair_saved_assignments_locally(
    database: Database,
    taxonomy: Taxonomy,
    *,
    limit: int,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """解包并验证失败映射的已保存 JSON；不调用 API。"""
    processed = 0
    errors = 0
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT ca.*, a.title,
                   (
                       SELECT aa.id FROM article_analyses aa
                       WHERE aa.article_id = ca.article_id
                         AND aa.content_hash = a.content_hash
                         AND aa.error IS NULL
                       ORDER BY aa.id DESC LIMIT 1
                   ) AS current_analysis_id
            FROM category_assignments ca
            JOIN articles a ON a.id = ca.article_id
            WHERE ca.error IS NOT NULL
              AND ca.raw_response_json IS NOT NULL
              AND ca.id = (
                  SELECT ca2.id FROM category_assignments ca2
                  WHERE ca2.article_id = ca.article_id
                  ORDER BY ca2.id DESC LIMIT 1
              )
            ORDER BY ca.article_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        run_id = connection.execute(
            """
            INSERT INTO assignment_runs (
                started_at, taxonomy_version, assignment_prompt_version,
                provider, model, source, status, requested_limit
            ) VALUES (?, ?, ?, 'local', ?, 'local_structure_repair', 'running', ?)
            """,
            (
                utc_now(),
                taxonomy.version,
                f"local-structure-repair-{LOCAL_STRUCTURE_REPAIR_VERSION}",
                "saved-deepseek-response",
                limit,
            ),
        ).lastrowid
        connection.commit()
        for index, row in enumerate(rows, start=1):
            created_at = utc_now()
            try:
                raw_envelope = json.loads(row["raw_response_json"])
                normalized = _normalize_saved_assignment_response(
                    raw_envelope.get("content"), taxonomy
                )
                assignment = validate_assignment(normalized, taxonomy)

                class SavedResponseClient:
                    model = raw_envelope.get("model") or "saved-deepseek-response"

                _insert_assignment(
                    connection,
                    article_id=row["article_id"],
                    analysis_id=row["current_analysis_id"],
                    run_id=run_id,
                    taxonomy=taxonomy,
                    client=SavedResponseClient(),
                    source="local_structure_repair",
                    assignment=assignment,
                    raw_response=raw_envelope,
                    created_at=created_at,
                )
                processed += 1
            except Exception:
                errors += 1
            connection.commit()
            _emit_progress(
                progress,
                phase="local_structure_repair",
                current=index,
                total=len(rows),
                processed=processed,
                errors=errors,
                title=row["title"],
            )
        connection.execute(
            """
            UPDATE assignment_runs
            SET completed_at=?, status=?, processed_count=?, error_count=?
            WHERE id=?
            """,
            (
                utc_now(),
                "completed_with_errors" if errors else "completed",
                processed,
                errors,
                run_id,
            ),
        )
    return {"run_id": run_id, "selected": len(rows), "processed": processed, "errors": errors}


def recheck_candidate_assignments(
    database: Database,
    taxonomy: Taxonomy,
    client: DeepSeekClient,
    *,
    limit: int,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """只用语义档案复核候选主题，优先映射到已有宽泛目录。"""
    processed = 0
    errors = 0
    with database.connect() as connection:
        run_id = connection.execute(
            """
            INSERT INTO assignment_runs (
                started_at, taxonomy_version, assignment_prompt_version,
                provider, model, source, status, requested_limit
            ) VALUES (?, ?, ?, 'deepseek', ?, 'candidate_recheck', 'running', ?)
            """,
            (
                utc_now(),
                taxonomy.version,
                f"candidate-recheck-{CANDIDATE_RECHECK_PROMPT_VERSION}",
                client.model,
                limit,
            ),
        ).lastrowid
        connection.commit()
        rows = connection.execute(
            """
            SELECT aa.*, a.title,
                   ca.level1_id AS previous_level1_id,
                   ca.level1_name AS previous_level1_name,
                   ca.level2_name AS previous_level2_name,
                   ca.alternatives_json AS previous_alternatives_json,
                   ca.reason AS previous_reason,
                   ca.confidence AS previous_confidence,
                   ca.needs_review AS previous_needs_review,
                   ca.candidate_new_topics_json AS previous_candidates_json
            FROM category_assignments ca
            JOIN articles a ON a.id = ca.article_id
            JOIN article_analyses aa ON aa.id = (
                SELECT aa2.id
                FROM article_analyses aa2
                WHERE aa2.article_id = ca.article_id
                  AND aa2.content_hash = a.content_hash
                  AND aa2.error IS NULL
                ORDER BY aa2.id DESC
                LIMIT 1
            )
            WHERE ca.id = (
                SELECT ca2.id
                FROM category_assignments ca2
                WHERE ca2.article_id = ca.article_id
                ORDER BY ca2.id DESC
                LIMIT 1
            )
              AND ca.error IS NULL
              AND (
                  COALESCE(ca.level1_id, '') = ''
                  OR COALESCE(ca.level2_name, '') = ''
                  OR (
                      ca.candidate_new_topics_json IS NOT NULL
                      AND ca.candidate_new_topics_json NOT IN ('[]', 'null', '')
                  )
              )
            ORDER BY ca.article_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        system = candidate_recheck_system_prompt(taxonomy)
        for index, row in enumerate(rows, start=1):
            created_at = utc_now()
            raw_envelope = None
            previous_assignment = {
                "category": {
                    "level1_id": row["previous_level1_id"] or "",
                    "level1_name": row["previous_level1_name"] or "",
                    "level2_name": row["previous_level2_name"] or "",
                },
                "alternatives": json.loads(row["previous_alternatives_json"] or "[]"),
                "reason": row["previous_reason"] or "",
                "confidence": row["previous_confidence"],
                "needs_review": bool(row["previous_needs_review"]),
                "candidate_new_topics": json.loads(row["previous_candidates_json"] or "[]"),
            }
            try:
                payload, raw_envelope = client.complete_json(
                    system_prompt=system,
                    user_prompt=candidate_recheck_user_prompt(
                        row["title"],
                        json.dumps(_row_to_profile(row), ensure_ascii=False, indent=2),
                        json.dumps(previous_assignment, ensure_ascii=False, indent=2),
                    ),
                )
                assignment = validate_assignment(payload, taxonomy)
                assignment_id = _insert_assignment(
                    connection,
                    article_id=row["article_id"],
                    analysis_id=row["id"],
                    run_id=run_id,
                    taxonomy=taxonomy,
                    client=client,
                    source="candidate_recheck",
                    assignment=assignment,
                    raw_response=raw_envelope,
                    created_at=created_at,
                )
                _record_usage(
                    connection,
                    phase="candidate_recheck",
                    article_id=row["article_id"],
                    source_table="category_assignments",
                    source_id=assignment_id,
                    client=client,
                    raw_response=raw_envelope,
                    created_at=created_at,
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
                    ) VALUES (?, ?, ?, ?, ?, 'deepseek', ?, 'candidate_recheck',
                              1, ?, ?, ?)
                    """,
                    (
                        row["article_id"], row["id"], run_id, taxonomy.version,
                        f"candidate-recheck-{CANDIDATE_RECHECK_PROMPT_VERSION}",
                        client.model,
                        json.dumps(raw_envelope, ensure_ascii=False) if raw_envelope else None,
                        str(exc), created_at,
                    ),
                )
            connection.commit()
            _emit_progress(
                progress,
                phase="candidate_recheck",
                current=index,
                total=len(rows),
                processed=processed,
                errors=errors,
                title=row["title"],
            )
        connection.execute(
            """
            UPDATE assignment_runs
            SET completed_at=?, status=?, processed_count=?, error_count=?
            WHERE id=?
            """,
            (
                utc_now(),
                "completed_with_errors" if errors else "completed",
                processed,
                errors,
                run_id,
            ),
        )
    return {"run_id": run_id, "selected": len(rows), "processed": processed, "errors": errors}


def carry_forward_human_assignments(
    database: Database,
    taxonomy: Taxonomy,
) -> int:
    """把当前有效人工分类本地继承到新规则版本，不调用模型。"""
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT ca.*, a.title
            FROM category_assignments ca
            JOIN articles a ON a.id = ca.article_id
            WHERE ca.id = (
                SELECT ca2.id FROM category_assignments ca2
                WHERE ca2.article_id = ca.article_id
                ORDER BY ca2.id DESC LIMIT 1
            )
              AND ca.error IS NULL
              AND ca.source LIKE 'human_%'
              AND NOT EXISTS (
                  SELECT 1 FROM category_assignments target
                  WHERE target.article_id = ca.article_id
                    AND target.taxonomy_version = ?
                    AND target.source LIKE 'human_%'
                    AND target.error IS NULL
              )
            ORDER BY ca.article_id
            """,
            (taxonomy.version,),
        ).fetchall()
        if not rows:
            return 0
        for row in rows:
            category = taxonomy.category_by_id(row["level1_id"] or "")
            if (
                category is None
                or row["level1_name"] != category.name
                or row["level2_name"] not in category.second_level
            ):
                raise ValueError(
                    f"人工分类无法继承到 {taxonomy.version}："
                    f"{row['title']} → {row['level1_name']}/{row['level2_name']}"
                )
        now = utc_now()
        run_id = connection.execute(
            """
            INSERT INTO assignment_runs (
                started_at, completed_at, taxonomy_version,
                assignment_prompt_version, provider, model, source, status,
                requested_limit, processed_count, error_count
            ) VALUES (?, ?, ?, ?, 'human', 'manual', 'human_carry_forward',
                      'completed', ?, ?, 0)
            """,
            (
                now, now, taxonomy.version,
                f"manual-carry-forward-{HUMAN_CARRY_FORWARD_VERSION}",
                len(rows), len(rows),
            ),
        ).lastrowid
        for row in rows:
            connection.execute(
                """
                INSERT INTO category_assignments (
                    article_id, analysis_id, run_id, taxonomy_version,
                    assignment_prompt_version, provider, model, source,
                    level1_id, level1_name, level2_name, alternatives_json,
                    reason, confidence, needs_review, candidate_new_topics_json,
                    raw_response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, 'human', 'manual', 'human_carry_forward',
                          ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    row["article_id"], row["analysis_id"], run_id, taxonomy.version,
                    f"manual-carry-forward-{HUMAN_CARRY_FORWARD_VERSION}",
                    row["level1_id"], row["level1_name"], row["level2_name"],
                    row["alternatives_json"] or "[]",
                    f"继承人工确认结果：{row['reason'] or '用户确认分类'}",
                    row["confidence"], row["needs_review"],
                    row["candidate_new_topics_json"] or "[]", now,
                ),
            )
        return len(rows)


def remap_pending(
    database: Database,
    taxonomy: Taxonomy,
    client: DeepSeekClient,
    *,
    limit: int,
    progress: ProgressCallback | None = None,
    only_unassigned: bool = False,
) -> dict[str, int]:
    human_carried = carry_forward_human_assignments(database, taxonomy)
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
                  AND ca.error IS NULL
                  AND (
                      ca.assignment_prompt_version = ?
                      OR ca.source LIKE 'human_%'
                      OR ?
                  )
              )
            ORDER BY aa.article_id LIMIT ?
            """,
            (taxonomy.version, ASSIGNMENT_PROMPT_VERSION, int(only_unassigned), limit),
        ).fetchall()
        system = remap_system_prompt(taxonomy)
        for index, row in enumerate(rows, start=1):
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
            _emit_progress(
                progress,
                phase="semantic_remap",
                current=index,
                total=len(rows),
                processed=processed,
                errors=errors,
                title=row["title"],
            )
        connection.execute(
            """
            UPDATE assignment_runs SET completed_at=?, status=?, processed_count=?, error_count=?
            WHERE id=?
            """,
            (utc_now(), "completed_with_errors" if errors else "completed", processed, errors, run_id),
        )
    return {
        "selected": len(rows),
        "processed": processed,
        "errors": errors,
        "human_carried": human_carried,
    }


def reuse_duplicate_analyses(database: Database, taxonomy: Taxonomy, *, article_id: int | None = None) -> int:
    """跨路径复用相同内容的永久档案，不读取全文、不记录虚假的 API 用量。"""
    reused = 0
    with database.connect() as connection:
        rows = connection.execute("""
            SELECT a.* FROM articles a WHERE a.read_error IS NULL
              AND (? IS NULL OR a.id=?)
              AND NOT EXISTS (SELECT 1 FROM article_analyses aa
                  WHERE aa.article_id=a.id AND aa.content_hash=a.content_hash AND aa.error IS NULL)
            ORDER BY a.id
        """, (article_id, article_id)).fetchall()
        analysis_columns = [r[1] for r in connection.execute("PRAGMA table_info(article_analyses)")
                            if r[1] not in {"id", "legacy_classification_id"}]
        assignment_columns = [r[1] for r in connection.execute("PRAGMA table_info(category_assignments)")
                              if r[1] != "id"]
        for article in rows:
            donor = connection.execute("""
                SELECT * FROM article_analyses WHERE content_hash=? AND error IS NULL
                ORDER BY id DESC LIMIT 1
            """, (article["content_hash"],)).fetchone()
            if donor is None:
                continue
            values = dict(donor)
            values.update(article_id=article["id"], run_id=None, raw_response_json=None, created_at=utc_now())
            analysis_id = connection.execute(
                f"INSERT INTO article_analyses ({','.join(analysis_columns)}) VALUES ({','.join('?' for _ in analysis_columns)})",
                tuple(values[c] for c in analysis_columns)).lastrowid
            assignment = connection.execute("""
                SELECT * FROM category_assignments WHERE analysis_id=? AND taxonomy_version=? AND error IS NULL
                ORDER BY id DESC LIMIT 1
            """, (donor["id"], taxonomy.version)).fetchone()
            if assignment is not None:
                values = dict(assignment)
                values.update(article_id=article["id"], analysis_id=analysis_id, run_id=None,
                              provider="local", model="deterministic", source="duplicate_reuse",
                              raw_response_json=None, created_at=utc_now())
                connection.execute(
                    f"INSERT INTO category_assignments ({','.join(assignment_columns)}) VALUES ({','.join('?' for _ in assignment_columns)})",
                    tuple(values[c] for c in assignment_columns))
            reused += 1
    return reused
