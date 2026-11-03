from pathlib import Path
import tempfile
import unittest
import yaml

from zhihu_classifier.word_count_backfill import set_word_count, backfill_word_counts, HEADER


class BackfillTests(unittest.TestCase):
    def test_preserves_other_metadata_body_bom_and_crlf(self):
        original=b"\xef\xbb\xbf"+'---\r\nauthor: 作者\r\nstatus: 待审核\r\n---\r\n# 标题\r\n中文正文\r\n## 评论区\r\n评论'.encode('utf-8')
        updated,count=set_word_count(original)
        self.assertEqual(count,4)
        self.assertTrue(updated.startswith(b"\xef\xbb\xbf"))
        self.assertIn('正文字数: 4\r\n'.encode('utf-8'),updated)
        self.assertEqual(updated.decode('utf-8-sig').split('---',2)[2],original.decode('utf-8-sig').split('---',2)[2])
        self.assertEqual(set_word_count(updated)[0],updated)

    def test_existing_property_is_recomputed_as_number(self):
        original='---\nauthor: 作者\n正文字数: "999"\n---\n正文'.encode()
        updated,count=set_word_count(original)
        self.assertEqual(count,2)
        meta=yaml.safe_load(HEADER.match(updated.decode())['metadata'])
        self.assertEqual(meta['正文字数'],2);self.assertIs(type(meta['正文字数']),int)
        self.assertEqual(meta['author'],'作者')

    def test_note_without_frontmatter_gets_property_without_body_changes(self):
        original='# 标题\n正文\n'.encode()
        updated,count=set_word_count(original)
        self.assertEqual(count,2)
        match=HEADER.match(updated.decode())
        self.assertEqual(updated.decode()[match.end():],original.decode())

    def test_malformed_frontmatter_is_not_rewritten(self):
        for original in ('---\nauthor: [invalid\n---\n正文','---\nauthor: 没有结束标记'):
            with self.assertRaises((ValueError,yaml.YAMLError)):set_word_count(original.encode())

    def test_preview_no_writes_then_apply_with_backup_and_repeat(self):
        root=Path(tempfile.mkdtemp(prefix='word-count-backfill-'))
        vault=root/'vault';vault.mkdir()
        note=vault/'笔记.md';original='# 标题\n正文'.encode();note.write_bytes(original)
        hidden=vault/'.obsidian';hidden.mkdir()
        (hidden/'不应处理.md').write_text('配置备份',encoding='utf-8')
        preview=backfill_word_counts(vault,apply=False,report_root=root/'reports')
        self.assertEqual(preview['total'],1);self.assertEqual(preview['would_update'],1)
        self.assertEqual(note.read_bytes(),original);self.assertFalse((root/'reports').exists())
        result=backfill_word_counts(vault,apply=True,report_root=root/'reports')
        self.assertEqual(result['updated'],1);self.assertEqual(result['errors'],[])
        self.assertEqual(Path(result['items'][0]['backup']).read_bytes(),original)
        after=note.read_bytes()
        repeated=backfill_word_counts(vault,apply=True,report_root=root/'reports')
        self.assertEqual(repeated['updated'],0);self.assertEqual(repeated['unchanged'],1)
        self.assertEqual(note.read_bytes(),after)
