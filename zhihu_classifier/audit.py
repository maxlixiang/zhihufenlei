from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from .db import Database
from .taxonomy import Taxonomy


def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _confidence_band(value: float) -> str:
    if value >= 0.85:
        return "0.85–1.00"
    if value >= 0.80:
        return "0.80–0.84"
    if value >= 0.75:
        return "0.75–0.79"
    return "低于0.75"


def _stable_sample(rows: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: hashlib.sha256(str(row["article_id"]).encode("ascii")).hexdigest(),
    )[:size]


def generate_quality_report(
    database: Database,
    taxonomy: Taxonomy,
    output_path: Path,
    *,
    sample_per_band: int = 15,
) -> dict[str, int]:
    with database.connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT a.id AS article_id, a.title, a.relative_path,
                       aa.summary_short,
                       ca.taxonomy_version, ca.assignment_prompt_version,
                       ca.source, ca.level1_id, ca.level1_name, ca.level2_name,
                       ca.reason, ca.confidence, ca.needs_review,
                       ca.candidate_new_topics_json
                FROM articles a
                JOIN article_analyses aa ON aa.id = (
                    SELECT aa2.id
                    FROM article_analyses aa2
                    WHERE aa2.article_id = a.id
                      AND aa2.content_hash = a.content_hash
                      AND aa2.error IS NULL
                    ORDER BY aa2.id DESC
                    LIMIT 1
                )
                JOIN category_assignments ca ON ca.id = (
                    SELECT ca2.id
                    FROM category_assignments ca2
                    WHERE ca2.article_id = a.id
                      AND ca2.error IS NULL
                    ORDER BY ca2.id DESC
                    LIMIT 1
                )
                ORDER BY a.id
                """
            )
        ]
        outstanding_errors = [
            dict(row)
            for row in connection.execute(
                """
                SELECT aa.error, COUNT(DISTINCT aa.article_id) AS articles
                FROM article_analyses aa
                JOIN articles a ON a.id = aa.article_id
                WHERE aa.error IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM article_analyses ok
                      WHERE ok.article_id = aa.article_id
                        AND ok.content_hash = a.content_hash
                        AND ok.error IS NULL
                  )
                GROUP BY aa.error
                ORDER BY articles DESC, aa.error
                """
            )
        ]

    level1: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"articles": 0, "review": 0, "confidence_sum": 0.0}
    )
    level2: dict[tuple[str, str], dict[str, float | int]] = defaultdict(
        lambda: {"articles": 0, "review": 0, "confidence_sum": 0.0}
    )
    versions: dict[str, int] = defaultdict(int)
    bands: dict[str, list[dict[str, Any]]] = defaultdict(list)
    candidate_groups: dict[str, dict[str, Any]] = {}
    high_confidence_review = 0
    low_confidence_review = 0

    for row in rows:
        confidence = float(row["confidence"] or 0)
        review = int(bool(row["needs_review"]))
        level1_name = row["level1_name"] or "[未归类候选]"
        level2_name = row["level2_name"] or "[未归类候选]"
        versions[str(row["taxonomy_version"])] += 1
        level1[level1_name]["articles"] += 1
        level1[level1_name]["review"] += review
        level1[level1_name]["confidence_sum"] += confidence
        key2 = (level1_name, level2_name)
        level2[key2]["articles"] += 1
        level2[key2]["review"] += review
        level2[key2]["confidence_sum"] += confidence
        bands[_confidence_band(confidence)].append(row)
        if review and confidence < 0.85:
            low_confidence_review += 1
        elif review:
            high_confidence_review += 1

        try:
            candidates = json.loads(row["candidate_new_topics_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            candidates = []
        if not isinstance(candidates, list):
            candidates = [candidates]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            proposed_level1 = (
                item.get("suggested_level1")
                or item.get("level1_name")
                or item.get("suggested_name")
                or "未命名一级"
            )
            proposed_level2 = (
                item.get("suggested_level2")
                or item.get("level2_name")
                or "未命名二级"
            )
            candidate_key = f"{proposed_level1}／{proposed_level2}"
            group = candidate_groups.setdefault(
                candidate_key,
                {
                    "count": 0,
                    "definition": item.get("definition", ""),
                    "titles": [],
                },
            )
            group["count"] += 1
            if len(group["titles"]) < 3:
                group["titles"].append(row["title"])

    counts = database.counts()
    lines = [
        "# 知乎文章分类质量报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 分类规则版本：`{taxonomy.version}`",
        f"- 原始文章：{counts['articles']} 篇",
        f"- 成功语义档案：{counts['current_analyses']} 篇",
        f"- 尚无成功语义档案：{counts['pending_full_text_analysis']} 篇",
        f"- 最新有效分类：{len(rows)} 篇",
        f"- 待人工审核：{sum(int(bool(row['needs_review'])) for row in rows)} 篇",
        "",
        "## 一、规则版本覆盖",
        "",
        "| 规则版本 | 当前有效分类数 |",
        "|---|---:|",
    ]
    for version, count in sorted(versions.items()):
        lines.append(f"| {_cell(version)} | {count} |")

    lines.extend(
        [
            "",
            "## 二、置信度与审核负担",
            "",
            f"- 低于0.85而进入审核：{low_confidence_review} 篇",
            f"- 不低于0.85但仍需审核：{high_confidence_review} 篇",
            "",
            "| 置信度区间 | 文章数 | 建议用途 |",
            "|---|---:|---|",
            f"| 0.85–1.00 | {len(bands['0.85–1.00'])} | 高置信度抽查 |",
            f"| 0.80–0.84 | {len(bands['0.80–0.84'])} | 阈值校准重点样本 |",
            f"| 0.75–0.79 | {len(bands['0.75–0.79'])} | 边界与目录混淆审核 |",
            f"| 低于0.75 | {len(bands['低于0.75'])} | 优先人工审核或候选主题处理 |",
            "",
            "在完成抽样核对前，不自动降低0.85阈值。建议先比较0.80–0.84样本与高置信度样本的实际准确率。",
            "",
            "## 三、一级目录分布",
            "",
            "| 一级目录 | 文章数 | 待审核 | 平均置信度 |",
            "|---|---:|---:|---:|",
        ]
    )
    for name, values in sorted(
        level1.items(), key=lambda item: int(item[1]["articles"]), reverse=True
    ):
        article_count = int(values["articles"])
        average = float(values["confidence_sum"]) / article_count
        lines.append(
            f"| {_cell(name)} | {article_count} | {int(values['review'])} | {average:.3f} |"
        )

    lines.extend(
        [
            "",
            "## 四、二级目录分布",
            "",
            "| 一级目录 | 二级目录 | 文章数 | 待审核 | 平均置信度 |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for (parent, child), values in sorted(
        level2.items(), key=lambda item: int(item[1]["articles"]), reverse=True
    ):
        article_count = int(values["articles"])
        average = float(values["confidence_sum"]) / article_count
        lines.append(
            f"| {_cell(parent)} | {_cell(child)} | {article_count} | "
            f"{int(values['review'])} | {average:.3f} |"
        )

    lines.extend(
        [
            "",
            "## 五、仍未解决的模型错误",
            "",
            "| 错误类型 | 文章数 |",
            "|---|---:|",
        ]
    )
    if outstanding_errors:
        for item in outstanding_errors:
            lines.append(f"| {_cell(item['error'])} | {item['articles']} |")
    else:
        lines.append("| 无 | 0 |")

    lines.extend(
        [
            "",
            "## 六、复核后仍保留的候选主题",
            "",
            "| 候选目录 | 文章数 | 定义 | 代表文章 |",
            "|---|---:|---|---|",
        ]
    )
    if candidate_groups:
        for name, group in sorted(
            candidate_groups.items(),
            key=lambda item: (-int(item[1]["count"]), item[0]),
        ):
            lines.append(
                f"| {_cell(name)} | {group['count']} | {_cell(group['definition'])} | "
                f"{_cell('；'.join(group['titles']))} |"
            )
    else:
        lines.append("| 无 | 0 | 现有目录已覆盖 | — |")

    lines.extend(["", "## 七、置信度分层抽查样本", ""])
    for band in ("0.85–1.00", "0.80–0.84", "0.75–0.79", "低于0.75"):
        samples = _stable_sample(bands[band], sample_per_band)
        lines.extend(
            [
                f"### {band}",
                "",
                "| 标题 | 分类 | 置信度 | 分类理由 | 语义摘要 |",
                "|---|---|---:|---|---|",
            ]
        )
        for row in samples:
            category = f"{row['level1_name'] or '[未归类]'}／{row['level2_name'] or '[未归类]'}"
            lines.append(
                f"| {_cell(row['title'])} | {_cell(category)} | "
                f"{float(row['confidence'] or 0):.2f} | {_cell(row['reason'])} | "
                f"{_cell(row['summary_short'])} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 八、当前结论",
            "",
            "1. 优先审核未归类候选和低于0.75的文章。",
            "2. 对文章量最大的一级目录进行分层抽查，防止宽泛目录成为杂物箱。",
            "3. 只有在0.80–0.84样本准确率得到验证后，才考虑降低自动通过阈值。",
            "4. 规则调整后只使用永久语义档案重映射，不重新发送未变化文章全文。",
            "5. 本报告不授权或执行任何文章复制。",
            "",
        ]
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return {
        "assignments": len(rows),
        "needs_review": sum(int(bool(row["needs_review"])) for row in rows),
        "candidate_groups": len(candidate_groups),
        "outstanding_errors": counts["pending_full_text_analysis"],
        "samples": sum(
            len(_stable_sample(bands[band], sample_per_band))
            for band in ("0.85–1.00", "0.80–0.84", "0.75–0.79", "低于0.75")
        ),
    }
