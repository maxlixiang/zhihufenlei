from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
import shutil
from typing import Callable

from .copier import (
    _attachment_files,
    _inside,
    _render_obsidian_copy,
    _safe_part,
    _sha256,
    utc_now,
)
from .db import Database
from .taxonomy import Taxonomy


ProgressCallback = Callable[[dict[str, object]], None]


def _latest_rows(database: Database, taxonomy: Taxonomy, *, strict: bool = True):
    with database.connect() as connection:
        article_count = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        rows = connection.execute(
            """
            SELECT a.id AS article_id, a.source_path, a.relative_path, a.title,
                   a.content_hash, aa.id AS analysis_id, ca.id AS assignment_id,
                   ca.taxonomy_version, ca.level1_id, ca.level1_name, ca.level2_name,
                   ca.confidence, ca.needs_review, ca.reason,
                   COALESCE(NULLIF(vr.visual_summary, ''), aa.summary_short) AS summary_short,
                   aa.tags_json, vr.tags_json AS visual_tags_json,
                   mr.tags_json AS manual_tags_json,
                   vr.retention_decision
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
                WHERE ca2.article_id = a.id AND ca2.analysis_id = aa.id AND ca2.error IS NULL
                  AND ca2.taxonomy_version = ?
                ORDER BY ca2.id DESC LIMIT 1
            )
            LEFT JOIN visual_reviews vr ON vr.id = (
                SELECT vr2.id FROM visual_reviews vr2
                WHERE vr2.article_id = a.id AND vr2.content_hash = a.content_hash ORDER BY vr2.id DESC LIMIT 1
            )
            LEFT JOIN manual_reviews mr ON mr.id = (
                SELECT mr2.id FROM manual_reviews mr2
                WHERE mr2.article_id = a.id AND mr2.content_hash = a.content_hash ORDER BY mr2.id DESC LIMIT 1
            )
            WHERE a.read_error IS NULL AND ca.taxonomy_version = ?
            ORDER BY a.relative_path
            """,
            (taxonomy.version, taxonomy.version),
        ).fetchall()
    if strict and len(rows) != article_count:
        raise ValueError(
            f"共有 {article_count} 篇索引文章，但只有 {len(rows)} 篇具有当前内容哈希和规则"
            f" {taxonomy.version} 的成功分类；拒绝不完整复制"
        )
    return rows


