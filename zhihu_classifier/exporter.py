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
               ca.error, ca.created_at,
               vr.review_status AS visual_review_status,
               vr.visual_summary, vr.collection_intent, vr.retention_decision,
               vr.tags_json AS visual_tags_json,
               mr.tags_json AS manual_tags_json,
               mr.review_result AS manual_review_result,
               mr.review_note AS manual_review_note
        FROM category_assignments ca
        JOIN articles a ON a.id = ca.article_id
        JOIN article_analyses aa ON aa.id = ca.analysis_id
        LEFT JOIN visual_reviews vr ON vr.id = (
            SELECT vr2.id FROM visual_reviews vr2
            WHERE vr2.article_id = a.id ORDER BY vr2.id DESC LIMIT 1
        )
        LEFT JOIN manual_reviews mr ON mr.id = (
            SELECT mr2.id FROM manual_reviews mr2
            WHERE mr2.article_id = a.id ORDER BY mr2.id DESC LIMIT 1
        )
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
            tags = json.loads(item.pop("tags_json") or "[]")
            visual_tags = json.loads(item.pop("visual_tags_json") or "[]")
            manual_tags = json.loads(item.pop("manual_tags_json") or "[]")
            item["tags"] = list(dict.fromkeys([*tags, *visual_tags, *manual_tags]))
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
