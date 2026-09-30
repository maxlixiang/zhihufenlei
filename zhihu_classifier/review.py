from __future__ import annotations

import json
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any

from .db import Database
from .taxonomy import Taxonomy


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_tags(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []
    if value.startswith("["):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("review_tags 的 JSON 必须是数组")
        values = [str(item).strip() for item in parsed]
    else:
        values = [item.strip() for item in re.split(r"[；;，,]", value)]
    return list(dict.fromkeys(item for item in values if item))


def _parse_review(path: Path) -> tuple[str, list[dict[str, Any]], str]:
    text = path.read_text(encoding="utf-8-sig")
    version_match = re.search(r"^- 分类规则版本：\s*(\S+)\s*$", text, re.MULTILINE)
    if version_match is None:
        raise ValueError("审核文件缺少分类规则版本")
    blocks = re.split(r"(?=^## \d{3} · )", text, flags=re.MULTILINE)[1:]
    items: list[dict[str, Any]] = []
    for block in blocks:
        heading = re.match(r"^## (\d{3}) · (.+)$", block, re.MULTILINE)
        if heading is None:
            continue
        fields: dict[str, str] = {}
        for line in block.splitlines():
            match = re.match(r"^-?\s*([a-z0-9_]+):\s*(.*)$", line)
            if match:
                fields[match.group(1)] = match.group(2).strip()
        result = fields.get("review_result", "")
        if result not in {"通过", "改类"}:
            raise ValueError(f"审核项 {heading.group(1)} 尚未定稿：{result or '空白'}")
        try:
            article_id = int(fields.get("article_id", ""))
        except ValueError as exc:
            raise ValueError(f"审核项 {heading.group(1)} 的 article_id 无效") from exc
        items.append({
            "index": heading.group(1), "title": heading.group(2).strip(),
            "article_id": article_id, "content_hash": fields.get("content_hash", ""),
            "current_category": fields.get("current_category", ""), "result": result,
            "level1_name": fields.get("review_level1", ""),
            "level2_name": fields.get("review_level2", ""),
            "tags": _parse_tags(fields.get("review_tags", "")),
            "note": fields.get("review_note", ""),
        })
    if not items:
        raise ValueError("审核文件没有可导入项目")
    ids = [item["article_id"] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("审核文件包含重复 article_id")
    return version_match.group(1), items, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _category_by_name(taxonomy: Taxonomy, level1_name: str, level2_name: str):
    category = next((item for item in taxonomy.categories if item.name == level1_name), None)
    if category is None or level2_name not in category.second_level:
        raise ValueError(f"分类不在规则 {taxonomy.version} 中：{level1_name}／{level2_name}")
    return category


def _single_line(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _display_alternatives(raw: str | None, taxonomy: Taxonomy) -> str:
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return "无"
    labels: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        level1_id = str(item.get("level1_id", "")).strip()
        level2_name = str(item.get("level2_name", "")).strip()
        category = taxonomy.category_by_id(level1_id)
        level1_name = category.name if category is not None else level1_id
        if level1_name or level2_name:
            labels.append(f"{level1_name}／{level2_name}".strip("／"))
    return "；".join(labels) if labels else "无"


def export_low_confidence_review(
    database: Database,
    taxonomy: Taxonomy,
    output_path: Path,
    *,
    below: float = 0.75,
) -> int:
    if not 0 < below <= 1:
        raise ValueError("below 必须大于0且不高于1")
    if output_path.exists():
        raise FileExistsError(
            f"审核文件已存在，为保护人工填写内容拒绝覆盖：{output_path}"
        )
    query = """
        SELECT a.id AS article_id, a.source_path, a.relative_path, a.title,
               a.content_hash, aa.summary_short, aa.primary_purpose,
               ca.level1_name, ca.level2_name, ca.alternatives_json,
               ca.reason, ca.confidence
        FROM category_assignments ca
        JOIN articles a ON a.id = ca.article_id
        JOIN article_analyses aa ON aa.id = ca.analysis_id
        WHERE ca.id = (
            SELECT ca2.id FROM category_assignments ca2
            WHERE ca2.article_id = ca.article_id
            ORDER BY ca2.id DESC LIMIT 1
        )
          AND ca.error IS NULL
          AND ca.confidence < ?
        ORDER BY ca.confidence, a.relative_path
    """
    with database.connect() as connection:
        rows = connection.execute(query, (below,)).fetchall()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 低置信度文章人工审核清单",
        "",
        f"- 分类规则版本：{taxonomy.version}",
        f"- 筛选条件：置信度低于 {below:.2f}",
        f"- 文章数量：{len(rows)}",
        "- 排序方式：置信度从低到高",
        "- 重要：该文件包含人工填写内容，程序默认拒绝覆盖。",
        "",
        "## 填写规则",
        "",
        "每篇只修改下面五个 review_ 字段：",
        "",
        "- review_result：填写“通过”“改类”“待讨论”或“内容不足”。",
        "- review_level1：仅在“改类”时填写正确一级目录的完整名称。",
        "- review_level2：仅在“改类”时填写正确二级目录的完整名称。",
        "- review_note：可选；写判断原因、规则疑问或希望我重点查看的内容。",
        "- review_tags：可选；多个标签用中文分号分隔。",
        "",
        f"如果当前分类正确，只填写 review_result: 通过，其他审核字段留空。完成一部分也可以直接把本文件交给 Codex，不必一次审完{len(rows)}篇。",
        "",
        "---",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        title = _single_line(row["title"])
        current = (
            f'{_single_line(row["level1_name"])}／{_single_line(row["level2_name"])}'
        )
        lines.extend(
            [
                f"## {index:03d} · {title}",
                "",
                f"- article_id: {row['article_id']}",
                f"- confidence: {row['confidence']:.2f}",
                f"- current_category: {current}",
                f"- alternatives: {_display_alternatives(row['alternatives_json'], taxonomy)}",
                f"- relative_path: {_single_line(row['relative_path'])}",
                f"- source_path: {_single_line(row['source_path'])}",
                f"- content_hash: {row['content_hash']}",
                f"- primary_purpose: {_single_line(row['primary_purpose'])}",
                f"- summary: {_single_line(row['summary_short'])}",
                f"- classification_reason: {_single_line(row['reason'])}",
                "",
                "review_result: 待填写",
                "review_level1:",
                "review_level2:",
                "review_tags:",
                "review_note:",
                "",
                "---",
                "",
            ]
        )
    output_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return len(rows)


def import_low_confidence_review(
    database: Database,
    taxonomy: Taxonomy,
    input_path: Path,
    result_path: Path,
) -> dict[str, int | str]:
    """本地继承兼容分类并导入人工审核；不读取原文、不调用模型。"""
    taxonomy.ensure_classifiable(allow_draft=False)
    if result_path.exists():
        raise FileExistsError(f"结果报告已存在，拒绝覆盖：{result_path}")
    source_version, items, source_hash = _parse_review(input_path)
    now = _utc_now()
    categories = {category.name: category for category in taxonomy.categories}
    with database.connect() as connection:
        if connection.execute(
            "SELECT 1 FROM manual_review_runs WHERE source_hash=?", (source_hash,)
        ).fetchone():
            raise ValueError("该审核文件已经导入，拒绝重复应用")

        resolved: list[dict[str, Any]] = []
        for item in items:
            row = connection.execute(
                """
                SELECT a.title, a.content_hash, ca.* FROM articles a
                JOIN category_assignments ca ON ca.id=(
                    SELECT ca2.id FROM category_assignments ca2
                    WHERE ca2.article_id=a.id AND ca2.error IS NULL
                    ORDER BY ca2.id DESC LIMIT 1)
                WHERE a.id=?
                """,
                (item["article_id"],),
            ).fetchone()
            if row is None:
                raise ValueError(f"审核项 {item['index']} 找不到成功分类")
            if row["title"].strip() != item["title"] or row["content_hash"] != item["content_hash"]:
                raise ValueError(f"审核项 {item['index']} 的标题或内容哈希与数据库不一致")
            original_category = f"{row['level1_name']}／{row['level2_name']}"
            if original_category != item["current_category"]:
                raise ValueError(f"审核项 {item['index']} 的原分类与数据库不一致")
            if item["result"] == "通过":
                level1_name, level2_name = row["level1_name"], row["level2_name"]
            else:
                level1_name, level2_name = item["level1_name"], item["level2_name"]
            category = _category_by_name(taxonomy, level1_name, level2_name)
            resolved.append(
                {**item, "category": category, "level1_name": level1_name,
                 "level2_name": level2_name}
            )

        carry_run_id = connection.execute(
            """
            INSERT INTO assignment_runs (
                started_at, completed_at, taxonomy_version, assignment_prompt_version,
                provider, model, source, status, processed_count, error_count
            ) VALUES (?, ?, ?, 'compatible-carry-forward-1', 'local', 'deterministic',
                      'compatible_carry_forward', 'completed', 0, 0)
            """,
            (now, now, taxonomy.version),
        ).lastrowid
        latest_rows = connection.execute(
            """
            SELECT ca.* FROM category_assignments ca
            WHERE ca.error IS NULL AND ca.id=(
                SELECT ca2.id FROM category_assignments ca2
                WHERE ca2.article_id=ca.article_id AND ca2.error IS NULL
                ORDER BY ca2.id DESC LIMIT 1)
            ORDER BY ca.article_id
            """
        ).fetchall()
        carried = 0
        for row in latest_rows:
            if row["taxonomy_version"] == taxonomy.version:
                continue
            category = categories.get(row["level1_name"] or "")
            if category is None or row["level2_name"] not in category.second_level:
                raise ValueError(
                    f"文章 {row['article_id']} 的旧分类与 {taxonomy.version} 不兼容，必须先审核"
                )
            connection.execute(
                """
                INSERT INTO category_assignments (
                    article_id, analysis_id, run_id, taxonomy_version,
                    assignment_prompt_version, provider, model, source,
                    level1_id, level1_name, level2_name, alternatives_json,
                    reason, confidence, needs_review, candidate_new_topics_json,
                    raw_response_json, created_at
                ) VALUES (?, ?, ?, ?, 'compatible-carry-forward-1', 'local', 'deterministic',
                          'compatible_carry_forward', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (row["article_id"], row["analysis_id"], carry_run_id, taxonomy.version,
                 category.id, category.name, row["level2_name"], row["alternatives_json"],
                 row["reason"], row["confidence"], row["needs_review"],
                 row["candidate_new_topics_json"], now),
            )
            carried += 1
        connection.execute(
            "UPDATE assignment_runs SET processed_count=? WHERE id=?", (carried, carry_run_id)
        )

        review_run_id = connection.execute(
            """
            INSERT INTO assignment_runs (
                started_at, completed_at, taxonomy_version, assignment_prompt_version,
                provider, model, source, status, requested_limit, processed_count, error_count
            ) VALUES (?, ?, ?, 'manual-review-1', 'human', 'manual',
                      'human_low_confidence_review', 'completed', ?, ?, 0)
            """,
            (now, now, taxonomy.version, len(resolved), len(resolved)),
        ).lastrowid
        accepted = sum(item["result"] == "通过" for item in resolved)
        overrides = len(resolved) - accepted
        tags = sum(len(item["tags"]) for item in resolved)
        manual_run_id = connection.execute(
            """
            INSERT INTO manual_review_runs (
                imported_at, source_path, source_hash, source_taxonomy_version,
                target_taxonomy_version, item_count, accepted_count, override_count, tag_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, str(input_path.resolve()), source_hash, source_version, taxonomy.version,
             len(resolved), accepted, overrides, tags),
        ).lastrowid
        for item in resolved:
            previous = connection.execute(
                "SELECT * FROM category_assignments WHERE article_id=? AND error IS NULL ORDER BY id DESC LIMIT 1",
                (item["article_id"],),
            ).fetchone()
            reason = "人工审核通过原分类" if item["result"] == "通过" else "人工审核改类"
            if item["note"]:
                reason += "：" + item["note"]
            assignment_id = connection.execute(
                """
                INSERT INTO category_assignments (
                    article_id, analysis_id, run_id, taxonomy_version,
                    assignment_prompt_version, provider, model, source,
                    level1_id, level1_name, level2_name, alternatives_json,
                    reason, confidence, needs_review, candidate_new_topics_json,
                    raw_response_json, created_at
                ) VALUES (?, ?, ?, ?, 'manual-review-1', 'human', 'manual',
                          'human_low_confidence_review', ?, ?, ?, '[]', ?, 1.0, 0, '[]', NULL, ?)
                """,
                (item["article_id"], previous["analysis_id"], review_run_id, taxonomy.version,
                 item["category"].id, item["level1_name"], item["level2_name"], reason, now),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO manual_reviews (
                    run_id, article_id, content_hash, previous_assignment_id,
                    new_assignment_id, review_result, level1_id, level1_name,
                    level2_name, tags_json, review_note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (manual_run_id, item["article_id"], item["content_hash"], previous["id"],
                 assignment_id, item["result"], item["category"].id,
                 item["level1_name"], item["level2_name"],
                 json.dumps(item["tags"], ensure_ascii=False), item["note"], now),
            )

    lines = [
        "# 低置信度人工审核导入结果", "", f"- 源规则版本：{source_version}",
        f"- 目标规则版本：{taxonomy.version}", f"- 审核项目：{len(resolved)}",
        f"- 通过原分类：{accepted}", f"- 人工改类：{overrides}",
        f"- 新增人工标签：{tags}", f"- 本地兼容继承：{carried}",
        "- DeepSeek 调用：0", "",
        "本次只更新项目数据库和审核记录，未读取原始文章正文，未写入分类文章库。", "",
    ]
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return {"items": len(resolved), "accepted": accepted, "overrides": overrides,
            "tags": tags, "carried": carried, "taxonomy_version": taxonomy.version}