def copy_all_current(
    database: Database,
    taxonomy: Taxonomy,
    *,
    destination_root: Path,
    manifest_path: Path,
    require_empty: bool = True,
    progress: ProgressCallback | None = None,
    incremental: bool = False,
) -> dict[str, object]:
    """把所有当前分类复制为可重建的 Obsidian 文章库；永不修改源文件。"""
    taxonomy.ensure_classifiable(allow_draft=False)
    source_root = taxonomy.source_library.resolve(strict=True)
    if _inside(destination_root.resolve(), source_root) or _inside(source_root, destination_root.resolve()):
        raise ValueError("原始库与分类库必须互不包含")
    destination_root = destination_root.resolve(strict=True)
    configured_root = taxonomy.classified_library.resolve(strict=True)
    if destination_root != configured_root:
        raise ValueError(f"目标必须是 taxonomy.yaml 配置的分类文章库：{configured_root}")
    if not destination_root.is_dir() or destination_root.is_symlink():
        raise ValueError("分类文章库必须是普通目录，不能是符号链接")
    if require_empty and any(destination_root.iterdir()):
        raise ValueError("全量首轮复制要求分类文章库为空；检测到现有内容，已停止")
    if _inside(manifest_path.resolve(), source_root) or _inside(manifest_path.resolve(), destination_root):
        raise ValueError("复制清单必须保存在原始库和分类库之外")
    if manifest_path.exists():
        raise FileExistsError(f"复制清单已存在，拒绝覆盖：{manifest_path}")

    rows = _latest_rows(database, taxonomy, strict=not incremental)
    skipped = excluded = pending = 0
    conflict_items = []
    if incremental:
        with database.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            # 已成功输出的旧笔记按历史记录跳过，保护用户在 Obsidian 中的修改。
            completed = connection.execute("""
                SELECT source_content_hash AS content_hash FROM full_copy_items i
                JOIN full_copy_runs r ON r.id=i.full_copy_run_id
                WHERE r.destination_root=? AND i.error IS NULL
                  AND i.disposition IN ('copied', 'already_exists')
                UNION
                SELECT content_hash FROM copy_items i JOIN copy_runs r ON r.id=i.copy_run_id
                WHERE r.destination_root=? AND i.error IS NULL
                  AND i.disposition IN ('copied', 'already_exists')
                  AND NOT EXISTS (SELECT 1 FROM copy_attachments a
                      WHERE a.copy_run_id=i.copy_run_id AND a.article_id=i.article_id
                        AND a.error IS NOT NULL)
            """, (str(destination_root), str(destination_root))).fetchall()
            previous = connection.execute("""
                SELECT article_id, source_content_hash AS content_hash FROM full_copy_items i
                JOIN full_copy_runs r ON r.id=i.full_copy_run_id
                WHERE r.destination_root=? AND i.error IS NULL
                  AND i.disposition IN ('copied', 'already_exists')
                UNION
                SELECT article_id, content_hash FROM copy_items i JOIN copy_runs r ON r.id=i.copy_run_id
                WHERE r.destination_root=? AND i.error IS NULL
                  AND i.disposition IN ('copied', 'already_exists')
            """, (str(destination_root), str(destination_root))).fetchall()
        previous_hashes = {}
        for row in previous:
            previous_hashes.setdefault(row["article_id"], set()).add(row["content_hash"])
        completed_hashes = {row["content_hash"] for row in completed}
        pending = total - len(rows)
        selected = []
        seen_hashes = set(completed_hashes)
        for row in rows:
            if row["retention_decision"] == "不纳入":
                excluded += 1
            elif row["content_hash"] in seen_hashes:
                skipped += 1
            elif row["article_id"] in previous_hashes and row["content_hash"] not in previous_hashes[row["article_id"]]:
                conflict_items.append({"article_id": row["article_id"], "title": row["title"],
                                       "reason": "原始内容已变化，已有笔记需人工决定如何更新"})
            else:
                selected.append(row)
                seen_hashes.add(row["content_hash"])
        rows = selected
    if incremental:
        target_counts = Counter(_target_path(destination_root, row, taxonomy) for row in rows)
        nonconflicting = []
        for row in rows:
            target = _target_path(destination_root, row, taxonomy)
            if target_counts[target] > 1:
                conflict_items.append({"article_id": row["article_id"], "title": row["title"],
                                       "reason": f"多篇文章将写入同一路径：{target}"})
            else:
                nonconflicting.append(row)
        rows = nonconflicting
    targets: set[Path] = set()
    for row in rows:
        category = taxonomy.category_by_id(row["level1_id"] or "")
        known = (category is not None and row["level1_name"] == category.name
                 and row["level2_name"] in category.second_level)
        if not known and not incremental:
            raise ValueError(f"文章 {row['article_id']} 的当前分类不属于规则 {taxonomy.version}")
        target = _target_path(destination_root, row, taxonomy)
        if target in targets:
            raise ValueError(f"两篇文章会写入同一目标路径：{target}")
        targets.add(target)

    now = utc_now()
    with database.connect() as connection:
        run_id = connection.execute(
            """
            INSERT INTO full_copy_runs (
                created_at, taxonomy_version, destination_root, requested_count,
                status, manifest_path
            ) VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (now, taxonomy.version, str(destination_root), len(rows), str(manifest_path.resolve())),
        ).lastrowid

    copied = existing = errors = 0
    attachments_copied = attachments_existing = attachment_errors = 0
    manifest_items: list[dict[str, object]] = []
    for index, row in enumerate(rows, start=1):
        source = Path(row["source_path"])
        target = _target_path(destination_root, row, taxonomy)
        disposition = "error"
        error: str | None = None
        output_hash: str | None = None
        generated_frontmatter: dict[str, object] | None = None
        attachment_items: list[dict[str, object]] = []
        image_report: dict = {}
        try:
            source = source.resolve(strict=True)
            if not _inside(source, source_root) or source.is_symlink():
                raise ValueError("源文件超出只读原始库或是符号链接")
            if not _inside(target, destination_root):
                raise ValueError("目标文件超出分类文章库")
            if _sha256(source) != row["content_hash"]:
                raise ValueError("源文件内容哈希已变化，请重新扫描和分析")
            try:
                semantic_tags = json.loads(row["tags_json"] or "[]")
                visual_tags = json.loads(row["visual_tags_json"] or "[]")
                manual_tags = json.loads(row["manual_tags_json"] or "[]")
            except json.JSONDecodeError as exc:
                raise ValueError("文章标签不是合法 JSON") from exc
            tags = list(dict.fromkeys([*semantic_tags, *visual_tags, *manual_tags]))
            output_bytes, generated_frontmatter = _render_obsidian_copy(
                source.read_bytes(),
                title=row["title"],
                category=f'{row["level1_name"]}/{row["level2_name"]}',
                tags=[str(tag) for tag in tags],
                summary=row["summary_short"] or "",
                source_hash=row["content_hash"],
                taxonomy_version=taxonomy.version,
                needs_review=bool(row["needs_review"]),
                image_report=image_report,
            )
            output_hash = hashlib.sha256(output_bytes).hexdigest()
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not target.is_file() or _sha256(target) != output_hash:
                    raise FileExistsError("目标已存在但内容不同，程序拒绝覆盖")
                disposition = "already_exists"
                existing += 1
            else:
                _write_verified(target, output_hash, data=output_bytes)
                shutil.copystat(source, target)
                if _sha256(target) != output_hash:
                    raise OSError("Markdown复制后哈希校验失败")
                disposition = "copied"
                copied += 1

            source_attachment_root, attachment_files = _attachment_files(source, source_root)
            target_attachment_root = target.with_suffix("")
            for source_attachment in attachment_files:
                relative_attachment = source_attachment.relative_to(source_attachment_root)
                target_attachment = (target_attachment_root / relative_attachment).resolve()
                attachment_disposition = "error"
                attachment_error: str | None = None
                attachment_hash = _sha256(source_attachment)
                try:
                    if not _inside(target_attachment, destination_root):
                        raise ValueError("附件目标超出分类文章库")
                    target_attachment.parent.mkdir(parents=True, exist_ok=True)
                    if target_attachment.exists():
                        if not target_attachment.is_file() or _sha256(target_attachment) != attachment_hash:
                            raise FileExistsError("附件目标已存在但内容不同，程序拒绝覆盖")
                        attachment_disposition = "already_exists"
                        attachments_existing += 1
                    else:
                        _write_verified(target_attachment, attachment_hash, source=source_attachment)
                        shutil.copystat(source_attachment, target_attachment)
                        if _sha256(target_attachment) != attachment_hash:
                            raise OSError("附件复制后哈希校验失败")
                        attachment_disposition = "copied"
                        attachments_copied += 1
                except Exception as exc:
                    attachment_error = str(exc)
                    attachment_errors += 1
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
            if any(item["error"] for item in attachment_items):
                error = "一个或多个附件复制失败"
                errors += 1
        except Exception as exc:
            error = str(exc)
            errors += 1

        item = {
            "article_id": row["article_id"],
            "title": row["title"],
            "source_path": str(source),
            "destination_path": str(target),
            "source_content_hash": row["content_hash"],
            "output_content_hash": output_hash,
            "frontmatter": generated_frontmatter,
            "image_cleanup": image_report,
            "category": f'{row["level1_name"]}/{row["level2_name"]}',
            "confidence": row["confidence"],
            "needs_review": bool(row["needs_review"]),
            "retention_decision": row["retention_decision"],
            "disposition": disposition,
            "error": error,
            "attachment_files": len(attachment_items),
            "attachments_copied": sum(i["disposition"] == "copied" for i in attachment_items),
            "attachments_already_existing": sum(
                i["disposition"] == "already_exists" for i in attachment_items
            ),
            "attachment_errors": sum(i["disposition"] == "error" for i in attachment_items),
        }
        manifest_items.append(item)
        with database.connect() as connection:
            connection.execute(
                """
                INSERT INTO full_copy_items (
                    full_copy_run_id, article_id, analysis_id, assignment_id,
                    source_path, destination_path, source_content_hash,
                    output_content_hash, frontmatter_json, disposition, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, row["article_id"], row["analysis_id"], row["assignment_id"],
                 str(source), str(target), row["content_hash"], output_hash,
                 json.dumps(generated_frontmatter, ensure_ascii=False)
                 if generated_frontmatter is not None else None,
                 disposition, error, now),
            )
            for attachment in attachment_items:
                connection.execute(
                    """
                    INSERT INTO full_copy_attachments (
                        full_copy_run_id, article_id, source_path, destination_path,
                        content_hash, size_bytes, disposition, error, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, row["article_id"], attachment["source_path"],
                     attachment["destination_path"], attachment["content_hash"],
                     attachment["size_bytes"], attachment["disposition"],
                     attachment["error"], now),
                )
        if progress is not None:
            progress({"current": index, "total": len(rows), "copied": copied,
                      "existing": existing, "errors": errors, "title": row["title"]})

    completed_at = utc_now()
    status = "completed" if errors == 0 and attachment_errors == 0 else "completed_with_errors"
    manifest = {
        "full_copy_run_id": run_id,
        "taxonomy_version": taxonomy.version,
        "destination_root": str(destination_root),
        "created_at": now,
        "completed_at": completed_at,
        "requested": len(rows),
        "copied": copied,
        "already_exists": existing,
        "errors": errors,
        "attachments_copied": attachments_copied,
        "attachments_already_existing": attachments_existing,
        "attachment_errors": attachment_errors,
        "status": status,
        "mode": "incremental" if incremental else "full",
        "skipped": skipped,
        "excluded": excluded,
        "pending": pending,
        "conflicts": len(conflict_items),
        "conflict_items": conflict_items,
        "image_cleanup": {
            name: sum(item.get("image_cleanup", {}).get(name, 0)
                      for item in manifest_items if item.get("disposition") in {"copied", "already_exists"})
            for name in ("placeholders_removed", "alt_text_fixed", "remote_images_remaining")
        },
        "items": manifest_items,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE full_copy_runs SET completed_at=?, copied_count=?, existing_count=?,
                error_count=?, attachment_copied_count=?, attachment_existing_count=?,
                attachment_error_count=?, status=? WHERE id=?
            """,
            (completed_at, copied, existing, errors, attachments_copied,
             attachments_existing, attachment_errors, status, run_id),
        )
    return manifest


