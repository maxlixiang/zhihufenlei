from dataclasses import replace
from contextlib import redirect_stdout
from types import SimpleNamespace
import io
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import yaml

from tests.test_classifier import FakeClient
from zhihu_classifier.cli import build_parser, main
from zhihu_classifier.db import Database
from zhihu_classifier.full_copier import copy_all_current, copy_incremental
from zhihu_classifier.incremental import update_library
from zhihu_classifier.pipeline import analyze_pending
from zhihu_classifier.scanner import scan_library
from zhihu_classifier.taxonomy import Taxonomy


class CountingClient(FakeClient):
    def __init__(self):
        self.calls = 0

    def complete_json(self, **kwargs):
        self.calls += 1
        if "失败标记" in kwargs["user_prompt"]:
            raise RuntimeError("模拟API失败")
        return super().complete_json(**kwargs)


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        # 保留测试目录；不使用递归删除清理。
        self.root = Path(tempfile.mkdtemp(prefix="zhihu-incremental-"))
        self.source = self.root / "source"
        self.destination = self.root / "classified"
        self.source.mkdir()
        self.destination.mkdir()
        self.taxonomy = replace(Taxonomy.load(Path("taxonomy.yaml")),
                                source_library=self.source, classified_library=self.destination)
        self.database = Database(self.root / "test.db")
        self.database.initialize()
        self.client = CountingClient()
        self.run_number = 0

    def update(self):
        self.run_number += 1
        return update_library(self.database, self.taxonomy, lambda: self.client,
                              manifest_path=self.root / f"manifest-{self.run_number}.json")

    def add(self, number):
        p = self.source / f"文章{number}.md"
        p.write_text(f"# 文章{number}\n\n独特正文 {number}", encoding="utf-8")
        return p

    def test_ten_then_fifty_then_fifty_and_repeat_preserves_old_notes(self):
        (self.destination / ".obsidian").mkdir()
        settings = self.destination / ".obsidian" / "app.json"
        settings.write_text("{}", encoding="utf-8")
        for n in range(10):
            self.add(n)
        first = self.update()
        self.assertEqual(first["copy"]["copied"], 10)
        old = next(self.destination.rglob("文章0.md"))
        old.write_text(old.read_text(encoding="utf-8") + "\n我的笔记", encoding="utf-8")
        edited = old.read_bytes()
        for n in range(10, 60):
            self.add(n)
        second = self.update()
        self.assertEqual(second["analysis"]["processed"], 50)
        self.assertEqual(second["copy"]["copied"], 50)
        for n in range(60, 110):
            self.add(n)
        third = self.update()
        self.assertEqual(third["analysis"]["processed"], 50)
        self.assertEqual(third["copy"]["copied"], 50)
        fourth = self.update()
        self.assertEqual(fourth["copy"]["copied"], 0)
        self.assertEqual(fourth["copy"]["skipped"], 110)
        self.assertEqual(self.client.calls, 110)
        self.assertEqual(old.read_bytes(), edited)
        self.assertEqual(settings.read_text(encoding="utf-8"), "{}")
        self.assertEqual(len(list(self.destination.rglob("*.md"))), 110)

    def test_duplicate_content_cross_path_and_same_batch_calls_model_once(self):
        first = self.add(1)
        (self.source / "重复.md").write_bytes(first.read_bytes())
        result = self.update()
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(result["reused"], 1)
        self.assertEqual(result["copy"]["copied"], 1)
        (self.source / "又重复.md").write_bytes(first.read_bytes())
        repeated = self.update()
        self.assertEqual(repeated["reused"], 1)
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(repeated["copy"]["copied"], 0)

    def test_retry_failed_attachment_without_reanalysis(self):
        article = self.add(1)
        attachments = article.with_suffix("")
        attachments.mkdir()
        (attachments / "图片.jpg").write_bytes(b"image")
        with patch("zhihu_classifier.full_copier.shutil.copyfileobj", side_effect=OSError("模拟附件失败")):
            failed = self.update()
        self.assertEqual(failed["copy"]["attachment_errors"], 1)
        self.assertEqual(list(self.destination.rglob("图片.jpg")), [])
        self.assertEqual(list(self.destination.rglob(".zhihu-copy-*.tmp")), [])
        retried = self.update()
        self.assertEqual(retried["copy"]["attachments_copied"], 1)
        self.assertEqual(retried["copy"]["errors"], 0)
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(next(self.destination.rglob("图片.jpg")).read_bytes(), b"image")

    def test_failed_analysis_does_not_block_successful_articles(self):
        self.add(1)
        (self.source / "失败.md").write_text("失败标记", encoding="utf-8")
        result = self.update()
        self.assertEqual(result["analysis"]["errors"], 1)
        self.assertEqual(result["copy"]["copied"], 1)
        self.assertEqual(result["copy"]["pending"], 1)
        (self.source / "失败.md").write_text("已恢复正文", encoding="utf-8")
        second = self.update()
        self.assertEqual(second["analysis"]["processed"], 1)
        self.assertEqual(second["copy"]["copied"], 1)

    def test_full_copy_history_is_recognized_and_changed_source_is_conflict(self):
        article = self.add(1)
        scan_library(self.database, self.source)
        analyze_pending(self.database, self.taxonomy, self.client, limit=10, max_chars=5000)
        copy_all_current(self.database, self.taxonomy, destination_root=self.destination,
                         manifest_path=self.root / "full.json")
        skipped = self.update()
        self.assertEqual(skipped["copy"]["skipped"], 1)
        old = next(self.destination.rglob("*.md"))
        before = old.read_bytes()
        article.write_text("新的正文版本", encoding="utf-8")
        changed = self.update()
        self.assertEqual(changed["copy"]["conflicts"], 1)
        self.assertEqual(changed["copy"]["copied"], 0)
        self.assertEqual(old.read_bytes(), before)

    def test_unclassified_goes_to_review_and_target_conflict_is_preserved(self):
        self.add(1)
        scan_library(self.database, self.source)
        analyze_pending(self.database, self.taxonomy, self.client, limit=10, max_chars=5000)
        with self.database.connect() as c:
            c.execute("UPDATE category_assignments SET level1_id='', level1_name='', level2_name='', needs_review=1")
        result = copy_incremental(self.database, self.taxonomy, destination_root=self.destination,
                                  manifest_path=self.root / "review.json")
        self.assertEqual(result["copied"], 1)
        self.assertTrue((self.destination / "_待审核/未分类/待定/文章1.md").exists())
        self.add(2)
        scan_library(self.database, self.source)
        analyze_pending(self.database, self.taxonomy, self.client, limit=10, max_chars=5000)
        target = self.destination / "两性与亲密关系/女性与婚恋观/文章2.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("用户已有笔记", encoding="utf-8")
        conflict = copy_incremental(self.database, self.taxonomy, destination_root=self.destination,
                                    manifest_path=self.root / "conflict.json")
        self.assertEqual(conflict["errors"], 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "用户已有笔记")

    def test_low_confidence_stays_in_category_and_excluded_article_is_not_copied(self):
        self.add(1)
        scan_library(self.database, self.source)
        analyze_pending(self.database, self.taxonomy, self.client, limit=10, max_chars=5000)
        with self.database.connect() as c:
            c.execute("UPDATE category_assignments SET confidence=0.8, needs_review=1")
        result = self.update()
        note = self.destination / "两性与亲密关系/女性与婚恋观/文章1.md"
        frontmatter = yaml.safe_load(note.read_text(encoding="utf-8").split("---", 2)[1])
        self.assertEqual(frontmatter["status"], "待审核")
        self.assertEqual(result["copy"]["copied"], 1)

    def test_filename_collision_does_not_block_other_articles(self):
        self.add(1)
        for name in ("a", "b"):
            folder = self.source / name
            folder.mkdir()
            (folder / "同名.md").write_text(name + "不同的正文", encoding="utf-8")
        result = self.update()
        self.assertEqual(result["copy"]["conflicts"], 2)
        self.assertEqual(result["copy"]["copied"], 1)

    def test_duplicate_with_old_rule_uses_profile_instead_of_full_text(self):
        original = self.add(1)
        self.update()
        (self.source / "重复.md").write_bytes(original.read_bytes())
        self.taxonomy = replace(self.taxonomy, version="test-next")
        result = self.update()
        self.assertEqual(result["analysis"]["processed"], 0)
        self.assertEqual(result["mapping"]["processed"], 2)
        self.assertEqual(result["copy"]["copied"], 0)
        self.assertEqual(self.client.calls, 3)

    def test_cli_update_and_copy_new_use_isolated_configuration(self):
        self.add(1)
        raw = dict(self.taxonomy.raw)
        raw["paths"] = {"source_library": {"path": str(self.source)},
                        "classified_library": {"path": str(self.destination)}}
        taxonomy_path = self.root / "taxonomy.yaml"
        taxonomy_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        settings = SimpleNamespace(project_root=self.root, taxonomy_path=taxonomy_path,
                                   database_path=self.database.path, source_library_override=None,
                                   api_key="fake", base_url="https://example.invalid", model="fake-model",
                                   thinking="disabled", timeout_seconds=1, max_retries=1)
        with patch("zhihu_classifier.cli.Settings.load", return_value=settings), \
             patch("zhihu_classifier.cli.DeepSeekClient", return_value=self.client), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["update", "--confirm-copy"]), 0)
            self.assertEqual(main(["copy-new", "--confirm-copy"]), 0)
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(len(list(self.destination.rglob("*.md"))), 1)

    def test_manual_tags_and_retention_decision_are_respected(self):
        self.add(1)
        self.add(2)
        scan_library(self.database, self.source)
        analyze_pending(self.database, self.taxonomy, self.client, limit=10, max_chars=5000)
        with self.database.connect() as c:
            first = c.execute("SELECT * FROM category_assignments WHERE article_id=1").fetchone()
            first_hash = c.execute("SELECT content_hash FROM articles WHERE id=1").fetchone()[0]
            second_hash = c.execute("SELECT content_hash FROM articles WHERE id=2").fetchone()[0]
            run = c.execute("""INSERT INTO manual_review_runs
                (imported_at, source_path, source_hash, source_taxonomy_version,
                 target_taxonomy_version, item_count, accepted_count, override_count, tag_count)
                VALUES ('now','manual.md','unique-review',?,?,1,1,0,1)""",
                (self.taxonomy.version, self.taxonomy.version)).lastrowid
            c.execute("""INSERT INTO manual_reviews
                (run_id,article_id,content_hash,previous_assignment_id,new_assignment_id,
                 review_result,level1_id,level1_name,level2_name,tags_json,review_note,created_at)
                VALUES (?,1,?,?,?,'通过','gender_and_intimacy','两性与亲密关系',
                        '女性与婚恋观','["收藏意图/测试"]','','now')""",
                (run, first_hash, first["id"], first["id"]))
            visual_run = c.execute("""INSERT INTO visual_review_runs
                (imported_at,source_path,source_hash,taxonomy_version,item_count,reviewed_count,override_count)
                VALUES ('now','visual.md','unique-visual',?,1,1,0)""",
                (self.taxonomy.version,)).lastrowid
            c.execute("""INSERT INTO visual_reviews
                (run_id,article_id,content_hash,taxonomy_version,review_status,category_decision,
                 level1_id,level1_name,level2_name,
                 visual_summary,collection_intent,retention_decision,tags_json,review_note,created_at)
                VALUES (?,2,?,?,'已识别','沿用','gender_and_intimacy','两性与亲密关系',
                        '女性与婚恋观','','','不纳入','[]','','now')""",
                (visual_run, second_hash, self.taxonomy.version))
        result = self.update()
        self.assertEqual(result["copy"]["copied"], 1)
        self.assertEqual(result["copy"]["excluded"], 1)
        note = next(self.destination.rglob("*.md"))
        frontmatter = yaml.safe_load(note.read_text(encoding="utf-8").split("---", 2)[1])
        self.assertIn("收藏意图/测试", frontmatter["tags"])

    def test_confirmation_and_manifest_protection(self):
        self.assertEqual(build_parser().parse_args(["update", "--confirm-copy"]).command, "update")
        with self.assertRaises(SystemExit):
            main(["update"])
        self.add(1)
        self.update()
        with self.assertRaises(FileExistsError):
            copy_incremental(self.database, self.taxonomy, destination_root=self.destination,
                             manifest_path=self.root / "manifest-1.json")

    def test_image_cleanup_in_incremental_copy_preserves_source_and_repeat(self):
        source = self.add(1)
        source.write_text(source.read_text(encoding="utf-8") +
            '\n![](data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="1" height="2"></svg>)'
            + '![[惊讶]](文章1/a.png) ![](https://picx.zhimg.com/example.jpg)', encoding="utf-8")
        source.with_suffix("").mkdir()
        (source.with_suffix("") / "a.png").write_bytes(b"fixture image")
        original = source.read_bytes()
        first = self.update()
        note = next(self.destination.rglob("文章1.md"))
        output = note.read_text(encoding="utf-8")
        self.assertNotIn("data:image/svg", output)
        self.assertIn(r"![\[惊讶\]](文章1/a.png)", output)
        self.assertEqual(first["copy"]["image_cleanup"],
            {"placeholders_removed": 1, "alt_text_fixed": 1, "remote_images_remaining": 1})
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual((note.with_suffix("") / "a.png").read_bytes(), b"fixture image")
        self.update()
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(note.read_text(encoding="utf-8"), output)

    def test_no_api_when_only_copying_existing_analysis(self):
        self.add(1)
        scan_library(self.database, self.source)
        analyze_pending(self.database, self.taxonomy, self.client, limit=10, max_chars=5000)
        def forbidden():
            raise AssertionError("不应创建API客户端")
        result = update_library(self.database, self.taxonomy, forbidden,
                                manifest_path=self.root / "no-api.json")
        self.assertEqual(result["copy"]["copied"], 1)


if __name__ == "__main__":
    unittest.main()
