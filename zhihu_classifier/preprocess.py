from __future__ import annotations

import re
from html import unescape


# 只接受独占一行的评论标题；正文中的“评论区有人问……”不是分隔符。
COMMENT_HEADING_RE = re.compile(
    r"^ {0,3}(?:#{1,6}[ \t]*)?(?:💬[ \t]*)?"
    r"(?:精选评论|热门评论|全部评论|评论区|评论列表|评论)"
    r"[ \t]*(?:\([^()\r\n]*\)|（[^（）\r\n]*）)?[ \t]*[:：]?[ \t]*$",
    re.IGNORECASE,
)
FRONT_MATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.DOTALL)
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.I)
HTML_ALT_RE = re.compile(r"\balt=[\"']([^\"']*)[\"']", re.I)


def _fence_end(line, fence):
    return re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*", line) is not None


def split_article_sections(text: str) -> tuple[str, str]:
    """共享正文边界；代码中的评论标题不作为评论区起点。"""
    text = FRONT_MATTER_RE.sub("", text.lstrip("\ufeff"), count=1)
    main, comments = [], []
    in_comments = False
    fence = None
    html_code = False
    for line in text.splitlines():
        marker = FENCE_RE.match(line)
        fenced_line = fence is not None or marker is not None
        indented = line.startswith(("    ", "\t"))
        is_code = fence is not None or html_code or indented
        if not is_code and not marker and not in_comments and COMMENT_HEADING_RE.fullmatch(line):
            in_comments = True
            continue
        (comments if in_comments else main).append(line)
        if fence:
            if _fence_end(line, fence):
                fence = None
        elif marker:
            token = marker.group(1)
            fence = (token[0], len(token))
        if not fenced_line and not indented and re.search(r"<(?:pre|code)\b", line, re.I):
            html_code = True
        if not fenced_line and not indented and re.search(r"</(?:pre|code)\s*>", line, re.I):
            html_code = False
    return "\n".join(main), "\n".join(comments)


def _parts(text):
    """划分代码围栏、代码内容和普通文本；不改写代码示例。"""
    fence = None
    normal = []
    def inline_parts(chunk):
        start = 0
        for match in re.finditer(r"(?<!`)(`+)(?!`)", chunk):
            if match.start() < start:
                continue
            close = re.search(r"(?<!`)" + re.escape(match[1]) + r"(?!`)", chunk[match.end():])
            if close is None:
                continue
            end = match.end() + close.end()
            yield "text", chunk[start:match.start()]
            yield "code", chunk[match.start():end]
            start = end
        yield "text", chunk[start:]
    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        marker = FENCE_RE.match(raw)
        if fence:
            if _fence_end(raw, fence):
                yield "marker", line
                fence = None
            else:
                yield "code", line
        elif marker or line.startswith(("    ", "\t")):
            yield from inline_parts("".join(normal))
            normal.clear()
            if marker:
                token = marker[1]
                fence = (token[0], len(token))
                yield "marker", line
            else:
                yield "code", line
        else:
            normal.append(line)
    yield from inline_parts("".join(normal))


def _closing(text, start, opener, closer):
    depth = 1
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == opener:
            depth += 1
        elif text[index] == closer:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _markdown_links(text, *, images, describe_images=False):
    """逐个处理图片/链接，保留相邻的正文，支持嵌套方括号和括号。"""
    result = []
    start = 0
    pattern = r"!\[" if images else r"(?<!!)\["
    for match in re.finditer(pattern, text):
        if match.start() < start:
            continue
        opening = match.end() - 1
        end_label = _closing(text, opening, "[", "]")
        if end_label is None:
            continue
        end = end_label + 1
        label = text[opening + 1:end_label]
        wiki_image = images and text.startswith("![[", match.start()) and label.startswith("[")
        if end < len(text) and text[end] in "([":
            opener = text[end]
            closer = ")" if opener == "(" else "]"
            end_target = _closing(text, end, opener, closer)
            if end_target is None:
                continue
            end = end_target + 1
        elif not wiki_image:
            continue
        result.append(text[start:match.start()])
        if images:
            if describe_images and label.strip() and not wiki_image:
                alt = re.sub(r"\\(.)", r"\1", label).strip()
                result.append(f"[图片说明：{alt}]")
        else:
            result.append(label)
        start = end
    result.append(text[start:])
    return "".join(result)


def _image_text(text, *, describe):
    text = _markdown_links(text, images=True, describe_images=describe)
    def html_image(match):
        alt = HTML_ALT_RE.search(match[0])
        return f"[图片说明：{unescape(alt[1])}]" if describe and alt and alt[1].strip() else ""
    text = HTML_IMAGE_RE.sub(html_image, text)
    return re.sub(r"<svg\b[^>]*>.*?</svg>", "", text, flags=re.S | re.I)


def _model_text(text):
    return "".join(chunk if kind != "text" else _image_text(chunk, describe=True)
                   for kind, chunk in _parts(text)).strip()


def prepare_article_for_model(text: str) -> str:
    main, comments = split_article_sections(text)
    main = _model_text(main)
    comments = _model_text(comments)
    result = "【文章正文（分类与摘要的主要依据）】\n" + main
    if comments:
        result += "\n\n【评论区（次要材料，仅用于补充语境，不得主导分类）】\n" + comments
    return result


# 只移除明确的常见HTML标签，保留书名、比较符和颜文字等普通尖括号内容。
VISIBLE_TAG_RE = re.compile(
    r"</?(?:a|abbr|address|article|aside|b|blockquote|body|br|button|caption|center|cite|code|col|colgroup|"
    r"dd|del|details|dfn|div|dl|dt|em|figcaption|figure|font|footer|form|h[1-6]|head|header|hr|html|i|"
    r"img|input|ins|kbd|label|legend|li|link|main|mark|meta|nav|ol|option|p|pre|q|rp|rt|ruby|s|samp|"
    r"section|select|small|source|span|strong|sub|summary|sup|table|tbody|td|textarea|tfoot|th|thead|"
    r"time|title|tr|u|ul|var|video|wbr)(?=[ \t\r\n/>])[^<>\n]*>", re.I
)


def _count_prose(text):
    text = _image_text(text, describe=False)
    text = re.sub(r"(?m)^ {0,3}\[[^\]\n]+\]:[^\n]*$", "", text)
    text = _markdown_links(text, images=False)
    text = re.sub(r"https?://[^\s<>]+", "", text, flags=re.I)
    text = re.sub(r"<!--.*?-->|<(style|script)\b[^>]*>.*?</\1\s*>", "", text, flags=re.S | re.I)
    text = VISIBLE_TAG_RE.sub("", text)
    return sum(char.isalnum() for char in unescape(text))


def count_body_characters(text: str) -> int:
    """正文汉字、字母和数字逐字符计数；代码按可见源码统计。"""
    main, _ = split_article_sections(text)
    lines = main.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.strip() or re.fullmatch(r"\s*(?:---+|\*\*\*+)\s*", line):
            continue
        if re.match(r"^ {0,3}#\s+", line):
            lines[index] = ""
        break
    return sum(0 if kind == "marker" else sum(char.isalnum() for char in chunk) if kind == "code"
               else _count_prose(chunk) for kind, chunk in _parts("".join(lines)))
