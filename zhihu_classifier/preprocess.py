from __future__ import annotations

import re
from html import unescape


COMMENT_HEADING_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:💬\s*)?"
    r"(?:精选评论|热门评论|全部评论|评论区|评论列表|评论\s*(?:\(|（|$))",
    re.IGNORECASE,
)
FRONT_MATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.DOTALL)
MARKDOWN_IMAGE_LINE_RE = re.compile(r"^\s*!\[([^\]]*)\]\(.+\)\s*$")
HTML_IMAGE_LINE_RE = re.compile(r"^\s*<img\b([^>]*)/?>\s*$", re.IGNORECASE)
HTML_ALT_RE = re.compile(r"\balt=[\"']([^\"']*)[\"']", re.IGNORECASE)


def _clean_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        markdown_image = MARKDOWN_IMAGE_LINE_RE.match(line)
        if markdown_image:
            alt = markdown_image.group(1).strip()
            if alt:
                cleaned.append(f"[图片说明：{alt}]")
            continue
        html_image = HTML_IMAGE_LINE_RE.match(line)
        if html_image:
            alt_match = HTML_ALT_RE.search(html_image.group(1))
            alt = alt_match.group(1).strip() if alt_match else ""
            if alt:
                cleaned.append(f"[图片说明：{alt}]")
            continue
        if stripped.startswith("<svg ") or stripped.startswith("data:image/"):
            continue
        cleaned.append(line.rstrip())
    return cleaned


def prepare_article_for_model(text: str) -> str:
    """移除无语义图片噪声；保留全文文字，并把评论区标为次要材料。"""
    text = FRONT_MATTER_RE.sub("", text, count=1)
    main_lines: list[str] = []
    comment_lines: list[str] = []
    in_comments = False
    for line in text.splitlines():
        if not in_comments and COMMENT_HEADING_RE.match(line):
            in_comments = True
            continue
        (comment_lines if in_comments else main_lines).append(line)

    main = "\n".join(_clean_lines(main_lines)).strip()
    comments = "\n".join(_clean_lines(comment_lines)).strip()
    if comments:
        return (
            "【文章正文（分类与摘要的主要依据）】\n"
            f"{main}\n\n"
            "【评论区（次要材料，仅用于补充语境，不得主导分类）】\n"
            f"{comments}"
        )
    return "【文章正文（分类与摘要的主要依据）】\n" + main


def count_body_characters(text: str) -> int:
    """正文汉字、字母和数字逐字符计数；不含首标题、评论、图片及URL。"""
    text = FRONT_MATTER_RE.sub("", text.lstrip("\ufeff"), count=1)
    lines = []
    at_start = True
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*$", line):
                fence = None
            else:
                lines.append(line)
            continue
        if marker:
            fence = (marker.group(1)[0], len(marker.group(1)))
            at_start = False
            continue
        if COMMENT_HEADING_RE.match(line):
            break
        if at_start and (not line.strip() or re.match(r"^\s*(?:---+|\*\*\*+)\s*$", line)):
            continue
        if at_start and re.match(r"^\s*#\s+", line):
            at_start = False
            continue
        at_start = False
        # 引用链接定义属于Markdown元信息，不是可见正文。
        if re.match(r"^ {0,3}\[[^\]]+\]:\s*", line):
            continue
        lines.append(line)
    body = "\n".join(lines)
    body = re.sub(r"<!--.*?-->|<svg\b[^>]*>.*?</svg>", "", body, flags=re.S | re.I)
    body = re.sub(r"!\[(?:\\.|[^\[\]\\\n]|\[[^\]\n]*\])*\]\((?:[^()\\\n]|\\.|\([^()\n]*\))*\)", "", body)
    body = re.sub(r"!\[[^\]\n]*\]\[[^\]\n]*\]|!\[\[[^\]\n]*\]\]", "", body)
    body = re.sub(r"\[((?:\\.|[^\]\\\n])*)\]\((?:[^()\\\n]|\\.|\([^()\n]*\))*\)", r"\1", body)
    body = re.sub(r"<[^>\n]+>|https?://[^\s<>]+", "", body, flags=re.I)
    return sum(character.isalnum() for character in unescape(body))
