from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import uuid

from .audit import generate_quality_report
from .config import Settings
from .copier import copy_analysis_run
from .db import Database
from .deepseek import DeepSeekClient
from .exporter import export_latest
from .full_copier import copy_all_current, copy_incremental
from .incremental import update_library
from .pipeline import (
    analyze_pending,
    recheck_candidate_assignments,
    remap_pending,
    repair_failed_analyses,
    repair_saved_assignments_locally,
    repair_saved_analyses_locally,
)
from .review import export_low_confidence_review, import_low_confidence_review
from .scanner import scan_library
from .taxonomy import Taxonomy
from .visual_review import export_visual_review, import_visual_review


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="知乎文章智能分类与获批小批次复制")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init-db", help="初始化 SQLite 状态库")

    scan = commands.add_parser("scan", help="只读扫描原始 Markdown 文章")
    scan.add_argument("--source", type=Path, help="临时覆盖原始文章库路径")

    commands.add_parser("status", help="显示数据库统计")

    watch = commands.add_parser("watch", help="在 PowerShell 中实时显示分析进度")
    watch.add_argument("--interval", type=float, default=10.0, help="刷新间隔秒数")
    watch.add_argument("--once", action="store_true", help="只显示一次后退出")

    analyze = commands.add_parser("analyze", help="一次阅读全文并生成永久语义档案和初始映射")
    analyze.add_argument("--limit", type=int, default=10)
    analyze.add_argument("--max-chars", type=int, default=300000)
    analyze.add_argument("--allow-draft", action="store_true", help="明确允许使用未定稿分类试跑")
    analyze.add_argument("--quiet", action="store_true", help="不显示逐篇实时进度")

    remap = commands.add_parser("remap", help="只用永久语义档案映射新版目录，不发送原文")
    remap.add_argument("--limit", type=int, default=100)
    remap.add_argument("--allow-draft", action="store_true", help="明确允许使用未定稿分类试跑")
    remap.add_argument("--quiet", action="store_true", help="不显示逐篇实时进度")

    repair = commands.add_parser("repair-analysis", help="用已保存响应修复失败分析，不重读正文")
    repair.add_argument("--limit", type=int, default=100)
    repair.add_argument("--allow-draft", action="store_true", help="明确允许使用未定稿分类试跑")
    repair.add_argument("--quiet", action="store_true", help="不显示逐篇实时进度")

    repair_saved = commands.add_parser(
        "repair-saved",
        help="完全在本地校正已保存响应的 JSON 层级，不调用 API",
    )
    repair_saved.add_argument("--limit", type=int, default=100)
    repair_saved.add_argument("--allow-draft", action="store_true", help="明确允许使用未定稿分类试跑")
    repair_saved.add_argument("--quiet", action="store_true", help="不显示逐篇实时进度")

    repair_assignment_saved = commands.add_parser(
        "repair-assignment-saved",
        help="完全在本地解包并验证失败映射的已保存 JSON",
    )
    repair_assignment_saved.add_argument("--limit", type=int, default=100)
    repair_assignment_saved.add_argument("--allow-draft", action="store_true", help="明确允许使用未定稿分类试跑")
    repair_assignment_saved.add_argument("--quiet", action="store_true", help="不显示逐篇实时进度")

    recheck = commands.add_parser(
        "recheck-candidates",
        help="只用语义档案复核未归类和候选主题",
    )
    recheck.add_argument("--limit", type=int, default=200)
    recheck.add_argument("--allow-draft", action="store_true", help="明确允许使用未定稿分类试跑")
    recheck.add_argument("--quiet", action="store_true", help="不显示逐篇实时进度")

    audit = commands.add_parser("audit", help="生成分类质量与抽查报告")
    audit.add_argument("--output", type=Path)
    audit.add_argument("--sample-per-band", type=int, default=15)

    export = commands.add_parser("export", help="导出每篇文章的最新分类结果")
    export.add_argument("--output", type=Path)
    export.add_argument("--review-only", action="store_true")

    export_review = commands.add_parser(
        "export-review",
        help="导出可在 Obsidian 中填写的低置信度人工审核清单",
    )
    export_review.add_argument("--output", type=Path)
    export_review.add_argument("--below", type=float, default=0.75)

    import_review = commands.add_parser(
        "import-review", help="导入低置信度人工审核并本地继承兼容分类"
    )
    import_review.add_argument("--input", type=Path)
    import_review.add_argument("--result-output", type=Path)

    export_visual_review_parser = commands.add_parser(
        "export-visual-review",
        help="只读识别图片主导文章并导出人工看图审核清单",
    )
    export_visual_review_parser.add_argument("--output", type=Path)
    export_visual_review_parser.add_argument("--max-text-chars", type=int, default=200)
    export_visual_review_parser.add_argument("--min-images", type=int, default=1)

    import_visual_review_parser = commands.add_parser(
        "import-visual-review",
        help="导入人工看图结果并保存人工分类覆盖和图片主导标签",
    )
    import_visual_review_parser.add_argument("--input", type=Path)
    import_visual_review_parser.add_argument("--result-output", type=Path)
    import_visual_review_parser.add_argument(
        "--accept-unfilled-current",
        action="store_true",
        help="明确允许未填写项沿用当前分类",
    )

    copy_pilot = commands.add_parser("copy-pilot", help="复制一个不超过10篇的已分析试运行批次")
    copy_pilot.add_argument("--analysis-run-id", type=int, required=True)
    copy_pilot.add_argument("--destination", type=Path)
    copy_pilot.add_argument("--manifest", type=Path)
    copy_pilot.add_argument(
        "--confirm-copy", action="store_true", help="确认本次得到用户明确复制授权"
    )

    copy_all = commands.add_parser(
        "copy-all", help="把当前规则下的全部文章复制为Obsidian分类库"
    )
    copy_all.add_argument("--destination", type=Path)
    copy_all.add_argument("--manifest", type=Path)
    copy_all.add_argument(
        "--confirm-copy-all", action="store_true", help="确认用户明确授权全量复制"
    )
    for name, help_text in (
        ("update", "扫描、分析整批新增文章并追加到现有Obsidian库"),
        ("copy-new", "只追加尚未成功复制的分类结果，不调用模型"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--confirm-copy", action="store_true", help="确认本次复制授权")
        command.add_argument("--manifest", type=Path)
        if name == "update":
            command.add_argument("--max-chars", type=int, default=300000)
    return parser


def _print_item_progress(event: dict) -> None:
    labels = {
        "full_analysis": "全文分析",
        "semantic_remap": "语义重映射",
        "analysis_repair": "结构修复",
        "local_structure_repair": "本地结构修复",
        "candidate_recheck": "候选复核",
    }
    total = int(event["total"])
    current = int(event["current"])
    percent = (current / total * 100) if total else 100.0
    title = str(event.get("title", "")).replace("\r", " ").replace("\n", " ")[:36]
    line = (
        f"[{labels.get(event['phase'], event['phase'])}] "
        f"{current}/{total} ({percent:5.1f}%) "
        f"成功 {event['processed']} | 错误 {event['errors']} | {title}"
    )
    print("\r" + line.ljust(110), end="", flush=True)


def _print_status_dashboard(counts: dict[str, int], *, clear: bool) -> None:
    if clear:
        print("\033[2J\033[H", end="")
    print("知乎文章 DeepSeek 全量语义分析")
    print("更新时间：", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("总文章数：", counts["articles"])
    print("已完成语义分析：", counts["current_analyses"])
    print("剩余待分析：", counts["pending_full_text_analysis"])
    print("当前分析错误：", counts["current_analysis_errors"])
    print("待人工审核：", counts["latest_needs_review"])
    print("累计 API Token：", counts["api_total_tokens"])
    print("运行中的全文分析批次：", counts["running_analysis_runs"])
    print("说明：这里只显示分析进度，不会复制文章。按 Ctrl+C 可关闭面板。")


def _print_copy_progress(event: dict[str, object]) -> None:
    total = int(event["total"])
    current = int(event["current"])
    percent = (current / total * 100) if total else 100.0
    title = str(event.get("title", "")).replace("\r", " ").replace("\n", " ")[:36]
    line = (
        f"[全量复制] {current}/{total} ({percent:5.1f}%) "
        f"新增 {event['copied']} | 已存在 {event['existing']} | 错误 {event['errors']} | {title}"
    )
    print("\r" + line.ljust(120), end="", flush=True)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    if args.command in {"update", "copy-new"} and not args.confirm_copy:
        raise SystemExit("追加复制必须在用户授权后显式添加 --confirm-copy")
    if args.command == "update" and args.max_chars < 1000:
        raise SystemExit("--max-chars 必须至少为 1000")
    settings = Settings.load()
    database = Database(settings.database_path)
    database.initialize()

    if args.command == "init-db":
        print(f"数据库已初始化：{database.path}")
        return 0

    if args.command == "status":
        print(json.dumps(database.counts(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "watch":
        if args.interval <= 0:
            raise SystemExit("--interval 必须大于 0")
        try:
            while True:
                counts = database.counts()
                _print_status_dashboard(counts, clear=not args.once)
                if args.once or counts["pending_full_text_analysis"] == 0:
                    return 0
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n已关闭进度面板；后台分析任务不受影响。")
            return 0

    taxonomy = Taxonomy.load(
        settings.taxonomy_path,
        allow_candidates=getattr(args, "allow_draft", False),
    )

    if args.command in {"update", "copy-new"}:
        taxonomy.ensure_classifiable(allow_draft=False)
        if settings.source_library_override:
            from dataclasses import replace
            taxonomy = replace(taxonomy, source_library=settings.source_library_override)
        manifest = args.manifest or (
            settings.project_root / "exports" /
            f"INCREMENTAL_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
        )
        if not manifest.is_absolute():
            manifest = settings.project_root / manifest
        if manifest.exists():
            raise SystemExit(f"复制清单已存在，拒绝覆盖：{manifest}")
        def client_factory():
            return DeepSeekClient(api_key=settings.api_key or "", base_url=settings.base_url,
                                  model=settings.model, thinking=settings.thinking,
                                  timeout_seconds=settings.timeout_seconds, max_retries=settings.max_retries)
        try:
            if args.command == "update":
                result = update_library(database, taxonomy, client_factory, manifest_path=manifest,
                                        max_chars=args.max_chars, analysis_progress=_print_item_progress,
                                        copy_progress=_print_copy_progress)
                failed = (result["scan"]["errors"] or result["analysis"]["errors"]
                          or result["mapping"]["errors"] or result["copy"]["errors"]
                          or result["copy"]["pending"] or result["copy"].get("conflicts", 0))
            else:
                result = copy_incremental(database, taxonomy, destination_root=taxonomy.classified_library,
                                          manifest_path=manifest, progress=_print_copy_progress)
                result = {k: v for k, v in result.items() if k != "items"}
                failed = result["errors"] or result["pending"] or result.get("conflicts", 0)
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc
        print()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if failed else 0

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

    if args.command == "copy-all":
        if not args.confirm_copy_all:
            raise SystemExit("全量复制必须在用户明确授权后添加 --confirm-copy-all")
        destination = args.destination or taxonomy.classified_library
        manifest = args.manifest or (
            settings.project_root / "exports" / f"FULL_COPY_{taxonomy.version}.json"
        )
        if not manifest.is_absolute():
            manifest = settings.project_root / manifest
        try:
            result = copy_all_current(
                database,
                taxonomy,
                destination_root=destination,
                manifest_path=manifest,
                require_empty=True,
                progress=_print_copy_progress,
            )
        except (OSError, UnicodeError, ValueError, FileExistsError) as exc:
            raise SystemExit(str(exc)) from exc
        print()
        summary = {key: result[key] for key in (
            "full_copy_run_id", "taxonomy_version", "requested", "copied",
            "already_exists", "errors", "attachments_copied",
            "attachments_already_existing", "attachment_errors", "status",
        )}
        summary["manifest"] = str(manifest)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 2

    if args.command == "scan":
        source = args.source or settings.source_library_override or taxonomy.source_library
        stats = scan_library(database, source)
        print(json.dumps(vars(stats), ensure_ascii=False, indent=2))
        return 0 if not stats.errors else 2

    if args.command == "audit":
        if args.sample_per_band < 1:
            raise SystemExit("--sample-per-band 必须大于 0")
        output = args.output or (
            settings.project_root / "reports" / "CLASSIFICATION_QUALITY_REPORT.md"
        )
        if not output.is_absolute():
            output = settings.project_root / output
        result = generate_quality_report(
            database,
            taxonomy,
            output,
            sample_per_band=args.sample_per_band,
        )
        print(json.dumps({**result, "output": str(output)}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "export-review":
        output = args.output or (
            settings.project_root / "reports" / "LOW_CONFIDENCE_REVIEW.md"
        )
        if not output.is_absolute():
            output = settings.project_root / output
        try:
            count = export_low_confidence_review(
                database,
                taxonomy,
                output,
                below=args.below,
            )
        except (ValueError, FileExistsError) as exc:
            raise SystemExit(str(exc)) from exc
        print(f"已导出 {count} 条人工审核项：{output}")
        return 0

    if args.command == "export-visual-review":
        output = args.output or (
            settings.project_root / "reports" / "IMAGE_DOMINANT_REVIEW.md"
        )
        if not output.is_absolute():
            output = settings.project_root / output
        try:
            count = export_visual_review(
                database,
                taxonomy,
                output,
                max_text_chars=args.max_text_chars,
                min_images=args.min_images,
            )
        except (ValueError, FileExistsError) as exc:
            raise SystemExit(str(exc)) from exc
        print(f"已导出 {count} 条图片主导审核项：{output}")
        return 0

    if args.command == "import-review":
        input_path = args.input or (
            settings.project_root / "reports" / "LOW_CONFIDENCE_REVIEW.md"
        )
        result_path = args.result_output or (
            settings.project_root / "reports" / "LOW_CONFIDENCE_REVIEW_APPLIED.md"
        )
        if not input_path.is_absolute():
            input_path = settings.project_root / input_path
        if not result_path.is_absolute():
            result_path = settings.project_root / result_path
        try:
            result = import_low_confidence_review(
                database, taxonomy, input_path, result_path
            )
        except (OSError, UnicodeError, ValueError, FileExistsError, json.JSONDecodeError) as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({**result, "output": str(result_path)}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "import-visual-review":
        input_path = args.input or (
            settings.project_root / "reports" / "IMAGE_DOMINANT_REVIEW.md"
        )
        result_path = args.result_output or (
            settings.project_root / "reports" / "IMAGE_DOMINANT_REVIEW_APPLIED.md"
        )
        if not input_path.is_absolute():
            input_path = settings.project_root / input_path
        if not result_path.is_absolute():
            result_path = settings.project_root / result_path
        try:
            result = import_visual_review(
                database,
                taxonomy,
                input_path,
                result_path,
                accept_unfilled_current=args.accept_unfilled_current,
            )
        except (OSError, UnicodeError, ValueError, FileExistsError) as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({**result, "output": str(result_path)}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "repair-saved":
        if args.limit < 1:
            raise SystemExit("--limit 必须大于 0")
        taxonomy.ensure_classifiable(allow_draft=args.allow_draft)
        progress = None if args.quiet else _print_item_progress
        result = repair_saved_analyses_locally(
            database,
            taxonomy,
            limit=args.limit,
            progress=progress,
        )
        if not args.quiet:
            print()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["errors"] else 2

    if args.command == "repair-assignment-saved":
        if args.limit < 1:
            raise SystemExit("--limit 必须大于 0")
        taxonomy.ensure_classifiable(allow_draft=args.allow_draft)
        progress = None if args.quiet else _print_item_progress
        result = repair_saved_assignments_locally(
            database,
            taxonomy,
            limit=args.limit,
            progress=progress,
        )
        if not args.quiet:
            print()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["errors"] else 2

    if args.command in {
        "analyze",
        "remap",
        "repair-analysis",
        "recheck-candidates",
    }:
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
        progress = None if args.quiet else _print_item_progress
        if args.command == "analyze":
            result = analyze_pending(
                database,
                taxonomy,
                client,
                limit=args.limit,
                max_chars=args.max_chars,
                progress=progress,
            )
        elif args.command == "remap":
            result = remap_pending(
                database,
                taxonomy,
                client,
                limit=args.limit,
                progress=progress,
            )
        elif args.command == "repair-analysis":
            result = repair_failed_analyses(
                database,
                taxonomy,
                client,
                limit=args.limit,
                progress=progress,
            )
        else:
            result = recheck_candidate_assignments(
                database,
                taxonomy,
                client,
                limit=args.limit,
                progress=progress,
            )
        if not args.quiet:
            print()
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
