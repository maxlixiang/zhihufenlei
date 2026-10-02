"""One entry point for Playwright download, append-only archival and classification."""
import argparse
import json
from pathlib import Path
import sys
import yaml

from workflow_runner import WorkflowConfig, run_workflow


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="知乎下载、原始备份和Obsidian分类统一入口")
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).with_name("workflow.example.yaml"))
    parser.add_argument("--confirm-run", action="store_true", help="授权本次下载、追加备份、模型分析及分类复制")
    parser.add_argument("--dry-run", action="store_true", help="只读预览现有待同步文件，不下载、不写入、不调用模型")
    parser.add_argument("--skip-download", action="store_true", help="跳过下载，补同步和分类已有下载文件")
    args = parser.parse_args(argv)
    try:
        summary = run_workflow(WorkflowConfig.load(args.config), confirm=args.confirm_run,
                               dry_run=args.dry_run, skip_download=args.skip_download)
    except (OSError, ValueError, KeyError, RuntimeError, yaml.YAMLError) as exc:
        parser.exit(2, str(exc) + "\n")
    display = dict(summary)
    sync = summary.get("sync")
    if isinstance(sync, dict):
        display["sync"] = {key: sync[key] for key in (
            "copied", "would_copy", "skipped", "attachments_copied")}
        display["sync"].update(errors=len(sync["errors"]), warnings=len(sync["warnings"]),
                               historical_index_warnings=len(sync["index_warnings"]))
    print(json.dumps(display, ensure_ascii=False, indent=2))
    return 0 if summary["status"] in {"completed", "preview"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
