from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import shutil

import yaml

from .db import Database
from .image_cleanup import clean_markdown_images
from .taxonomy import Taxonomy


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_part(value: str, fallback: str) -> str:
    value = value.strip()
    if not value or value in {".", ".."} or any(ch in value for ch in '<>:"/\\|?*'):
        return fallback
    return value


def _attachment_files(source_article: Path, source_root: Path) -> tuple[Path, list[Path]]:
    """返回与 Markdown 同名的附件目录及其中普通文件；符号链接一律拒绝。"""
    attachment_root = source_article.with_suffix("")
    if not attachment_root.exists():
        return attachment_root, []
    if not attachment_root.is_dir() or attachment_root.is_symlink():
        raise ValueError(f"同名附件路径不是普通目录：{attachment_root}")
    files: list[Path] = []
    for path in sorted(attachment_root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"附件目录包含符号链接，程序拒绝复制：{path}")
        if path.is_file():
            resolved = path.resolve(strict=True)
            if not _inside(resolved, attachment_root.resolve()) or not _inside(resolved, source_root):
                raise ValueError(f"附件文件超出只读原始库：{path}")
            files.append(resolved)
    return attachment_root.resolve(), files


FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


def _render_obsidian_copy(
    source_bytes: bytes,
    *,
    title: str,
    category: str,
    tags: list[str],
    summary: str,
    source_hash: str,
    taxonomy_version: str,
    needs_review: bool,
    image_report: dict | None = None,
) -> tuple[bytes, dict[str, object]]:
    text = source_bytes.decode("utf-8-sig")
    match = FRONT_MATTER_RE.match(text)
    existing: dict[str, object] = {}
    body = text
    if match:
        parsed = yaml.safe_load(match.group(1)) or {}
        if not isinstance(parsed, dict):
            raise ValueError("原文 YAML Front Matter 顶层不是对象")
        existing = dict(parsed)
        body = text[match.end():]
    generated: dict[str, object] = {
        "title": title,
        "category": category,
        "tags": tags,
        "summary": summary,
        "source_hash": source_hash,
        "taxonomy_version": taxonomy_version,
        "status": "待审核" if needs_review else "已分类",
    }
    frontmatter = {**existing, **generated}
    yaml_text = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).strip()
    body, cleanup = clean_markdown_images(body)
    if image_report is not None:
        image_report.update(asdict(cleanup))
    output = f"---\n{yaml_text}\n---\n\n{body}"
    return output.encode("utf-8"), generated


