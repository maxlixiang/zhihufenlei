from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .metadata import CategoryAssignment, validate_assignment


ANALYSIS_VERSION = "2"
ANALYSIS_PROMPT_VERSION = "6"
ASSIGNMENT_PROMPT_VERSION = "2"
ANALYSIS_REPAIR_PROMPT_VERSION = "1"
CANDIDATE_RECHECK_PROMPT_VERSION = "1"
LOCAL_STRUCTURE_REPAIR_VERSION = "1"


class SemanticError(ValueError):
    pass


@dataclass(frozen=True)
class ArticleAnalysis:
    summary_short: str
    summary_detailed: str
    primary_purpose: str
    main_topics: tuple[str, ...]
    tags: tuple[str, ...]
    key_points: tuple[str, ...]
    entities: tuple[str, ...]
    content_type: str
    scope: str
    actionability: str
    time_sensitivity: str
    candidate_new_topics: tuple[dict[str, Any], ...]


def _strings(payload: Any, name: str, *, maximum: int) -> tuple[str, ...]:
    if not isinstance(payload, list):
        raise SemanticError(f"{name} 必须是数组")
    values: list[str] = []
    for item in payload:
        value = str(item).strip()
        if value and value not in values:
            values.append(value)
    if len(values) > maximum:
        raise SemanticError(f"{name} 不能超过 {maximum} 项")
    return tuple(values)


def validate_analysis(payload: Any) -> ArticleAnalysis:
    if not isinstance(payload, dict):
        raise SemanticError("analysis 必须是 JSON 对象")
    required_text = {}
    for name in (
        "summary_short", "summary_detailed", "primary_purpose", "content_type",
        "scope", "actionability", "time_sensitivity",
    ):
        value = str(payload.get(name, "")).strip()
        if not value:
            raise SemanticError(f"{name} 不能为空")
        required_text[name] = value
    if len(required_text["summary_short"]) > 300:
        raise SemanticError("summary_short 不能超过 300 字符")
    if len(required_text["summary_detailed"]) > 1200:
        raise SemanticError("summary_detailed 不能超过 1200 字符")

    candidate_raw = payload.get("candidate_new_topics", [])
    if not isinstance(candidate_raw, list):
        raise SemanticError("candidate_new_topics 必须是数组")
    candidates: list[dict[str, Any]] = []
    for item in candidate_raw:
        if not isinstance(item, dict):
            raise SemanticError("候选新主题必须是对象")
        candidates.append(item)
    if len(candidates) > 5:
        raise SemanticError("候选新主题不能超过 5 个")

    return ArticleAnalysis(
        summary_short=required_text["summary_short"],
        summary_detailed=required_text["summary_detailed"],
        primary_purpose=required_text["primary_purpose"],
        main_topics=_strings(payload.get("main_topics", []), "main_topics", maximum=10),
        tags=_strings(payload.get("tags", []), "tags", maximum=12),
        key_points=_strings(payload.get("key_points", []), "key_points", maximum=8),
        entities=_strings(payload.get("entities", []), "entities", maximum=20),
        content_type=required_text["content_type"],
        scope=required_text["scope"],
        actionability=required_text["actionability"],
        time_sensitivity=required_text["time_sensitivity"],
        candidate_new_topics=tuple(candidates),
    )


def validate_full_response(payload: Any, taxonomy) -> tuple[ArticleAnalysis, CategoryAssignment]:
    if not isinstance(payload, dict):
        raise SemanticError("模型输出必须是 JSON 对象")
    analysis = validate_analysis(payload.get("analysis"))
    assignment_payload = payload.get("assignment")
    assignment = validate_assignment(assignment_payload, taxonomy)
    return analysis, assignment
