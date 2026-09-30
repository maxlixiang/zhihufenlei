from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .taxonomy import Taxonomy


class MetadataError(ValueError):
    pass


@dataclass(frozen=True)
class ClassificationMetadata:
    level1_id: str
    level1_name: str
    level2_name: str
    tags: tuple[str, ...]
    summary: str
    purpose: str
    reason: str
    confidence: float
    needs_review: bool
    candidate_new_topic: dict[str, Any] | None


@dataclass(frozen=True)
class CategoryAssignment:
    level1_id: str
    level1_name: str
    level2_name: str
    alternatives: tuple[dict[str, Any], ...]
    reason: str
    confidence: float
    needs_review: bool
    candidate_new_topics: tuple[dict[str, Any], ...]


def validate_assignment(payload: Any, taxonomy: Taxonomy) -> CategoryAssignment:
    if not isinstance(payload, dict):
        raise MetadataError("assignment 必须是 JSON 对象")
    if (
        "category" not in payload
        and payload.get("type") == "json_object"
        and isinstance(payload.get("content"), dict)
    ):
        payload = payload["content"]
    category = payload.get("category")
    if not isinstance(category, dict):
        raise MetadataError("assignment 缺少 category 对象")
    level1_id = str(category.get("level1_id", "")).strip()
    level1_name = str(category.get("level1_name", "")).strip()
    level2_name = str(category.get("level2_name", "")).strip()
    candidates_raw = payload.get("candidate_new_topics", [])
    if not isinstance(candidates_raw, list):
        raise MetadataError("candidate_new_topics 必须是数组")
    candidates = tuple(item for item in candidates_raw if isinstance(item, dict))
    known_category = taxonomy.category_by_id(level1_id)
    category_is_empty = not level1_id and not level1_name and not level2_name
    explicitly_insufficient = (
        category_is_empty
        and not candidates
        and bool(payload.get("needs_review", False))
    )
    if not candidates and not explicitly_insufficient:
        if known_category is None:
            raise MetadataError(f"未知一级分类 ID：{level1_id}")
        if level1_name != known_category.name:
            raise MetadataError("一级分类 ID 与名称不匹配")
        if level2_name not in known_category.second_level:
            raise MetadataError(f"未知二级分类：{level1_name}/{level2_name}")
    alternatives_raw = payload.get("alternatives", [])
    if not isinstance(alternatives_raw, list):
        raise MetadataError("alternatives 必须是数组")
    alternatives = tuple(item for item in alternatives_raw if isinstance(item, dict))
    reason = str(payload.get("reason", "")).strip()
    if not reason:
        raise MetadataError("assignment.reason 不能为空")
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise MetadataError("assignment.confidence 必须是数字") from exc
    if not 0 <= confidence <= 1:
        raise MetadataError("assignment.confidence 必须在 0 到 1 之间")
    if explicitly_insufficient and confidence > 0.2:
        raise MetadataError("内容不足且未分类时，confidence 不能高于 0.2")
    uses_candidates = known_category is not None and known_category.uses_candidates
    needs_review = (
        bool(payload.get("needs_review", False))
        or confidence < 0.85
        or bool(candidates)
        or uses_candidates
    )
    return CategoryAssignment(
        level1_id=level1_id,
        level1_name=level1_name,
        level2_name=level2_name,
        alternatives=alternatives,
        reason=reason,
        confidence=confidence,
        needs_review=needs_review,
        candidate_new_topics=candidates,
    )


def validate_metadata(payload: Any, taxonomy: Taxonomy) -> ClassificationMetadata:
    if not isinstance(payload, dict):
        raise MetadataError("模型输出必须是 JSON 对象")
    category = payload.get("category")
    if not isinstance(category, dict):
        raise MetadataError("缺少 category 对象")

    level1_id = str(category.get("level1_id", "")).strip()
    level1_name = str(category.get("level1_name", "")).strip()
    level2_name = str(category.get("level2_name", "")).strip()
    candidate = payload.get("candidate_new_topic")
    known_category = taxonomy.category_by_id(level1_id)

    if candidate is None:
        if known_category is None:
            raise MetadataError(f"未知一级分类 ID：{level1_id}")
        if level1_name != known_category.name:
            raise MetadataError("一级分类 ID 与名称不匹配")
        if level2_name not in known_category.second_level:
            raise MetadataError(f"未知二级分类：{level1_name}/{level2_name}")
    elif not isinstance(candidate, dict):
        raise MetadataError("candidate_new_topic 必须是对象或 null")

    raw_tags = payload.get("tags", [])
    if not isinstance(raw_tags, list):
        raise MetadataError("tags 必须是数组")
    tags: list[str] = []
    for raw_tag in raw_tags:
        tag = str(raw_tag).strip()
        if tag and tag not in tags:
            tags.append(tag)
    if len(tags) > 12:
        raise MetadataError("标签不能超过 12 个")

    summary = str(payload.get("summary", "")).strip()
    purpose = str(payload.get("purpose", "")).strip()
    reason = str(payload.get("reason", "")).strip()
    if not summary or not purpose or not reason:
        raise MetadataError("summary、purpose 和 reason 不能为空")

    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise MetadataError("confidence 必须是数字") from exc
    if not 0 <= confidence <= 1:
        raise MetadataError("confidence 必须在 0 到 1 之间")

    model_review = bool(payload.get("needs_review", False))
    uses_unconfirmed_category = known_category is not None and known_category.uses_candidates
    needs_review = (
        model_review
        or confidence < 0.85
        or candidate is not None
        or uses_unconfirmed_category
    )
    return ClassificationMetadata(
        level1_id=level1_id,
        level1_name=level1_name,
        level2_name=level2_name,
        tags=tuple(tags),
        summary=summary,
        purpose=purpose,
        reason=reason,
        confidence=confidence,
        needs_review=needs_review,
        candidate_new_topic=candidate,
    )
