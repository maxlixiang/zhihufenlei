from pathlib import Path
import json
import unittest

from zhihu_classifier.taxonomy import Taxonomy


class TaxonomyTests(unittest.TestCase):
    def test_confirmed_boundaries_are_included_in_model_prompt(self):
        taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)

        self.assertEqual(taxonomy.version, "1.1")
        self.assertEqual(taxonomy.status, "final")
        taxonomy.ensure_classifiable(allow_draft=False)
        self.assertEqual(len(taxonomy.categories), 19)
        self.assertEqual(
            len(taxonomy.category_by_id("investing").second_level),
            8,
        )
        self.assertEqual(
            len(taxonomy.category_by_id("artificial_intelligence").second_level),
            7,
        )
        self.assertEqual(
            len(taxonomy.category_by_id("thinking_models").second_level),
            8,
        )
        self.assertEqual(
            len(taxonomy.category_by_id("history_and_civilization").second_level),
            8,
        )
        self.assertEqual(
            len(taxonomy.category_by_id("culture_and_entertainment").second_level),
            9,
        )
        self.assertFalse(taxonomy.category_by_id("investing").uses_candidates)
        self.assertFalse(
            taxonomy.category_by_id("artificial_intelligence").uses_candidates
        )
        self.assertIn(
            "法律规则、案件责任或办案方法为主时归本类别",
            taxonomy.prompt_json(),
        )
        self.assertIn(
            "编程技术本身归计算机与数字技术",
            taxonomy.prompt_json(),
        )
        prompt_payload = json.loads(taxonomy.prompt_json())
        category_choices = {
            value
            for category in prompt_payload["categories"]
            for value in [category["name"], *category["second_level"]]
        }
        self.assertNotIn("图片为主待识别", category_choices)
        review_bucket = taxonomy.review_bucket_by_id(
            "image_dominant_pending_identification"
        )
        self.assertEqual(review_bucket.name, "图片为主待识别")
        self.assertEqual(review_bucket.path, "_待审核/图片为主待识别")


if __name__ == "__main__":
    unittest.main()
