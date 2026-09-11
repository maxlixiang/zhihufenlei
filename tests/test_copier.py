from pathlib import Path
import yaml
import tempfile
import unittest

from zhihu_classifier.copier import copy_analysis_run
from zhihu_classifier.db import Database
from zhihu_classifier.pipeline import analyze_pending
from zhihu_classifier.scanner import scan_library
from zhihu_classifier.taxonomy import Taxonomy
from tests.test_classifier import FakeClient


class CopierTests(unittest.TestCase):
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
            self.assertEqual(frontmatter["tags"], ["女性", "婚恋观", "社会观察"])
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
