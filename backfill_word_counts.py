"""Explicitly authorized word-count maintenance; does not download or call models."""
import argparse
import json
from pathlib import Path
import sys

from zhihu_classifier.taxonomy import Taxonomy
from zhihu_classifier.word_count_backfill import backfill_word_counts


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(description="只补全Obsidian笔记正文字数，自动备份；不下载、不分类")
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run",action="store_true",help="只读检查，不创建备份或报告")
    mode.add_argument("--confirm-update",action="store_true",help="授权备份并补全现有分类笔记的正文字数")
    args=parser.parse_args(argv)
    project=Path(__file__).resolve().parent
    taxonomy=Taxonomy.load(project / "taxonomy.yaml")
    result=backfill_word_counts(taxonomy.classified_library,apply=args.confirm_update,report_root=project/"reports/word_counts")
    print(json.dumps({key:value for key,value in result.items() if key!="items"},ensure_ascii=False,indent=2))
    return 2 if result["errors"] else 0


if __name__=="__main__":
    raise SystemExit(main())