def copy_analysis_run(
    database: Database,
    taxonomy: Taxonomy,
    *,
    analysis_run_id: int,
    destination_root: Path,
    manifest_path: Path,
) -> dict[str, object]:
    """复制一个明确分析批次的成功结果；从不修改源文件或覆盖不同内容。"""
    source_root = taxonomy.source_library.resolve(strict=True)
    destination_root = destination_root.resolve()
    configured_root = taxonomy.classified_library.resolve()
    if destination_root != configured_root:
        raise ValueError(f"目标必须是 taxonomy.yaml 配置的分类文章库：{configured_root}")

    with database.connect() as connection:
        run = connection.execute(
            "SELECT * FROM analysis_runs WHERE id = ?", (analysis_run_id,)
        ).fetchone()
        if run is None:
            raise ValueError(f"分析批次不存在：{analysis_run_id}")
        rows = connection.execute(
            """
            SELECT a.id AS article_id, a.source_path, a.relative_path, a.title,
                   a.content_hash, aa.id AS analysis_id, ca.id AS assignment_id,
                   ca.level1_id, ca.level1_name, ca.level2_name, ca.confidence,
                   ca.needs_review, ca.candidate_new_topics_json, ca.reason,
                   COALESCE(NULLIF(vr.visual_summary, ''), aa.summary_short) AS summary_short,
                   aa.tags_json, vr.tags_json AS visual_tags_json,
                   vr.retention_decision
            FROM article_analyses aa
            JOIN articles a ON a.id = aa.article_id
            JOIN category_assignments ca ON ca.id = (
                SELECT ca2.id FROM category_assignments ca2
                WHERE ca2.article_id = a.id AND ca2.error IS NULL
                ORDER BY ca2.id DESC LIMIT 1
            )
            LEFT JOIN visual_reviews vr ON vr.id = (
                SELECT vr2.id FROM visual_reviews vr2
                WHERE vr2.article_id = a.id ORDER BY vr2.id DESC LIMIT 1
            )
            WHERE aa.run_id = ? AND aa.error IS NULL
              AND COALESCE(vr.retention_decision, '') != '不纳入'
            ORDER BY aa.id
            """,
            (analysis_run_id,),
        ).fetchall()
        if not rows:
            raise ValueError(f"分析批次 {analysis_run_id} 没有可复制的成功结果")
        if len(rows) > 10:
            raise ValueError("试运行复制一次最多允许 10 篇")

        now = utc_now()
        copy_run_id = connection.execute(
            """
            INSERT INTO copy_runs (
                created_at, analysis_run_id, taxonomy_version, destination_root,
                requested_count, status
            ) VALUES (?, ?, ?, ?, ?, 'running')
            """,
            (now, analysis_run_id, taxonomy.version, str(destination_root), len(rows)),
        ).lastrowid
        connection.commit()

        copied = existing = errors = 0
        attachments_copied = attachments_existing = attachment_errors = 0
        manifest_items: list[dict[str, object]] = []
        for row in rows:
            source = Path(row["source_path"]).resolve(strict=True)
            category = taxonomy.category_by_id(row["level1_id"] or "")
            known = (
                category is not None
                and row["level1_name"] == category.name
                and row["level2_name"] in category.second_level
            )
            needs_review = bool(row["needs_review"]) or not known
            if needs_review:
                level1 = _safe_part(row["level1_name"] or "", "未分类") if known else "未分类"
                level2 = _safe_part(row["level2_name"] or "", "待定") if known else "待定"
                target_dir = destination_root / "_待审核" / level1 / level2
            else:
                target_dir = destination_root / category.name / row["level2_name"]
            target = (target_dir / source.name).resolve()
            disposition = "error"
            error = None
            attachment_items: list[dict[str, object]] = []
            output_hash = None
            generated_frontmatter = None
            try:
                if not _inside(source, source_root):
                    raise ValueError("源文件超出只读原始库")
                if not _inside(target, destination_root):
                    raise ValueError("目标文件超出分类文章库")
                if _sha256(source) != row["content_hash"]:
                    raise ValueError("源文件内容哈希已变化，请重新扫描和分析")
                category_value = (
                    f'{row["level1_name"]}/{row["level2_name"]}' if known else "未分类/待定"
                )
                try:
                    tags = json.loads(row["tags_json"] or "[]")
                    visual_tags = json.loads(row["visual_tags_json"] or "[]")
                except json.JSONDecodeError as exc:
                    raise ValueError("语义档案中的 tags 不是合法 JSON") from exc
                tags = list(dict.fromkeys([*tags, *visual_tags]))
                output_bytes, generated_frontmatter = _render_obsidian_copy(
                    source.read_bytes(),
                    title=row["title"],
                    category=category_value,
                    tags=[str(tag) for tag in tags],
                    summary=row["summary_short"] or "",
                    source_hash=row["content_hash"],
                    taxonomy_version=taxonomy.version,
                    needs_review=needs_review,
                )
                output_hash = hashlib.sha256(output_bytes).hexdigest()
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if _sha256(target) != output_hash:
                        raise FileExistsError("目标已存在但不是当前预期的 Obsidian 副本，程序拒绝覆盖")
                    disposition = "already_exists"
                    existing += 1
                else:
                    target.write_bytes(output_bytes)
                    shutil.copystat(source, target)
                    if _sha256(target) != output_hash:
                        raise OSError("复制后哈希校验失败")
                    disposition = "copied"
                    copied += 1

                source_attachment_root, attachment_files = _attachment_files(source, source_root)
                target_attachment_root = target.with_suffix("")
                for source_attachment in attachment_files:
                    relative_attachment = source_attachment.relative_to(source_attachment_root)
                    target_attachment = (target_attachment_root / relative_attachment).resolve()
                    attachment_disposition = "error"
                    attachment_error = None
                    attachment_hash = _sha256(source_attachment)
                    try:
                        if not _inside(target_attachment, destination_root):
                            raise ValueError("附件目标超出分类文章库")
                        target_attachment.parent.mkdir(parents=True, exist_ok=True)
                        if target_attachment.exists():
                            if not target_attachment.is_file() or _sha256(target_attachment) != attachment_hash:
                                raise FileExistsError("附件目标已存在同名但内容不同的文件，程序拒绝覆盖")
                            attachment_disposition = "already_exists"
                            attachments_existing += 1
                        else:
                            shutil.copy2(source_attachment, target_attachment)
                            if _sha256(target_attachment) != attachment_hash:
                                raise OSError("附件复制后哈希校验失败")
                            attachment_disposition = "copied"
                            attachments_copied += 1
                    except Exception as exc:
                        attachment_error = str(exc)
                        attachment_errors += 1
                        errors += 1
                    attachment_items.append(
                        {
                            "source_path": str(source_attachment),
                            "destination_path": str(target_attachment),
                            "content_hash": attachment_hash,
                            "size_bytes": source_attachment.stat().st_size,
                            "disposition": attachment_disposition,
                            "error": attachment_error,
                        }
                    )
            except Exception as exc:
                error = str(exc)
                errors += 1

            item = {
                "article_id": row["article_id"],
                "title": row["title"],
                "source_path": str(source),
                "destination_path": str(target),
                "content_hash": row["content_hash"],
                "output_content_hash": output_hash,
                "frontmatter": generated_frontmatter,
                "category": f'{row["level1_name"] or ""}/{row["level2_name"] or ""}',
                "confidence": row["confidence"],
                "needs_review": needs_review,
                "reason": row["reason"],
                "disposition": disposition,
                "error": error,
                "attachment_directory": str(target.with_suffix("")) if attachment_items else None,
                "attachment_files": len(attachment_items),
                "attachments_copied": sum(i["disposition"] == "copied" for i in attachment_items),
                "attachments_already_existing": sum(
                    i["disposition"] == "already_exists" for i in attachment_items
                ),
                "attachment_errors": sum(i["disposition"] == "error" for i in attachment_items),
            }
            manifest_items.append(item)
            connection.execute(
                """
                INSERT INTO copy_items (
                    copy_run_id, article_id, analysis_id, assignment_id, source_path,
                    destination_path, content_hash, disposition, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    copy_run_id, row["article_id"], row["analysis_id"], row["assignment_id"],
                    str(source), str(target), row["content_hash"], disposition, error, now,
                ),
            )
            for attachment in attachment_items:
                connection.execute(
                    """
                    INSERT INTO copy_attachments (
                        copy_run_id, article_id, source_path, destination_path,
                        content_hash, size_bytes, disposition, error, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        copy_run_id, row["article_id"], attachment["source_path"],
                        attachment["destination_path"], attachment["content_hash"],
                        attachment["size_bytes"], attachment["disposition"],
                        attachment["error"], now,
                    ),
                )
            if error is None and output_hash and generated_frontmatter:
                connection.execute(
                    """
                    INSERT INTO copy_outputs (
                        copy_run_id, article_id, source_content_hash,
                        output_content_hash, frontmatter_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        copy_run_id, row["article_id"], row["content_hash"], output_hash,
                        json.dumps(generated_frontmatter, ensure_ascii=False), now,
                    ),
                )
            connection.commit()

        status = "completed_with_errors" if errors else "completed"
        connection.execute(
            """
            UPDATE copy_runs SET copied_count=?, existing_count=?, error_count=?, status=?
            WHERE id=?
            """,
            (copied, existing, errors, status, copy_run_id),
        )

    manifest = {
        "copy_run_id": copy_run_id,
        "analysis_run_id": analysis_run_id,
        "taxonomy_version": taxonomy.version,
        "destination_root": str(destination_root),
        "created_at": now,
        "copied": copied,
        "already_exists": existing,
        "errors": errors,
        "attachments_copied": attachments_copied,
        "attachments_already_existing": attachments_existing,
        "attachment_errors": attachment_errors,
        "items": manifest_items,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
