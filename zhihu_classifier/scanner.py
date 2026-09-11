from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from .db import Database


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def title_from_path(path: Path) -> str:
    stem = path.stem
    if stem.startswith("[") and "]" in stem:
        stem = stem.split("]", 1)[1].strip()
    return stem or path.name


@dataclass
class ScanStats:
    discovered: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0


def scan_library(database: Database, source_root: Path) -> ScanStats:
    """只读扫描 Markdown 文件。函数不会在 source_root 内创建或修改任何内容。"""
    if not source_root.is_dir():
        raise FileNotFoundError(f"原始文章库不存在或不是目录：{source_root}")

    source_root = source_root.resolve()
    stats = ScanStats()
    now = utc_now()
    with database.connect() as connection:
        for path in sorted(source_root.rglob("*.md")):
            try:
                resolved = path.resolve(strict=True)
                if source_root not in resolved.parents:
                    raise ValueError("文件解析后超出原始库范围")
                data = resolved.read_bytes()
                content_hash = hashlib.sha256(data).hexdigest()
                stat = resolved.stat()
                error = None
                try:
                    data.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    error = f"UTF-8 解码失败：{exc}"
                    stats.errors += 1
                existing = connection.execute(
                    "SELECT content_hash, mtime_ns, read_error FROM articles WHERE source_path = ?",
                    (str(resolved),),
                ).fetchone()
                values = (
                    str(resolved),
                    str(resolved.relative_to(source_root)),
                    title_from_path(resolved),
                    content_hash,
                    stat.st_size,
                    stat.st_mtime_ns,
                    now,
                    now,
                    error,
                )
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO articles (
                            source_path, relative_path, title, content_hash, size_bytes,
                            mtime_ns, discovered_at, updated_at, read_error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                    stats.discovered += 1
                elif (
                    existing["content_hash"] != content_hash
                    or existing["mtime_ns"] != stat.st_mtime_ns
                    or existing["read_error"] != error
                ):
                    connection.execute(
                        """
                        UPDATE articles SET relative_path=?, title=?, content_hash=?, size_bytes=?,
                            mtime_ns=?, updated_at=?, read_error=? WHERE source_path=?
                        """,
                        (
                            str(resolved.relative_to(source_root)),
                            title_from_path(resolved),
                            content_hash,
                            stat.st_size,
                            stat.st_mtime_ns,
                            now,
                            error,
                            str(resolved),
                        ),
                    )
                    stats.updated += 1
                else:
                    stats.unchanged += 1
            except (OSError, ValueError) as exc:
                stats.errors += 1
                print(f"扫描失败：{path}：{exc}")
    return stats
