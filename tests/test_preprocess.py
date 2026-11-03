import unittest

from zhihu_classifier.preprocess import prepare_article_for_model


class PreprocessTests(unittest.TestCase):
    def test_filters_image_noise_without_truncating_text_and_marks_comments(self):
        source = """---
old: metadata
---
# 正文
第一段。
![](文章/图片.jpg)
![结构图](文章/结构图.png)
<svg width="1"></svg>
## 精选评论
评论文字。
![](文章/评论图片.jpg)
"""
        result = prepare_article_for_model(source)
        self.assertIn("第一段。", result)
        self.assertIn("[图片说明：结构图]", result)
        self.assertIn("评论文字。", result)
        self.assertIn("评论区（次要材料", result)
        self.assertNotIn("图片.jpg", result)
        self.assertNotIn("<svg", result)
        self.assertNotIn("old: metadata", result)

    def test_prose_mentioning_comments_stays_in_main_body(self):
        text="# 标题\n正文\n评论区有人问我，所以补充重要正文。\n后续正文\n### 💬 精选评论 (第一页)\n真正评论"
        result=prepare_article_for_model(text)
        main,comments=result.split("【评论区（次要材料")
        self.assertIn("评论区有人问我，所以补充重要正文。",main)
        self.assertIn("后续正文",main)
        self.assertNotIn("真正评论",main)
        self.assertIn("真正评论",comments)

    def test_images_never_remove_neighboring_text(self):
        text="前文![](a.jpg)关键正文![示意图](b(1).png)后文\n<img src='a.png'>中间文字<img src='b.png'>"
        result=prepare_article_for_model(text)
        self.assertIn("前文关键正文[图片说明：示意图]后文",result)
        self.assertIn("中间文字",result)
        self.assertNotIn("a.jpg",result)

    def test_code_with_comment_heading_and_images_is_preserved(self):
        text='# 标题\n```html\n# 评论区\n![](example.png)\n<code>\n<button disabled>保存</button>\n```\n真正正文\n## 评论区\n真正评论'
        result=prepare_article_for_model(text)
        main,comments=result.split("【评论区（次要材料")
        self.assertIn("# 评论区\n![](example.png)",main)
        self.assertIn("真正正文",main)
        self.assertIn("真正评论",comments)

    def test_inline_code_and_nested_image_targets(self):
        text='正文 `![](example.png)` ![嵌套图](folder/image(1(2)).png) 后文'
        result=prepare_article_for_model(text)
        self.assertIn('`![](example.png)`',result)
        self.assertIn('[图片说明：嵌套图] 后文',result)
        self.assertNotIn('folder/image',result)

    def test_malformed_image_does_not_drop_text(self):
        text='正文 ![不完整图片](没有结束括号 后续正文'
        self.assertIn(text,prepare_article_for_model(text))


if __name__ == "__main__":
    unittest.main()
