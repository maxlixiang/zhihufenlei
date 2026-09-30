from pathlib import Path
import tempfile
import unittest

from zhihu_classifier.db import Database
from zhihu_classifier.exporter import export_latest
from zhihu_classifier.pipeline import analyze_pending
from zhihu_classifier.review import export_low_confidence_review, import_low_confidence_review
from zhihu_classifier.scanner import scan_library
from zhihu_classifier.taxonomy import Taxonomy
from zhihu_classifier.visual_review import export_visual_review, import_visual_review

from test_classifier import FakeClient


class ReviewExportTests(unittest.TestCase):
    def test_imports_completed_review_with_local_carry_forward_and_tags(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            article = source / "文章.md"
            article.write_text("# 标题\n\n正文", encoding="utf-8")
            database = Database(root / "test.db")
            database.initialize()
            scan_library(database, source)
            taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)
            analyze_pending(database, taxonomy, FakeClient(), limit=1, max_chars=5000)
            with database.connect() as connection:
                connection.execute(
                    "UPDATE category_assignments SET taxonomy_version='1.0', confidence=0.5, needs_review=1"
                )
            review = root / "review.md"
            export_low_confidence_review(database, taxonomy, review, below=0.75)
            review.write_text(
                review.read_text(encoding="utf-8")
                .replace("review_result: 待填写", "review_result: 改类")
                .replace("review_level1:", "review_level1: 健康")
                .replace("review_level2:", "review_level2: 个人安全防护")
                .replace("review_note:", "review_tags: 收藏意图/安全防护\nreview_note:"),
                encoding="utf-8",
            )
            result_path = root / "applied.md"

            result = import_low_confidence_review(database, taxonomy, review, result_path)

            self.assertEqual(result["carried"], 1)
            self.assertEqual(result["overrides"], 1)
            with database.connect() as connection:
                assignment = connection.execute(
                    "SELECT * FROM category_assignments ORDER BY id DESC LIMIT 1"
                ).fetchone()
            self.assertEqual(assignment["taxonomy_version"], "1.1")
            self.assertEqual(assignment["source"], "human_low_confidence_review")
            self.assertEqual(assignment["level2_name"], "个人安全防护")
            exported = root / "export.jsonl"
            export_latest(database, exported)
            exported_item = __import__("json").loads(exported.read_text(encoding="utf-8"))
            self.assertIn("收藏意图/安全防护", exported_item["tags"])
            with self.assertRaises(ValueError):
                import_low_confidence_review(database, taxonomy, review, root / "again.md")

    def test_exports_fillable_low_confidence_markdown_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "文章.md").write_text("# 标题\n\n正文", encoding="utf-8")
            database = Database(root / "test.db")
            database.initialize()
            scan_library(database, source)
            taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)
            analyze_pending(database, taxonomy, FakeClient(), limit=1, max_chars=5000)
            with database.connect() as connection:
                connection.execute(
                    "UPDATE category_assignments SET confidence=0.5, needs_review=1"
                )
            output = root / "review.md"

            count = export_low_confidence_review(
                database,
                taxonomy,
                output,
                below=0.75,
            )

            text = output.read_text(encoding="utf-8")
            self.assertEqual(count, 1)
            self.assertIn("review_result: 待填写", text)
            self.assertIn("review_level1:", text)
            self.assertIn("女性与婚恋观", text)
            with self.assertRaises(FileExistsError):
                export_low_confidence_review(
                    database,
                    taxonomy,
                    output,
                    below=0.75,
                )

    def test_exports_visual_candidates_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            article = source / "图片文章.md"
            original = "# 图片文章\n\n一句话\n![](图片文章_图片/1.jpg)\n"
            article.write_text(original, encoding="utf-8")
            attachment = source / "图片文章_图片"
            attachment.mkdir()
            (attachment / "1.jpg").write_bytes(b"image")
            (source / "长文章.md").write_text(
                "# 长文章\n\n" + "有价值的文字" * 100 + "\n![](长文章_图片/1.jpg)",
                encoding="utf-8",
            )
            database = Database(root / "test.db")
            database.initialize()
            scan_library(database, source)
            taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)
            output = root / "visual-review.md"

            count = export_visual_review(
                database,
                taxonomy,
                output,
                max_text_chars=20,
            )

            text = output.read_text(encoding="utf-8")
            self.assertEqual(count, 1)
            self.assertIn("visual_review_status: 仍待识别", text)
            self.assertIn("retention_decision: 待定", text)
            self.assertIn("attachment_images: 1", text)
            self.assertIn("text_chars: 3", text)
            self.assertEqual(article.read_text(encoding="utf-8"), original)
            with self.assertRaises(FileExistsError):
                export_visual_review(database, taxonomy, output, max_text_chars=20)

    def test_imports_visual_override_and_image_dominant_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            article = source / "图片文章.md"
            article.write_text("# 图片文章\n\n![](图片文章/1.jpg)\n", encoding="utf-8")
            attachment = source / "图片文章"
            attachment.mkdir()
            (attachment / "1.jpg").write_bytes(b"image")
            database = Database(root / "test.db")
            database.initialize()
            scan_library(database, source)
            taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)
            analyze_pending(database, taxonomy, FakeClient(), limit=1, max_chars=5000)
            review = root / "visual-review.md"
            export_visual_review(database, taxonomy, review, max_text_chars=20)
            review.write_text(
                review.read_text(encoding="utf-8")
                .replace("visual_review_status: 仍待识别", "visual_review_status: 已识别")
                .replace("review_level1:", "review_level1: 文化娱乐")
                .replace("review_level2:", "review_level2: 美女与视觉欣赏")
                .replace("visual_summary:", "visual_summary: 女性人像视觉内容")
                .replace("collection_intent:", "collection_intent: 视觉欣赏"),
                encoding="utf-8",
            )
            result_path = root / "applied.md"

            result = import_visual_review(database, taxonomy, review, result_path)

            self.assertEqual(result["overrides"], 1)
            with database.connect() as connection:
                assignment = connection.execute(
                    "SELECT * FROM category_assignments ORDER BY id DESC LIMIT 1"
                ).fetchone()
                visual = connection.execute(
                    "SELECT * FROM visual_reviews ORDER BY id DESC LIMIT 1"
                ).fetchone()
            self.assertEqual(assignment["source"], "human_visual_review")
            self.assertEqual(assignment["level2_name"], "美女与视觉欣赏")
            self.assertEqual(visual["tags_json"], '["内容形态/图片为主"]')
            self.assertIn("明确改类：1", result_path.read_text(encoding="utf-8"))
            exported = root / "export.jsonl"
            export_latest(database, exported)
            exported_item = __import__("json").loads(exported.read_text(encoding="utf-8"))
            self.assertIn("内容形态/图片为主", exported_item["tags"])


if __name__ == "__main__":
    unittest.main()
