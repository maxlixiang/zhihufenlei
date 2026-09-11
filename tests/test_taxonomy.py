from pathlib import Path
import unittest

from zhihu_classifier.taxonomy import Taxonomy


class TaxonomyTests(unittest.TestCase):
    def test_confirmed_boundaries_are_included_in_model_prompt(self):
        taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)

        self.assertEqual(taxonomy.version, "0.5-draft")
        self.assertEqual(len(taxonomy.categories), 19)
        self.assertIn(
            "法律规则、案件责任或办案方法为主时归本类别",
            taxonomy.prompt_json(),
        )


if __name__ == "__main__":
    unittest.main()
