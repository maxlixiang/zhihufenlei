from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote

from .db import Database
from .preprocess import COMMENT_HEADING_RE, FRONT_MATTER_RE
from .taxonomy import Taxonomy


MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HTML_IMAGE_RE = re.compile(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".svg", ".webp"
}
IMAGE_DOMINANT_TAG = "内容形态/图片为主"
REVIEW_BLOCK_RE = re.compile(r"(?=^## \d{3} · )", re.MULTILINE)


@dataclass(frozen=True)
class VisualCandidate:
    article_id: int
    title: str
    source_path: Path
    relative_path: str
    text_chars: int
    image_references: int
    attachment_images: int
    attachment_paths: tuple[Path, ...]
    level1_name: str
    level2_name: str
    confidence: float | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _field(block: str, name: str) -> str:
    prefix = name + ":"
    for line in block.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _parse_review_blocks(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    items: list[dict[str, str]] = []
    for block in REVIEW_BLOCK_RE.split(text):
        heading = re.match(r"^## (\d{3}) · (.+)$", block, re.MULTILINE)
        if heading is None:
            continue
        values = {
            "index": heading.group(1),
            "title": heading.group(2).strip(),
            "article_id": _field(block, "- article_id"),
            "source_path": _field(block, "- source_path"),
            "visual_review_status": _field(block, "visual_review_status"),
            "review_level1": _field(block, "review_level1"),
            "review_level2": _field(block, "review_level2"),
            "visual_summary": _field(block, "visual_summary"),
            "collection_intent": _field(block, "collection_intent"),
            "retention_decision": _field(block, "retention_decision"),
            "review_note": _field(block, "review_note"),
        }
        if not values["article_id"]:
            raise ValueError(f"审核项 {values['index']} 缺少 article_id")
        items.append(values)
    if not items:
        raise ValueError("审核清单中没有找到文章条目")
    if len({item["article_id"] for item in items}) != len(items):
        raise ValueError("审核清单包含重复 article_id")
    return items


def _main_body(text: str) -> str:
    text = FRONT_MATTER_RE.sub("", text, count=1)
    lines: list[str] = []
    skipped_title = False
    for line in text.splitlines():
        if COMMENT_HEADING_RE.match(line):
            break
        if not skipped_title and re.match(r"^\s*#\s+", line):
            skipped_title = True
            continue
        lines.append(line)
    return "\n".join(lines)


def _local_image_targets(body: str, source_path: Path) -> tuple[int, tuple[Path, ...]]:
    raw_targets = MARKDOWN_IMAGE_RE.findall(body) + HTML_IMAGE_RE.findall(body)
    local_targets: list[str] = []
    for target in raw_targets:
        value = target.strip().strip("<>").split("#", 1)[0]
        if value.lower().startswith(("http://", "https://", "data:")):
            continue
        local_targets.append(unquote(value))

    directories: set[Path] = set()
    for target in local_targets:
        candidate = (source_path.parent / target).resolve()
        if candidate.parent != source_path.parent:
            current = candidate.parent
            while current != source_path.parent and source_path.parent in current.parents:
                if current.parent == source_path.parent:
                    directories.add(current)
                    break
                current = current.parent

    for name in (source_path.stem, source_path.stem + "_图片"):
        candidate = source_path.parent / name
        if candidate.is_dir():
            directories.add(candidate.resolve())
    return len(local_targets), tuple(sorted(directories, key=str))


def _count_attachment_images(directories: tuple[Path, ...]) -> int:
    return sum(
        1
        for directory in directories
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def _semantic_text_chars(body: str) -> int:
    body = MARKDOWN_IMAGE_RE.sub("", body)
    body = HTML_IMAGE_RE.sub("", body)
    body = HTML_TAG_RE.sub("", body)
    body = URL_RE.sub("", body)
    return len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", body))


def find_visual_candidates(
    database: Database,
    *,
    max_text_chars: int = 200,
    min_images: int = 1,
) -> list[VisualCandidate]:
    if max_text_chars < 0:
        raise ValueError("max_text_chars 不能小于0")
    if min_images < 1:
        raise ValueError("min_images 必须大于0")
    query = """
        SELECT a.id AS article_id, a.title, a.source_path, a.relative_path,
               ca.level1_name, ca.level2_name, ca.confidence
        FROM articles a
        LEFT JOIN category_assignments ca ON ca.id = (
            SELECT ca2.id FROM category_assignments ca2
            WHERE ca2.article_id = a.id
            ORDER BY ca2.id DESC LIMIT 1
        )
        WHERE a.read_error IS NULL
        ORDER BY a.relative_path
    """
    with database.connect() as connection:
        rows = connection.execute(query).fetchall()

    candidates: list[VisualCandidate] = []
    for row in rows:
        source_path = Path(row["source_path"])
        try:
            body = _main_body(source_path.read_text(encoding="utf-8-sig"))
            image_references, attachment_paths = _local_image_targets(body, source_path)
            attachment_images = _count_attachment_images(attachment_paths)
            text_chars = _semantic_text_chars(body)
        except (OSError, UnicodeError):
            continue
        if text_chars > max_text_chars or max(image_references, attachment_images) < min_images:
            continue
        candidates.append(
            VisualCandidate(
                article_id=row["article_id"],
                title=str(row["title"]),
                source_path=source_path,
                relative_path=str(row["relative_path"]),
                text_chars=text_chars,
                image_references=image_references,
                attachment_images=attachment_images,
                attachment_paths=attachment_paths,
                level1_name=str(row["level1_name"] or ""),
                level2_name=str(row["level2_name"] or ""),
                confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            )
        )
    return sorted(candidates, key=lambda item: (item.text_chars, item.relative_path))


def export_visual_review(
    database: Database,
    taxonomy: Taxonomy,
    output_path: Path,
    *,
    max_text_chars: int = 200,
    min_images: int = 1,
) -> int:
    if output_path.exists():
        raise FileExistsError(f"审核文件已存在，为保护人工填写内容拒绝覆盖：{output_path}")
    candidates = find_visual_candidates(
        database,
        max_text_chars=max_text_chars,
        min_images=min_images,
    )
    lines = [
        "# 图片为主文章人工审核清单",
        "",
        f"- 分类规则版本：{taxonomy.version}",
        f"- 识别条件：正文有效文字不超过 {max_text_chars} 字，且至少有 {min_images} 张本地图片引用或附件图片",
        f"- 候选数量：{len(candidates)}",
        "- 排序方式：正文有效文字从少到多",
        "- 重要：该文件包含人工填写内容，程序默认拒绝覆盖；不会修改或删除原始文章。",
        "",
        "## 你需要填写什么",
        "",
        "每篇优先填写 `review_level1`、`review_level2` 和一句 `visual_summary`。如果认为文章不值得进入分类文章库，只把 `retention_decision` 改为“不纳入”，原始文章仍永久保留。",
        "",
        "- visual_review_status：填写“已识别”“仍待识别”或“待讨论”。",
        "- review_level1 / review_level2：图片真实内容对应的唯一正式分类；仍无法判断时留空。",
        "- visual_summary：一句话描述图片内容及文章的主要收藏价值。",
        "- collection_intent：填写“获取知识”“幽默放松”“视觉欣赏”或简短自定义用途。",
        "- retention_decision：填写“保留”“不纳入”或“待定”；它与主题分类相互独立，不会触发删除原文。",
        "- review_note：可选，记录疑问或希望 Codex 重点处理的边界问题。",
        "",
        "完成任意一部分即可交给 Codex处理，不必一次审完。",
        "",
        "---",
        "",
    ]
    for index, item in enumerate(candidates, start=1):
        current = "／".join(v for v in (item.level1_name, item.level2_name) if v) or "未分类"
        confidence = f"{item.confidence:.2f}" if item.confidence is not None else "无"
        attachment_paths = "；".join(str(path) for path in item.attachment_paths) or "未发现独立目录"
        lines.extend(
            [
                f"## {index:03d} · {item.title}",
                "",
                f"- article_id: {item.article_id}",
                f"- text_chars: {item.text_chars}",
                f"- image_references: {item.image_references}",
                f"- attachment_images: {item.attachment_images}",
                f"- current_category: {current}",
                f"- confidence: {confidence}",
                f"- relative_path: {item.relative_path}",
                f"- source_path: {item.source_path}",
                f"- content_hash: {_sha256(item.source_path)}",
                f"- attachment_paths: {attachment_paths}",
                "",
                "visual_review_status: 仍待识别",
                "review_level1:",
                "review_level2:",
                "visual_summary:",
                "collection_intent:",
                "retention_decision: 待定",
                "review_note:",
                "",
                "---",
                "",
            ]
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return len(candidates)


def import_visual_review(
    database: Database,
    taxonomy: Taxonomy,
    input_path: Path,
    result_path: Path,
    *,
    accept_unfilled_current: bool = False,
) -> dict[str, int]:
    if result_path.exists():
        raise FileExistsError(f"整理结果已存在，为避免覆盖而停止：{result_path}")
    source_hash = _sha256(input_path)
    items = _parse_review_blocks(input_path)
    now = _utc_now()
    reviewed = overrides = inherited = 0
    resolved: list[dict[str, str]] = []
    with database.connect() as connection:
        if connection.execute(
            "SELECT 1 FROM visual_review_runs WHERE source_hash = ?", (source_hash,)
        ).fetchone():
            raise ValueError("这份审核清单内容已经导入，拒绝重复写入")
        run_id = connection.execute(
            """
            INSERT INTO visual_review_runs (
                imported_at, source_path, source_hash, taxonomy_version,
                item_count, reviewed_count, override_count
            ) VALUES (?, ?, ?, ?, ?, 0, 0)
            """,
            (now, str(input_path.resolve()), source_hash, taxonomy.version, len(items)),
        ).lastrowid

        for item in items:
            try:
                article_id = int(item["article_id"])
            except ValueError as exc:
                raise ValueError(f"审核项 {item['index']} 的 article_id 不是整数") from exc
            row = connection.execute(
                """
                SELECT a.*, aa.id AS analysis_id,
                       ca.level1_id, ca.level1_name, ca.level2_name,
                       ca.confidence, ca.needs_review
                FROM articles a
                JOIN article_analyses aa ON aa.id = (
                    SELECT aa2.id FROM article_analyses aa2
                    WHERE aa2.article_id = a.id
                      AND aa2.content_hash = a.content_hash
                      AND aa2.error IS NULL
                    ORDER BY aa2.id DESC LIMIT 1
                )
                JOIN category_assignments ca ON ca.id = (
                    SELECT ca2.id FROM category_assignments ca2
                    WHERE ca2.article_id = a.id AND ca2.error IS NULL
                    ORDER BY ca2.id DESC LIMIT 1
                )
                WHERE a.id = ?
                """,
                (article_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"审核项 {item['index']} 找不到有效文章、语义档案或分类")
            if Path(item["source_path"]).resolve() != Path(row["source_path"]).resolve():
                raise ValueError(f"审核项 {item['index']} 的源路径与数据库不一致")
            if _sha256(Path(row["source_path"])) != row["content_hash"]:
                raise ValueError(f"审核项 {item['index']} 的原文已变化，请先重新扫描")

            explicit_level1 = item["review_level1"]
            explicit_level2 = item["review_level2"]
            if bool(explicit_level1) != bool(explicit_level2):
                raise ValueError(f"审核项 {item['index']} 必须同时填写一级和二级分类")
            was_reviewed = item["visual_review_status"] == "已识别"
            if explicit_level1:
                category = next(
                    (candidate for candidate in taxonomy.categories if candidate.name == explicit_level1),
                    None,
                )
                if category is None or explicit_level2 not in category.second_level:
                    raise ValueError(
                        f"审核项 {item['index']} 使用未知分类：{explicit_level1}/{explicit_level2}"
                    )
                level1_id = category.id
                level1_name = category.name
                level2_name = explicit_level2
                category_decision = "manual_override"
                overrides += 1
            else:
                category = taxonomy.category_by_id(row["level1_id"] or "")
                if (
                    category is None
                    or row["level1_name"] != category.name
                    or row["level2_name"] not in category.second_level
                ):
                    raise ValueError(f"审核项 {item['index']} 的现有分类不再属于当前规则")
                level1_id = category.id
                level1_name = category.name
                level2_name = row["level2_name"]
                category_decision = "accepted_current" if was_reviewed else "unfilled_current"
                inherited += 1
                if not was_reviewed and not accept_unfilled_current:
                    raise ValueError(
                        f"审核项 {item['index']} 尚未填写；如要沿用现有分类需明确启用 accept_unfilled_current"
                    )

            if was_reviewed:
                reviewed += 1
                connection.execute(
                    """
                    INSERT INTO category_assignments (
                        article_id, analysis_id, taxonomy_version, assignment_prompt_version,
                        provider, model, source, level1_id, level1_name, level2_name,
                        alternatives_json, reason, confidence, needs_review,
                        candidate_new_topics_json, created_at
                    ) VALUES (?, ?, ?, 'manual-visual-review-1', 'human', 'manual',
                              'human_visual_review', ?, ?, ?, '[]', ?, 1.0, 0, '[]', ?)
                    """,
                    (
                        article_id, row["analysis_id"], taxonomy.version,
                        level1_id, level1_name, level2_name,
                        "用户查看图片后确认分类" if explicit_level1 else "用户查看图片后确认沿用现有分类",
                        now,
                    ),
                )

            retention = item["retention_decision"] or "待定"
            if retention not in {"保留", "不纳入", "待定"}:
                raise ValueError(f"审核项 {item['index']} 的 retention_decision 无效：{retention}")
            review_status = "已识别" if was_reviewed else "沿用现有分类"
            connection.execute(
                """
                INSERT INTO visual_reviews (
                    run_id, article_id, content_hash, taxonomy_version, review_status,
                    category_decision, level1_id, level1_name, level2_name,
                    visual_summary, collection_intent, retention_decision,
                    tags_json, review_note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, article_id, row["content_hash"], taxonomy.version,
                    review_status, category_decision, level1_id, level1_name, level2_name,
                    item["visual_summary"], item["collection_intent"], retention,
                    json.dumps([IMAGE_DOMINANT_TAG], ensure_ascii=False),
                    item["review_note"], now,
                ),
            )
            resolved.append(
                {
                    "index": item["index"],
                    "title": item["title"],
                    "decision": category_decision,
                    "category": f"{level1_name}／{level2_name}",
                    "review_status": review_status,
                    "retention": retention,
                }
            )

        connection.execute(
            "UPDATE visual_review_runs SET reviewed_count=?, override_count=? WHERE id=?",
            (reviewed, overrides, run_id),
        )

    lines = [
        "# 图片主导文章审核整理结果",
        "",
        f"- 规则版本：{taxonomy.version}",
        f"- 总数：{len(items)}",
        f"- 人工标记已识别：{reviewed}",
        f"- 明确改类：{overrides}",
        f"- 沿用现有分类：{inherited}",
        f"- 统一附加标签：{IMAGE_DOMINANT_TAG}",
        "- 说明：未填写项按用户授权沿用现有分类；本次没有复制或修改原始文章。",
        "",
        "## 明确改类",
        "",
    ]
    override_items = [item for item in resolved if item["decision"] == "manual_override"]
    lines.extend(
        f"- {item['index']} · {item['title']} → {item['category']}"
        for item in override_items
    )
    lines.extend(["", "## 全部处理结果", ""])
    lines.extend(
        f"- {item['index']} · {item['title']} → {item['category']}｜{item['review_status']}｜保留决定：{item['retention']}"
        for item in resolved
    )
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return {
        "items": len(items),
        "reviewed": reviewed,
        "overrides": overrides,
        "inherited": inherited,
        "tagged": len(items),
    }
