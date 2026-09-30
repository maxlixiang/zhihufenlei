from __future__ import annotations

from pathlib import Path

from .full_copier import copy_incremental
from .pipeline import analyze_pending, remap_pending, reuse_duplicate_analyses
from .scanner import scan_library


def update_library(database, taxonomy, client_factory, *, manifest_path: Path,
                   max_chars: int = 300000, analysis_progress=None, copy_progress=None):
    """扫描、复用、整批分析和追加复制；每次只尝试一次待处理队列，失败留待下次。"""
    source = taxonomy.source_library.resolve(strict=True)
    destination = taxonomy.classified_library.resolve(strict=True)
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("原始库与分类库必须互不包含")
    if manifest_path.exists():
        raise FileExistsError(f"复制清单已存在，拒绝覆盖：{manifest_path}")
    scan = scan_library(database, taxonomy.source_library)
    reused = reuse_duplicate_analyses(database, taxonomy)
    pending = database.counts()["pending_full_text_analysis"]
    analysis = {"selected": 0, "processed": 0, "errors": 0, "reused": 0}
    client = None
    if pending:
        client = client_factory()
        analysis = analyze_pending(database, taxonomy, client, limit=pending,
                                   max_chars=max_chars, progress=analysis_progress)
    with database.connect() as connection:
        missing = connection.execute("""
            SELECT COUNT(*) FROM articles a WHERE EXISTS (
                SELECT 1 FROM article_analyses aa WHERE aa.article_id=a.id
                  AND aa.content_hash=a.content_hash AND aa.error IS NULL
                  AND NOT EXISTS (SELECT 1 FROM category_assignments ca
                      WHERE ca.analysis_id=aa.id AND ca.taxonomy_version=? AND ca.error IS NULL))
        """, (taxonomy.version,)).fetchone()[0]
    mapping = {"selected": 0, "processed": 0, "errors": 0}
    if missing:
        client = client or client_factory()
        mapping = remap_pending(database, taxonomy, client, limit=missing,
                                only_unassigned=True, progress=analysis_progress)
    copied = copy_incremental(database, taxonomy, destination_root=taxonomy.classified_library,
                              manifest_path=manifest_path, progress=copy_progress)
    return {"scan": vars(scan), "reused": reused + analysis.get("reused", 0),
            "analysis": analysis, "mapping": mapping,
            "copy": {k: v for k, v in copied.items() if k != "items"},
            "pending_analysis": database.counts()["pending_full_text_analysis"]}
