import unittest
import yaml

from zhihu_classifier.preprocess import count_body_characters
from zhihu_classifier.copier import _render_obsidian_copy


class WordCountTests(unittest.TestCase):
    def test_chinese_letters_and_numbers_are_characters(self):
        self.assertEqual(count_body_characters("中文 AI 2026，。！"),8)

    def test_metadata_title_and_comment_area_excluded(self):
        text="---\ntitle: 元数据\nsummary: 很长的摘要\n---\n\n# 文章标题\n---\n中文正文\n### 💬 精选评论 (第一页)\n评论不计入"
        self.assertEqual(count_body_characters(text),4)

    def test_images_and_urls_excluded_but_link_text_kept(self):
        text='正文 ![图片说明](附件/图片.png) ![[笔记图片.png]] [说明](https://example.invalid/abcdef) https://example.invalid/long/path'
        text+=' ![\\[惊讶\\]](附件/a.png) <img src="图片.jpg" alt="说明">'
        self.assertEqual(count_body_characters(text),4)

    def test_svg_data_html_entities_and_reference_images(self):
        text='中文 ![](data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="999"></svg>)'
        text+=' <b>正文</b>&nbsp; ![说明][image]\n[image]: https://example.invalid/image.jpg'
        self.assertEqual(count_body_characters(text),4)

    def test_comment_heading_inside_fenced_code_does_not_end_body(self):
        text='# 标题\n```python\n# 评论区\nabc123\n```\n正文\n## 评论区\n不计入'
        self.assertEqual(count_body_characters(text),11)

    def test_empty_and_picture_only_body_have_zero_count(self):
        for text in ('', '# 标题\n![](a.png)', '---\ntitle: 图片笔记\n---\n![[a.png]]'):
            self.assertEqual(count_body_characters(text),0)

    def test_renderer_sets_numeric_property_and_preserves_article(self):
        source='---\nauthor: 作者\n正文字数: 999\n---\n# 标题\n中文正文\n## 评论区\n评论'.encode('utf-8')
        output,generated=_render_obsidian_copy(source,title='标题',category='健康/日常保健与长寿',
            tags=[],summary='摘要',source_hash='source-hash',taxonomy_version='1.1',needs_review=True)
        meta=yaml.safe_load(output.decode('utf-8').split('---',2)[1])
        self.assertEqual(meta['正文字数'],4)
        self.assertIs(type(meta['正文字数']),int)
        self.assertEqual(generated['正文字数'],4)
        self.assertEqual(meta['author'],'作者')
        self.assertEqual(meta['source_hash'],'source-hash')
        self.assertEqual(meta['status'],'待审核')
        self.assertIn('中文正文\n## 评论区\n评论',output.decode('utf-8'))
        self.assertIn('正文字数: 999',source.decode('utf-8'))
