from __future__ import annotations

import json
from pathlib import Path

from .db import Database


def export_latest(database: Database, output_path: Path, *, review_only: bool = False) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    where = "AND ca.needs_review = 1" if review_only else ""
    query = f"""
        SELECT a.source_path, a.relative_path, a.title, a.content_hash,
               aa.analysis_version, aa.prompt_version AS analysis_prompt_version,
               aa.provider AS analysis_provider, aa.model AS analysis_model,
               aa.summary_short, aa.summary_detailed, aa.primary_purpose,
               aa.main_topics_json, aa.tags_json, aa.key_points_json,
               aa.entities_json, aa.content_type, aa.scope, aa.actionability,
               aa.time_sensitivity,
               aa.candidate_new_topics_json AS analysis_candidates_json,
               ca.taxonomy_version, ca.assignment_prompt_version,
               ca.provider AS assignment_provider, ca.model AS assignment_model,
               ca.source AS assignment_source, ca.level1_id, ca.level1_name,
               ca.level2_name, ca.alternatives_json, ca.reason, ca.confidence,
               ca.needs_review,
               ca.candidate_new_topics_json AS assignment_candidates_json,
               ca.error, ca.created_at
        FROM category_assignments ca
        JOIN articles a ON a.id = ca.article_id
        JOIN article_analyses aa ON aa.id = ca.analysis_id
        WHERE ca.id = (
            SELECT ca2.id FROM category_assignments ca2
            WHERE ca2.article_id = ca.article_id ORDER BY ca2.id DESC LIMIT 1
        ) {where}
        ORDER BY a.relative_path
    """
    count = 0
    with database.connect() as connection, output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in connection.execute(query):
            item = dict(row)
            item["main_topics"] = json.loads(item.pop("main_topics_json") or "[]")
            item["tags"] = json.loads(item.pop("tags_json") or "[]")
            item["key_points"] = json.loads(item.pop("key_points_json") or "[]")
            item["entities"] = json.loads(item.pop("entities_json") or "[]")
            item["analysis_candidate_new_topics"] = json.loads(
                item.pop("analysis_candidates_json") or "[]"
            )
            item["alternatives"] = json.loads(item.pop("alternatives_json") or "[]")
            item["assignment_candidate_new_topics"] = json.loads(
                item.pop("assignment_candidates_json") or "[]"
            )
            item["needs_review"] = bool(item["needs_review"])
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            count += 1
    return count
