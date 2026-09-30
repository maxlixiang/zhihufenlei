from pathlib import Path
import yaml
import tempfile
import unittest

from zhihu_classifier.copier import copy_analysis_run
from zhihu_classifier.db import Database
from zhihu_classifier.full_copier import copy_all_current
from zhihu_classifier.pipeline import analyze_pending
from zhihu_classifier.scanner import scan_library
from zhihu_classifier.taxonomy import Taxonomy
from tests.test_classifier import FakeClient


class CopierTests(unittest.TestCase):
    def test_copy_all_uses_current_category_and_manual_tags_for_review_items(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            destination = root / "classified"
            source.mkdir()
            destination.mkdir()
            article = source / "文章.md"
            article.write_text("# 标题\n\n正文", encoding="utf-8")
            attachments = source / "文章"
            attachments.mkdir()
            (attachments / "图片.jpg").write_bytes(b"image")
            original = article.read_bytes()
            taxonomy_base = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)
            taxonomy = Taxonomy(
                version=taxonomy_base.version,
                status=taxonomy_base.status,
                source_library=source,
                classified_library=destination,
                categories=taxonomy_base.categories,
                core_priority=taxonomy_base.core_priority,
                raw=taxonomy_base.raw,
            )
            database = Database(root / "test.db")
            database.initialize()
            scan_library(database, source)
            analyze_pending(database, taxonomy, FakeClient(), limit=1, max_chars=5000)
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT ca.id AS assignment_id, a.id AS article_id, a.content_hash "
                    "FROM category_assignments ca JOIN articles a ON a.id=ca.article_id"
                ).fetchone()
                connection.execute(
                    "UPDATE category_assignments SET confidence=0.8, needs_review=1"
                )
                run_id = connection.execute(
                    """INSERT INTO manual_review_runs
                    (imported_at, source_path, source_hash, source_taxonomy_version,
                     target_taxonomy_version, item_count, accepted_count, override_count, tag_count)
                    VALUES ('now', 'review.md', 'manual-hash', ?, ?, 1, 1, 0, 1)""",
                    (taxonomy.version, taxonomy.version),
                ).lastrowid
                connection.execute(
                    """INSERT INTO manual_reviews
                    (run_id, article_id, content_hash, previous_assignment_id,
                     new_assignment_id, review_result, level1_id, level1_name,
                     level2_name, tags_json, review_note, created_at)
                    VALUES (?, ?, ?, ?, ?, '通过', 'gender_and_intimacy',
                            '两性与亲密关系', '女性与婚恋观',
                            '["收藏意图/测试"]', '', 'now')""",
                    (run_id, row["article_id"], row["content_hash"],
                     row["assignment_id"], row["assignment_id"]),
                )

            manifest = copy_all_current(
                database,
                taxonomy,
                destination_root=destination,
                manifest_path=root / "full-manifest.json",
            )

            copied = destination / "两性与亲密关系" / "女性与婚恋观" / "文章.md"
            copied_image = destination / "两性与亲密关系" / "女性与婚恋观" / "文章" / "图片.jpg"
            frontmatter = yaml.safe_load(copied.read_text(encoding="utf-8").split("---", 2)[1])
            self.assertEqual(manifest["requested"], 1)
            self.assertEqual(manifest["copied"], 1)
            self.assertEqual(manifest["errors"], 0)
            self.assertEqual(frontmatter["status"], "待审核")
            self.assertIn("收藏意图/测试", frontmatter["tags"])
            self.assertEqual(copied_image.read_bytes(), b"image")
            self.assertEqual(article.read_bytes(), original)

    def test_copy_analysis_run_preserves_source_and_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            destination = root / "classified"
            source.mkdir()
            article = source / "文章.md"
            article.write_text("# 标题\n\n正文", encoding="utf-8")
            attachments = source / "文章"
            attachments.mkdir()
            image = attachments / "图片.jpg"
            image.write_bytes(b"fake-image-bytes")
            before = article.read_bytes()
            image_before = image.read_bytes()
            taxonomy_base = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)
            taxonomy = Taxonomy(
                version=taxonomy_base.version,
                status=taxonomy_base.status,
                source_library=source,
                classified_library=destination,
                categories=taxonomy_base.categories,
                core_priority=taxonomy_base.core_priority,
                raw=taxonomy_base.raw,
            )
            database = Database(root / "test.db")
            database.initialize()
            scan_library(database, source)
            analysis = analyze_pending(database, taxonomy, FakeClient(), limit=1, max_chars=5000)
            with database.connect() as connection:
                article_id = connection.execute("SELECT id FROM articles").fetchone()[0]
                visual_run_id = connection.execute(
                    """INSERT INTO visual_review_runs
                    (imported_at, source_path, source_hash, taxonomy_version,
                     item_count, reviewed_count, override_count)
                    VALUES ('now', 'review.md', 'review-hash', ?, 1, 0, 0)""",
                    (taxonomy.version,),
                ).lastrowid
                connection.execute(
                    """INSERT INTO visual_reviews
                    (run_id, article_id, content_hash, taxonomy_version, review_status,
                     category_decision, level1_id, level1_name, level2_name,
                     visual_summary, collection_intent, retention_decision,
                     tags_json, review_note, created_at)
                    SELECT ?, a.id, a.content_hash, ?, '沿用现有分类', 'unfilled_current',
                           'gender_and_intimacy', '两性与亲密关系', '女性与婚恋观',
                           '', '', '待定', '["内容形态/图片为主"]', '', 'now'
                    FROM articles a WHERE a.id=?""",
                    (visual_run_id, taxonomy.version, article_id),
                )
            manifest = copy_analysis_run(
                database,
                taxonomy,
                analysis_run_id=analysis["run_id"],
                destination_root=destination,
                manifest_path=root / "manifest.json",
            )
            copied = destination / "两性与亲密关系" / "女性与婚恋观" / "文章.md"
            copied_image = destination / "两性与亲密关系" / "女性与婚恋观" / "文章" / "图片.jpg"
            self.assertEqual(manifest["copied"], 1)
            self.assertEqual(manifest["attachments_copied"], 1)
            copied_text = copied.read_bytes().decode("utf-8")
            frontmatter_text, copied_body = copied_text.split("---", 2)[1:]
            frontmatter = yaml.safe_load(frontmatter_text)
            self.assertEqual(frontmatter["category"], "两性与亲密关系/女性与婚恋观")
            self.assertEqual(
                frontmatter["tags"],
                ["女性", "婚恋观", "社会观察", "内容形态/图片为主"],
            )
            self.assertEqual(frontmatter["status"], "已分类")
            self.assertEqual(copied_body.strip(), before.decode("utf-8").strip())
            self.assertEqual(copied_image.read_bytes(), image_before)
            self.assertEqual(article.read_bytes(), before)
            self.assertEqual(image.read_bytes(), image_before)

            repeated = copy_analysis_run(
                database,
                taxonomy,
                analysis_run_id=analysis["run_id"],
                destination_root=destination,
                manifest_path=root / "manifest-repeated.json",
            )
            self.assertEqual(repeated["copied"], 0)
            self.assertEqual(repeated["already_exists"], 1)
            self.assertEqual(repeated["attachments_copied"], 0)
            self.assertEqual(repeated["attachments_already_existing"], 1)
            self.assertEqual(repeated["errors"], 0)


if __name__ == "__main__":
    unittest.main()
