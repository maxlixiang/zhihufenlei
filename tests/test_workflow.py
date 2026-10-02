from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import os
import sys
from contextlib import redirect_stdout
import tempfile
import threading
import unittest
from unittest.mock import patch

import yaml

from tests.test_classifier import FakeClient
from workflow_runner import (WorkflowConfig, run_workflow, sync_archives, publish_file,
                             digest, final_json, workflow_lock, collector_runs, run_command, ConsoleProgress)
from zhihu_classifier.db import Database
from zhihu_classifier.incremental import update_library
from zhihu_classifier.taxonomy import Taxonomy


class Client(FakeClient):
    def __init__(self):
        self.calls = 0

    def complete_json(self, **kwargs):
        self.calls += 1
        return super().complete_json(**kwargs)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        # 本次测试保留临时目录，不递归删除任何文件。
        self.root = Path(tempfile.mkdtemp(prefix="zhihu-workflow-"))
        self.collector = self.root / "collector"
        self.classifier = self.root / "classifier"
        self.download = self.collector / "data/articles"
        self.source = self.root / "original"
        self.destination = self.root / "obsidian"
        for directory in (self.collector, self.classifier, self.download, self.source, self.destination):
            directory.mkdir(parents=True, exist_ok=True)
        (self.collector / "zhihu_scraper.py").write_text("# placeholder", encoding="utf-8")
        (self.classifier / "zhihu_classifier").mkdir()
        (self.classifier / "zhihu_classifier/__main__.py").write_text("# placeholder", encoding="utf-8")
        (self.collector / "state.json").write_text("{}", encoding="utf-8")
        raw = Taxonomy.load(Path("taxonomy.yaml")).raw
        raw = {**raw, "paths": {"source_library": {"path": str(self.source)},
                                "classified_library": {"path": str(self.destination)}}}
        (self.classifier / "taxonomy.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        self.database = Database(self.classifier / "classification.db")
        self.database.initialize()
        self.client = Client()
        self.config = WorkflowConfig(self.collector, self.classifier, self.download, self.source,
                                     self.destination, self.collector / "archive.db", self.collector / "state.json",
                                     self.classifier / "reports/workflow")
        with sqlite3.connect(self.config.collector_database) as connection:
            connection.executescript("""
                CREATE TABLE archive_items(content_key TEXT PRIMARY KEY, markdown_path TEXT,
                    image_dir TEXT, status TEXT,source_method TEXT,scraped_at TEXT);
                CREATE TABLE archive_runs(run_id TEXT PRIMARY KEY,status TEXT,new_count INTEGER,source_method TEXT);
            """)
        self.round = 0
        self.new_count = 0
        self.complete = True

    def add(self, number, *, body=None, image=False):
        folder = self.download / "2026/10"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"文章{number}.md"
        metadata = {"title": f"文章{number}", "source_url": f"https://www.zhihu.com/question/1/answer/{number}",
                    "source_type": "answer", "zhihu_answer_id": str(number)}
        body = body if body is not None else f"独特正文 {number}"
        if image:
            images = path.with_suffix("")
            images.mkdir(exist_ok=True)
            (images / "图片.jpg").write_bytes(b"image")
            body += f"\n![图](文章{number}/图片.jpg)"
        path.write_text("---\n" + yaml.safe_dump(metadata, allow_unicode=True) + "---\n\n" + body,
                        encoding="utf-8")
        with sqlite3.connect(self.config.collector_database) as connection:
            connection.execute("INSERT INTO archive_items VALUES (?,?,?,'success','playwright','now')",
                               (f"answer:{number}", str(path), str(path.with_suffix(""))))
        return path

    def runner(self, command, cwd, env, log):
        self.assertNotIn("shell=True", str(command))
        if "zhihu_scraper.py" in command:
            self.round += 1
            with sqlite3.connect(self.config.collector_database) as connection:
                count = connection.execute("SELECT COUNT(*) FROM archive_items").fetchone()[0]
                connection.execute("INSERT INTO archive_runs VALUES (?,?,?,'playwright')",
                                   (str(self.round), "complete" if self.complete else "incomplete", self.new_count))
            for number in range(count + 1, count + self.new_count + 1):
                self.add(number, image=number == 1)
            return {"returncode": 0, "stdout": "模拟下载"}
        self.assertEqual(env["SOURCE_LIBRARY"], str(self.source))
        self.assertEqual(cwd, self.classifier)
        taxonomy = Taxonomy.load(self.classifier / "taxonomy.yaml")
        manifest = Path(command[command.index("--manifest") + 1])
        result = update_library(self.database, taxonomy, lambda: self.client, manifest_path=manifest)
        failed = result["copy"]["errors"] or result["copy"]["pending"] or result["copy"]["conflicts"]
        return {"returncode": 2 if failed else 0, "stdout": "模拟进度\n" + json.dumps(result, ensure_ascii=False)}

    def run_it(self, **kwargs):
        return run_workflow(self.config, confirm=True, runner=self.runner, **kwargs)

    def test_download_ten_then_fifty_then_fifty_full_flow(self):
        for count in (10, 50, 50):
            self.new_count = count
            result = self.run_it()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["sync"]["copied"], count)
            self.assertEqual(result["classification"]["summary"]["analysis"]["processed"], count)
            self.assertEqual(result["classification"]["summary"]["copy"]["copied"], count)
            self.assertTrue(Path(result["report"]).is_file())
        self.assertEqual(self.client.calls, 110)
        self.assertEqual(len(list(self.source.rglob("*.md"))), 110)
        self.assertEqual(len(list(self.destination.rglob("*.md"))), 110)
        self.assertEqual(next(self.source.rglob("图片.jpg")).read_bytes(), b"image")
        self.assertEqual(next(self.destination.rglob("图片.jpg")).read_bytes(), b"image")
        repeat = self.run_it(skip_download=True)
        self.assertEqual(repeat["sync"]["copied"], 0)
        self.assertEqual(self.client.calls, 110)

    def test_dry_run_no_commands_files_or_database_changes(self):
        self.add(1)
        before = digest(self.config.collector_database)
        def forbidden(*args):
            raise AssertionError("预览不应调用程序")
        result = run_workflow(self.config, dry_run=True, runner=forbidden)
        self.assertEqual(result["sync"]["would_copy"], 1)
        self.assertEqual(list(self.source.rglob("*")), [])
        self.assertFalse(self.config.report_root.exists())
        self.assertEqual(before, digest(self.config.collector_database))

    def test_zero_exit_with_incomplete_download_is_not_success(self):
        self.complete = False
        self.new_count = 1
        result = self.run_it()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["download"]["status"], "incomplete")
        self.assertEqual(result["sync"]["copied"], 1)
        self.assertEqual(result["classification"]["summary"]["copy"]["copied"], 1)

    def test_failed_placeholder_and_missing_local_image_are_quarantined(self):
        self.add(1, body="【正文提取失败】")
        self.add(2, body="![图](文章2/不存在.jpg)")
        self.add(3)
        result = self.run_it(skip_download=True)
        self.assertEqual(len(result["sync"]["errors"]), 2)
        self.assertEqual(result["sync"]["copied"], 1)
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(result["status"], "partial")

    def test_existing_original_and_edited_obsidian_are_preserved(self):
        self.add(1)
        self.run_it(skip_download=True)
        note = next(self.destination.rglob("*.md"))
        note.write_text(note.read_text(encoding="utf-8") + "\n我的笔记", encoding="utf-8")
        before = note.read_bytes()
        original = next(self.source.rglob("*.md"))
        original_before = original.read_bytes()
        self.run_it(skip_download=True)
        self.assertEqual(note.read_bytes(), before)
        self.assertEqual(original.read_bytes(), original_before)
        downloaded = next(self.download.rglob("*.md"))
        downloaded.write_text(downloaded.read_text(encoding="utf-8") + "\n新的原文版本", encoding="utf-8")
        conflict = self.run_it(skip_download=True)
        self.assertEqual(len(conflict["sync"]["errors"]), 1)
        self.assertEqual(original.read_bytes(), original_before)
        self.assertEqual(self.client.calls, 1)

    def test_attachment_failure_publishes_no_markdown_and_retry_completes(self):
        self.add(1, image=True)
        with patch("workflow_runner.shutil.copyfileobj", side_effect=OSError("模拟复制失败")):
            failed = sync_archives(self.config)
        self.assertEqual(len(failed["errors"]), 1)
        self.assertEqual(list(self.source.rglob("*.md")), [])
        retried = sync_archives(self.config)
        self.assertEqual(retried["copied"], 1)
        self.assertEqual(len(retried["errors"]), 0)

    def test_classification_failure_can_resume_without_download(self):
        self.new_count = 1
        def fail_classifier(command, cwd, env, log):
            if "zhihu_scraper.py" in command:
                return self.runner(command, cwd, env, log)
            return {"returncode": 2, "stdout": "模型服务失败"}
        failed = run_workflow(self.config, confirm=True, runner=fail_classifier)
        self.assertEqual(failed["status"], "partial")
        retried = self.run_it(skip_download=True)
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(self.round, 1)
        self.assertEqual(self.client.calls, 1)

    def test_path_escape_and_missing_confirmation(self):
        outside = self.root / "outside.md"
        outside.write_text("不能导入", encoding="utf-8")
        with sqlite3.connect(self.config.collector_database) as connection:
            connection.execute("INSERT INTO archive_items VALUES ('answer:1',?,'','success','playwright','now')",
                               (str(outside),))
        result = sync_archives(self.config)
        self.assertEqual(len(result["errors"]), 1)
        with self.assertRaises(ValueError):
            run_workflow(self.config)
        with self.assertRaises(ValueError):
            replace(self.config, download_root=self.source / "nested").validate()

    def test_legacy_headerless_and_already_archived_inconsistent_metadata_only_warn(self):
        legacy = self.source / "旧文章.md"
        legacy.write_text("没有表头的旧正文", encoding="utf-8")
        download = self.add(1)
        text = download.read_text(encoding="utf-8").replace("/answer/1", "/pin/999")
        download.write_text(text, encoding="utf-8")
        target = self.source / download.relative_to(self.download)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(download.read_bytes())
        self.add(2)
        result = sync_archives(self.config)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["copied"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(result["index_warnings"]), 1)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(legacy.read_text(encoding="utf-8"), "没有表头的旧正文")

    def test_remote_images_warn_without_inventing_local_images(self):
        self.add(1, body="文字正文 ![远程](https://example.invalid/image.jpg)")
        result = sync_archives(self.config)
        self.assertEqual(result["copied"], 1)
        self.assertEqual(result["warnings"][0]["remote_images"], 1)

    def test_reading_old_database_does_not_migrate_it(self):
        old = self.root / "old.db"
        with sqlite3.connect(old) as connection:
            connection.execute("CREATE TABLE articles(title TEXT)")
        before = digest(old)
        self.assertEqual(collector_runs(replace(self.config, collector_database=old)), {})
        self.assertEqual(digest(old), before)

    def test_subprocess_logging_preserves_utf8_and_structured_output(self):
        env={**os.environ,"PYTHONUTF8":"1","PYTHONIOENCODING":"utf-8"}
        log=self.root / "child.log"
        code="print('中文进度'); print('{\"完成\": true}')"
        with redirect_stdout(io.StringIO()):
            result=run_command([sys.executable,"-u","-B","-c",code],self.root,env,log)
        self.assertEqual(result["returncode"],0)
        self.assertIn("中文进度",log.read_text(encoding="utf-8"))
        self.assertEqual(final_json(result["stdout"]),{"完成":True})

    def test_console_phases_and_backup_summary_precede_classification(self):
        self.new_count = 1
        output = io.StringIO()
        with redirect_stdout(output):
            result = self.run_it()
        text = output.getvalue()
        labels = ["[阶段 1/3] 依次下载", "[阶段 2/3] 追加原始备份",
                  "正在扫描已有文章", "1/1 已追加", "核对完成：新增 1 篇",
                  "[阶段 3/3] 分类并生成Obsidian副本"]
        positions = [text.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(result["status"], "completed")
        with redirect_stdout(io.StringIO()) as repeated:
            self.run_it(skip_download=True)
        self.assertIn("依次下载：本次跳过", repeated.getvalue())
        self.assertIn("已有备份跳过 1 篇", repeated.getvalue())

    def test_backup_progress_covers_scan_attachments_and_errors(self):
        self.add(1, image=True)
        self.add(2, body="【正文提取失败】")
        messages = []
        result = sync_archives(self.config, progress=lambda message, **kwargs: messages.append(message))
        text = "\n".join(messages)
        self.assertIn("共 2 篇下载文章待核对", text)
        self.assertIn("追加附件 1/1", text)
        self.assertIn("正在追加正文", text)
        self.assertIn("2/2 异常，未完成", text)
        self.assertIn("异常 1 篇", text)
        self.assertEqual(result["copied"], 1)

    def test_silent_heartbeat_stops_on_exception(self):
        emitted = threading.Event()
        class Output(io.StringIO):
            def write(self, value):
                if "正在核对附件" in value:
                    emitted.set()
                return super().write(value)
        output = Output()
        progress = ConsoleProgress(interval=0.01)
        with redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "模拟中断"):
                with progress:
                    progress.update("正在核对附件")
                    self.assertTrue(emitted.wait(timeout=2), "静默期间应显示当前操作")
                    raise RuntimeError("模拟中断")
        self.assertFalse(progress.thread.is_alive())
        self.assertIn("本阶段已用时", output.getvalue())

    def test_lock_and_structured_summary(self):
        lock = self.root / "workflow.lock"
        with workflow_lock(lock):
            with self.assertRaises((OSError, RuntimeError)):
                with workflow_lock(lock):
                    pass
        self.assertEqual(final_json('日志\n{"nested":{"value":1}}\n'), {"nested": {"value": 1}})
        self.assertIsNone(final_json("错误而没有摘要"))


if __name__ == "__main__":
    unittest.main()
