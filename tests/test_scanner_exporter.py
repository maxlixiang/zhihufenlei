import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from zhihu_classifier.db import Database
from zhihu_classifier.exporter import export_latest
from zhihu_classifier.scanner import scan_library


class ScannerExporterTests(unittest.TestCase):
    def test_scan_is_incremental_and_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            article = source / "[2026-01-01_00-00] 中文标题.md"
            article.write_text("# 中文标题\n\n正文", encoding="utf-8")
            before = article.read_bytes()
            database = Database(root / "data" / "test.db")
            database.initialize()

            first = scan_library(database, source)
            second = scan_library(database, source)

            self.assertEqual(first.discovered, 1)
            self.assertEqual(second.unchanged, 1)
            self.assertEqual(article.read_bytes(), before)
            counts = database.counts()
            self.assertEqual(counts["articles"], 1)
            self.assertEqual(counts["current_analyses"], 0)

    def test_export_latest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            database = Database(root / "test.db")
            database.initialize()
            with database.connect() as connection:
                article_id = connection.execute(
                    """INSERT INTO articles
                    (source_path, relative_path, title, content_hash, size_bytes, mtime_ns,
                     discovered_at, updated_at)
                    VALUES ('x.md', 'x.md', '标题', 'hash', 1, 1, 'now', 'now')"""
                ).lastrowid
                analysis_id = connection.execute(
                    """INSERT INTO article_analyses
                    (article_id, content_hash, analysis_version, prompt_version, provider,
                     model, summary_short, summary_detailed, primary_purpose,
                     main_topics_json, tags_json, key_points_json, entities_json,
                     content_type, scope, actionability, time_sensitivity,
                     candidate_new_topics_json, created_at)
                    VALUES (?, 'hash', '2', '2', 'deepseek', 'model', '短摘要', '详细摘要',
                     '用途', '[\"主题\"]', '[\"标签\"]', '[\"要点\"]', '[]', '观点分析',
                     '个人', '认知理解', '低', '[]', 'now')""",
                    (article_id,),
                ).lastrowid
                connection.execute(
                    """INSERT INTO category_assignments
                    (article_id, analysis_id, taxonomy_version, assignment_prompt_version,
                     provider, model, source, level1_id, level1_name, level2_name,
                     alternatives_json, reason, confidence, needs_review,
                     candidate_new_topics_json, created_at)
                    VALUES (?, ?, '0.3-draft', '2', 'deepseek', 'model', 'initial_analysis',
                     'health', '健康', '心理健康', '[]', '理由', 0.9, 0, '[]', 'now')""",
                    (article_id, analysis_id),
                )
            output = root / "out.jsonl"
            self.assertEqual(export_latest(database, output), 1)
            record = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(record["tags"], ["标签"])
            self.assertEqual(record["main_topics"], ["主题"])


if __name__ == "__main__":
    unittest.main()
