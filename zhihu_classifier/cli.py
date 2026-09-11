from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import Settings
from .copier import copy_analysis_run
from .db import Database
from .deepseek import DeepSeekClient
from .exporter import export_latest
from .pipeline import analyze_pending, remap_pending
from .scanner import scan_library
from .taxonomy import Taxonomy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="知乎文章智能分类与获批小批次复制")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init-db", help="初始化 SQLite 状态库")

    scan = commands.add_parser("scan", help="只读扫描原始 Markdown 文章")
    scan.add_argument("--source", type=Path, help="临时覆盖原始文章库路径")

    commands.add_parser("status", help="显示数据库统计")

    analyze = commands.add_parser("analyze", help="一次阅读全文并生成永久语义档案和初始映射")
    analyze.add_argument("--limit", type=int, default=10)
    analyze.add_argument("--max-chars", type=int, default=300000)
    analyze.add_argument("--allow-draft", action="store_true", help="明确允许使用未定稿分类试跑")

    remap = commands.add_parser("remap", help="只用永久语义档案映射新版目录，不发送原文")
    remap.add_argument("--limit", type=int, default=100)
    remap.add_argument("--allow-draft", action="store_true", help="明确允许使用未定稿分类试跑")

    export = commands.add_parser("export", help="导出每篇文章的最新分类结果")
    export.add_argument("--output", type=Path)
    export.add_argument("--review-only", action="store_true")

    copy_pilot = commands.add_parser("copy-pilot", help="复制一个不超过10篇的已分析试运行批次")
    copy_pilot.add_argument("--analysis-run-id", type=int, required=True)
    copy_pilot.add_argument("--destination", type=Path)
    copy_pilot.add_argument("--manifest", type=Path)
    copy_pilot.add_argument(
        "--confirm-copy", action="store_true", help="确认本次得到用户明确复制授权"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    database = Database(settings.database_path)
    database.initialize()

    if args.command == "init-db":
        print(f"数据库已初始化：{database.path}")
        return 0

    if args.command == "status":
        print(json.dumps(database.counts(), ensure_ascii=False, indent=2))
        return 0

    taxonomy = Taxonomy.load(
        settings.taxonomy_path,
        allow_candidates=getattr(args, "allow_draft", False),
    )

    if args.command == "copy-pilot":
        if not args.confirm_copy:
            raise SystemExit("复制必须显式添加 --confirm-copy")
        destination = args.destination or taxonomy.classified_library
        manifest = args.manifest or (
            settings.project_root / "exports" / f"copy_run_analysis_{args.analysis_run_id}.json"
        )
        result = copy_analysis_run(
            database,
            taxonomy,
            analysis_run_id=args.analysis_run_id,
            destination_root=destination,
            manifest_path=manifest,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["errors"] else 2

    if args.command == "scan":
        source = args.source or settings.source_library_override or taxonomy.source_library
        stats = scan_library(database, source)
        print(json.dumps(vars(stats), ensure_ascii=False, indent=2))
        return 0 if not stats.errors else 2

    if args.command in {"analyze", "remap"}:
        if args.limit < 1:
            raise SystemExit("--limit 必须大于 0")
        if args.command == "analyze" and args.max_chars < 1000:
            raise SystemExit("--max-chars 必须至少为 1000")
        taxonomy.ensure_classifiable(allow_draft=args.allow_draft)
        if not settings.api_key:
            raise SystemExit("未配置 DEEPSEEK_API_KEY；请复制 .env.example 为 .env 后填写")
        client = DeepSeekClient(
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            thinking=settings.thinking,
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
        )
        if args.command == "analyze":
            result = analyze_pending(
                database, taxonomy, client, limit=args.limit, max_chars=args.max_chars
            )
        else:
            result = remap_pending(database, taxonomy, client, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["errors"] else 2

    if args.command == "export":
        output = args.output or settings.export_path
        if not output.is_absolute():
            output = settings.project_root / output
        if args.review_only and args.output is None:
            output = settings.project_root / "exports/review_queue.jsonl"
        count = export_latest(database, output, review_only=args.review_only)
        print(f"已导出 {count} 条：{output}")
        return 0

    return 1
