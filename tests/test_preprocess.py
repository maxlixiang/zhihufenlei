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


if __name__ == "__main__":
    unittest.main()
