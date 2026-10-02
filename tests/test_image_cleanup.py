import unittest
from urllib.parse import quote
from zhihu_classifier.image_cleanup import clean_markdown_images
from zhihu_classifier.copier import _render_obsidian_copy

EMPTY = '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="1000"></svg>'
PLACEHOLDER = '![](data:image/svg+xml;utf8,' + EMPTY + ')'


class ImageCleanupTests(unittest.TestCase):
    def test_real_image_and_prose_preserved_while_placeholder_removed(self):
        text = '正文 ![](图片/a.jpg)' + PLACEHOLDER + '\n评论'
        result, stats = clean_markdown_images(text)
        self.assertEqual(result, '正文 ![](图片/a.jpg)\n评论')
        self.assertEqual(stats.placeholders_removed, 1)

    def test_encoded_empty_svg_and_nonempty_svg(self):
        encoded = '![](data:image/svg+xml;utf8,' + quote(EMPTY) + ')'
        real = PLACEHOLDER.replace('</svg>', '<rect width="10" height="10"/></svg>')
        styled = PLACEHOLDER.replace('width="720"', 'style="background:red" width="720"')
        malformed = '![](data:image/svg+xml;utf8,invalid)'
        result, stats = clean_markdown_images(encoded + real + styled + malformed)
        self.assertEqual(result, real + styled + malformed)
        self.assertEqual(stats.placeholders_removed, 1)

    def test_alt_escape_idempotence_and_wiki_links_preserved(self):
        text = '![[惊讶]](图片/a.png) ![[真实附件.png]]'
        result, stats = clean_markdown_images(text)
        self.assertEqual(result, r'![\[惊讶\]](图片/a.png) ![[真实附件.png]]')
        self.assertEqual(stats.alt_text_fixed, 1)
        again, second = clean_markdown_images(result)
        self.assertEqual(again, result)
        self.assertEqual(second.alt_text_fixed, 0)

    def test_frontmatter_inline_fenced_and_html_code_preserved(self):
        text = '---\nsummary: ' + PLACEHOLDER + '\n---\n'
        text += '`' + PLACEHOLDER + '`\n```markdown\n' + PLACEHOLDER + '\n```\n'
        text += '~~~markdown\n' + PLACEHOLDER + '\n~~~~\n'
        text += '<pre>' + PLACEHOLDER + '</pre>\n'
        result, stats = clean_markdown_images(text + PLACEHOLDER)
        self.assertEqual(result, text)
        self.assertEqual(stats.placeholders_removed, 1)

    def test_renderer_cleans_body_without_changing_source_or_status(self):
        source = ('# 正文\n' + PLACEHOLDER + '![[惊讶]](图片/a.png)').encode('utf-8')
        original = source
        result, metadata = _render_obsidian_copy(source, title='文章', category='健康/疾病预防与就医',
            tags=['标签'], summary='摘要', source_hash='hash', taxonomy_version='1.1', needs_review=True)
        self.assertNotIn('data:image/svg', result.decode('utf-8'))
        self.assertIn(r'![\[惊讶\]](图片/a.png)', result.decode('utf-8'))
        self.assertEqual(metadata['status'], '待审核')
        self.assertEqual(source, original)

    def test_remote_images_ordinary_links_and_unicode_untouched(self):
        text = '中文 ![](https://picx.zhimg.com/example.jpg) [链接](a) ![](a.png)'
        result, stats = clean_markdown_images(text)
        self.assertEqual(result, text)
        self.assertEqual(stats.placeholders_removed + stats.alt_text_fixed, 0)