def _target_path(destination_root: Path, row, taxonomy: Taxonomy) -> Path:
    category = taxonomy.category_by_id(row["level1_id"] or "")
    known = (category is not None and row["level1_name"] == category.name
             and row["level2_name"] in category.second_level)
    if known:
        directory = destination_root / category.name / row["level2_name"]
    else:
        directory = destination_root / "_待审核" / "未分类" / "待定"
    return (directory / Path(row["source_path"]).name).resolve()


def copy_incremental(database: Database, taxonomy: Taxonomy, *, destination_root: Path,
                     manifest_path: Path, progress: ProgressCallback | None = None):
    """只追加未成功输出的文章；失败文章可重试，旧笔记永不覆盖或搬迁。"""
    return copy_all_current(database, taxonomy, destination_root=destination_root,
                            manifest_path=manifest_path, require_empty=False,
                            progress=progress, incremental=True)


def _write_verified(target: Path, expected_hash: str, *, data: bytes | None = None,
                    source: Path | None = None) -> None:
    """先在同目录验证临时文件，再以硬链接独占发布，避免失败留下半成品或覆盖旧文件。"""
    descriptor, name = tempfile.mkstemp(prefix=".zhihu-copy-", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as writer:
            if source is not None:
                with source.open("rb") as reader:
                    shutil.copyfileobj(reader, writer)
            else:
                writer.write(data or b"")
        if _sha256(temporary) != expected_hash:
            raise OSError("临时副本哈希校验失败")
        # 同文件系统的独占创建；目标存在时失败，绝不替换用户已有文件。
        os.link(temporary, target)
    finally:
        # 只删除本次创建的一个明确临时文件，不递归清理任何目录。
        temporary.unlink(missing_ok=True)
