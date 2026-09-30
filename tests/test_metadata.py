from dataclasses import replace
from pathlib import Path
import unittest

from zhihu_classifier.metadata import MetadataError, validate_metadata
from zhihu_classifier.taxonomy import Taxonomy


class MetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)

    def test_valid_confirmed_category(self):
        payload = {
            "category": {
                "level1_id": "gender_and_intimacy",
                "level1_name": "两性与亲密关系",
                "level2_name": "女性与婚恋观",
            },
            "tags": ["婚恋观", "女性", "婚恋市场"],
            "summary": "文章讨论女性婚恋观及其形成背景。",
            "purpose": "认识婚恋现实",
            "reason": "主要讨论女性群体的婚恋观",
            "confidence": 0.91,
            "needs_review": False,
            "candidate_new_topic": None,
        }
        result = validate_metadata(payload, self.taxonomy)
        self.assertFalse(result.needs_review)
        self.assertEqual(result.level2_name, "女性与婚恋观")

    def test_low_confidence_forces_review(self):
        payload = {
            "category": {
                "level1_id": "gender_and_intimacy",
                "level1_name": "两性与亲密关系",
                "level2_name": "女性与婚恋观",
            },
            "tags": [],
            "summary": "摘要",
            "purpose": "用途",
            "reason": "理由",
            "confidence": 0.6,
            "needs_review": False,
            "candidate_new_topic": None,
        }
        self.assertTrue(validate_metadata(payload, self.taxonomy).needs_review)

    def test_confirmed_ai_second_level_does_not_force_review(self):
        payload = {
            "category": {
                "level1_id": "artificial_intelligence",
                "level1_name": "人工智能",
                "level2_name": "AI使用方法",
            },
            "tags": ["心理健康"],
            "summary": "摘要",
            "purpose": "用途",
            "reason": "理由",
            "confidence": 0.95,
            "needs_review": False,
            "candidate_new_topic": None,
        }
        self.assertFalse(validate_metadata(payload, self.taxonomy).needs_review)

    def test_future_draft_second_level_forces_review(self):
        categories = tuple(
            replace(category, uses_candidates=True)
            if category.id == "artificial_intelligence"
            else category
            for category in self.taxonomy.categories
        )
        draft_taxonomy = replace(self.taxonomy, categories=categories)
        payload = {
            "category": {
                "level1_id": "artificial_intelligence",
                "level1_name": "人工智能",
                "level2_name": "AI使用方法",
            },
            "tags": ["人工智能"],
            "summary": "摘要",
            "purpose": "用途",
            "reason": "理由",
            "confidence": 0.95,
            "needs_review": False,
            "candidate_new_topic": None,
        }
        self.assertTrue(validate_metadata(payload, draft_taxonomy).needs_review)

    def test_unknown_second_level_rejected(self):
        payload = {
            "category": {
                "level1_id": "gender_and_intimacy",
                "level1_name": "两性与亲密关系",
                "level2_name": "不存在",
            },
            "tags": [],
            "summary": "摘要",
            "purpose": "用途",
            "reason": "理由",
            "confidence": 0.9,
            "needs_review": False,
            "candidate_new_topic": None,
        }
        with self.assertRaises(MetadataError):
            validate_metadata(payload, self.taxonomy)


if __name__ == "__main__":
    unittest.main()
