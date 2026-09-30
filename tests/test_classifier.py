from pathlib import Path
import tempfile
import unittest

from zhihu_classifier.db import Database
from zhihu_classifier.metadata import MetadataError, validate_assignment
from zhihu_classifier.pipeline import (
    _normalize_saved_assignment_response,
    _normalize_saved_full_response,
    analyze_pending,
    recheck_candidate_assignments,
    remap_pending,
    repair_failed_analyses,
)
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
    def test_saved_assignment_normalizer_repairs_unique_parent_and_confidence_key(self):
        taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)
        payload = {
            "category": {
                "level1_id": "thinking_models",
                "level1_name": "思维模型与思想方法",
                "level2_name": "制度权力与社会运行",
            },
            "alternatives": [],
            "reason": "讨论制度和权力结构。",
            ",confidence": 0.74,
            "needs_review": True,
            "candidate_new_topics": [],
        }

        normalized = _normalize_saved_assignment_response(payload, taxonomy)
        assignment = validate_assignment(normalized, taxonomy)

        self.assertEqual(assignment.level1_id, "understanding_world")
        self.assertEqual(assignment.level1_name, "认识世界")
        self.assertEqual(assignment.level2_name, "制度权力与社会运行")
        self.assertEqual(assignment.confidence, 0.74)

    def test_saved_response_normalizer_repairs_misplaced_fields(self):
        payload = {
            "analysis": {"summary_short": "短摘要", "summary_detailed": "长摘要"},
            "primary_purpose": "保存家庭记忆",
            "assignment": {"category": {}},
        }

        normalized = _normalize_saved_full_response(payload)

        self.assertEqual(normalized["analysis"]["primary_purpose"], "保存家庭记忆")
        self.assertEqual(normalized["assignment"], {"category": {}})

    def test_explicitly_insufficient_content_can_remain_unclassified_for_review(self):
        taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)
        payload = {
            "category": {"level1_id": "", "level1_name": "", "level2_name": ""},
            "alternatives": [],
            "reason": "正文只有无意义占位内容，无法可靠判断主题。",
            "confidence": 0.0,
            "needs_review": True,
            "candidate_new_topics": [],
        }

        assignment = validate_assignment(payload, taxonomy)

        self.assertEqual(assignment.level1_id, "")
        self.assertTrue(assignment.needs_review)

        payload["confidence"] = 0.6
        with self.assertRaises(MetadataError):
            validate_assignment(payload, taxonomy)

    def test_assignment_validator_unwraps_json_object_envelope(self):
        taxonomy = Taxonomy.load(Path("taxonomy.yaml"), allow_candidates=True)
        payload = {
            "type": "json_object",
            "content": {
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
            },
        }

        assignment = validate_assignment(payload, taxonomy)

        self.assertEqual(assignment.level1_id, "gender_and_intimacy")

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
            progress_events = []

            result = analyze_pending(
                database,
                taxonomy,
                FakeClient(),
                limit=1,
                max_chars=5000,
                progress=progress_events.append,
            )

            self.assertEqual(result["selected"], 1)
            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["errors"], 0)
            self.assertIsInstance(result["run_id"], int)
            self.assertEqual(database.counts()["current_analyses"], 1)
            self.assertEqual(database.counts()["latest_assignments"], 1)
            self.assertEqual(database.counts()["latest_needs_review"], 0)
            self.assertEqual(database.counts()["api_calls_recorded"], 1)
            self.assertEqual(len(progress_events), 1)
            self.assertEqual(progress_events[0]["current"], 1)
            self.assertEqual(progress_events[0]["processed"], 1)
            self.assertEqual(progress_events[0]["errors"], 0)

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
            self.assertEqual(database.counts()["current_analyses"], 0)
            self.assertEqual(database.counts()["pending_full_text_analysis"], 1)
            self.assertEqual(database.counts()["current_analysis_errors"], 1)

    def test_remap_carries_forward_latest_human_assignment_without_api(self):
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
                current = connection.execute(
                    "SELECT * FROM category_assignments ORDER BY id DESC LIMIT 1"
                ).fetchone()
                connection.execute(
                    """INSERT INTO category_assignments
                    (article_id, analysis_id, taxonomy_version, assignment_prompt_version,
                     provider, model, source, level1_id, level1_name, level2_name,
                     alternatives_json, reason, confidence, needs_review,
                     candidate_new_topics_json, created_at)
                    VALUES (?, ?, ?, 'manual', 'human', 'manual', 'human_visual_review',
                            'culture_and_entertainment', '文化娱乐', '美女与视觉欣赏',
                            '[]', '用户确认', 1.0, 0, '[]', 'now')""",
                    (current["article_id"], current["analysis_id"], taxonomy.version),
                )
            newer_taxonomy = Taxonomy(
                version="test-final",
                status="final",
                source_library=taxonomy.source_library,
                classified_library=taxonomy.classified_library,
                categories=taxonomy.categories,
                core_priority=taxonomy.core_priority,
                raw=taxonomy.raw,
            )

            remap = remap_pending(database, newer_taxonomy, FakeClient(), limit=10)

            self.assertEqual(remap["human_carried"], 1)
            self.assertEqual(remap["processed"], 0)
            self.assertEqual(database.counts()["api_calls_recorded"], 1)
            with database.connect() as connection:
                latest = connection.execute(
                    "SELECT * FROM category_assignments ORDER BY id DESC LIMIT 1"
                ).fetchone()
            self.assertEqual(latest["taxonomy_version"], "test-final")
            self.assertEqual(latest["source"], "human_carry_forward")
            self.assertEqual(latest["level2_name"], "美女与视觉欣赏")
            self.assertEqual(database.counts()["api_calls_recorded"], 1)

    def test_failed_retry_does_not_hide_existing_success(self):
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
            with database.connect() as connection:
                article = connection.execute("SELECT * FROM articles").fetchone()
                connection.execute(
                    """
                    INSERT INTO article_analyses (
                        article_id, content_hash, analysis_version, prompt_version,
                        provider, model, error, created_at
                    ) VALUES (?, ?, 'test', 'test', 'deepseek', 'fake-model',
                              'retry failed', '2026-09-12T00:00:00+00:00')
                    """,
                    (article["id"], article["content_hash"]),
                )

            counts = database.counts()
            self.assertEqual(counts["current_analyses"], 1)
            self.assertEqual(counts["pending_full_text_analysis"], 0)
            self.assertEqual(counts["current_analysis_errors"], 0)

    def test_repair_failed_analysis_uses_saved_response(self):
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
                database, taxonomy, InvalidClient(), limit=1, max_chars=5000
            )
            repaired = repair_failed_analyses(
                database, taxonomy, FakeClient(), limit=1
            )

            self.assertEqual(repaired["processed"], 1)
            self.assertEqual(repaired["errors"], 0)
            self.assertEqual(database.counts()["current_analyses"], 1)
            self.assertEqual(database.counts()["pending_full_text_analysis"], 0)

    def test_candidate_recheck_uses_semantic_profile(self):
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
            with database.connect() as connection:
                analysis = connection.execute(
                    "SELECT * FROM article_analyses WHERE error IS NULL"
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO category_assignments (
                        article_id, analysis_id, taxonomy_version,
                        assignment_prompt_version, provider, model, source,
                        level1_id, level1_name, level2_name, alternatives_json,
                        reason, confidence, needs_review,
                        candidate_new_topics_json, created_at
                    ) VALUES (?, ?, ?, 'test', 'deepseek', 'fake-model', 'test',
                              '', '', '', '[]', '未找到目录', 0.4, 1, ?,
                              '2026-09-12T00:00:00+00:00')
                    """,
                    (
                        analysis["article_id"],
                        analysis["id"],
                        taxonomy.version,
                        '[{"suggested_level1":"新目录","suggested_level2":"新小类"}]',
                    ),
                )

            result = recheck_candidate_assignments(
                database, taxonomy, FakeClient(), limit=1
            )
            self.assertEqual(result["processed"], 1)
            with database.connect() as connection:
                latest = connection.execute(
                    "SELECT * FROM category_assignments ORDER BY id DESC LIMIT 1"
                ).fetchone()
            self.assertEqual(latest["source"], "candidate_recheck")
            self.assertEqual(latest["level1_id"], "gender_and_intimacy")
            self.assertEqual(latest["candidate_new_topics_json"], "[]")


if __name__ == "__main__":
    unittest.main()
