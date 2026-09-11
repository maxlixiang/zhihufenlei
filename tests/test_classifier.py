from pathlib import Path
import tempfile
import unittest

from zhihu_classifier.db import Database
from zhihu_classifier.pipeline import analyze_pending, remap_pending
from zhihu_classifier.scanner import scan_library
from zhihu_classifier.taxonomy import Taxonomy


class FakeClient:
    model = "fake-model"

    def envelope(self, payload):
        return {
            "model": self.model,
            "content": payload,
            "usage": {
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 40,
                "prompt_cache_miss_tokens": 60,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        }

    def complete_json(self, *, system_prompt: str, user_prompt: str):
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        assignment = {
            "category": {
                "level1_id": "gender_and_intimacy",
                "level1_name": "两性与亲密关系",
                "level2_name": "女性与婚恋观",
            },
            "alternatives": [],
            "reason": "核心内容是女性群体的婚恋观",
            "confidence": 0.92,
            "needs_review": False,
            "candidate_new_topics": [],
        }
        if "文章分析器" not in system_prompt:
            return assignment, self.envelope(assignment)
        payload = {
            "analysis": {
                "summary_short": "文章讨论女性婚恋观。",
                "summary_detailed": "文章结合现实案例讨论女性婚恋观、择偶行为及形成背景，可用于理解婚恋市场。",
                "primary_purpose": "认识当前婚恋现实",
                "main_topics": ["女性婚恋观", "择偶"],
                "tags": ["女性", "婚恋观", "社会观察"],
                "key_points": ["婚恋观受现实条件影响", "择偶行为存在群体差异"],
                "entities": [],
                "content_type": "观点分析",
                "scope": "亲密关系",
                "actionability": "认知理解",
                "time_sensitivity": "中",
                "candidate_new_topics": [],
            },
            "assignment": assignment,
        }
        return payload, self.envelope(payload)


class InvalidClient(FakeClient):
    def complete_json(self, *, system_prompt: str, user_prompt: str):
        payload, envelope = super().complete_json(
            system_prompt=system_prompt, user_prompt=user_prompt
        )
        payload["analysis"]["entities"] = [f"实体{i}" for i in range(21)]
        envelope["content"] = payload
        return payload, envelope


class ClassifierTests(unittest.TestCase):
    def test_end_to_end_with_fake_client(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "文章.md").write_text("# 标题\n\n正文", encoding="utf-8")
            database = Database(root / "test.db")
            database.initialize()
            scan_library(database, source)
            taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)

            result = analyze_pending(
                database, taxonomy, FakeClient(), limit=1, max_chars=5000
            )

            self.assertEqual(result["selected"], 1)
            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["errors"], 0)
            self.assertIsInstance(result["run_id"], int)
            self.assertEqual(database.counts()["current_analyses"], 1)
            self.assertEqual(database.counts()["latest_assignments"], 1)
            self.assertEqual(database.counts()["latest_needs_review"], 0)
            self.assertEqual(database.counts()["api_calls_recorded"], 1)

            newer_taxonomy = Taxonomy(
                version="test-new-version",
                status=taxonomy.status,
                source_library=taxonomy.source_library,
                classified_library=taxonomy.classified_library,
                categories=taxonomy.categories,
                core_priority=taxonomy.core_priority,
                raw=taxonomy.raw,
            )
            remap = remap_pending(database, newer_taxonomy, FakeClient(), limit=1)
            self.assertEqual(remap["processed"], 1)
            self.assertEqual(database.counts()["current_analyses"], 1)
            self.assertEqual(database.counts()["api_calls_recorded"], 2)

    def test_failed_structured_response_still_records_api_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "文章.md").write_text("# 标题\n\n正文", encoding="utf-8")
            database = Database(root / "test.db")
            database.initialize()
            scan_library(database, source)
            taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)

            result = analyze_pending(
                database, taxonomy, InvalidClient(), limit=1, max_chars=5000
            )

            self.assertEqual(result["processed"], 0)
            self.assertEqual(result["errors"], 1)
            self.assertEqual(database.counts()["api_calls_recorded"], 1)


if __name__ == "__main__":
    unittest.main()
