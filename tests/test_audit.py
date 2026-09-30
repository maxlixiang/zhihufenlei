from pathlib import Path
import tempfile
import unittest

from zhihu_classifier.audit import generate_quality_report
from zhihu_classifier.db import Database
from zhihu_classifier.pipeline import analyze_pending
from zhihu_classifier.scanner import scan_library
from zhihu_classifier.taxonomy import Taxonomy

from test_classifier import FakeClient


class AuditTests(unittest.TestCase):
    def test_generate_quality_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "文章.md").write_text("# 标题\n\n正文", encoding="utf-8")
            database = Database(root / "test.db")
            database.initialize()
            scan_library(database, source)
            taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)
            analyze_pending(
                database, taxonomy, FakeClient(), limit=1, max_chars=5000
            )
            output = root / "quality.md"

            result = generate_quality_report(
                database, taxonomy, output, sample_per_band=1
            )

            text = output.read_text(encoding="utf-8")
            self.assertEqual(result["assignments"], 1)
            self.assertIn("# 知乎文章分类质量报告", text)
            self.assertIn("两性与亲密关系", text)
            self.assertIn("本报告不授权或执行任何文章复制", text)


if __name__ == "__main__":
    unittest.main()
