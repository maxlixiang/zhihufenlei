from __future__ import annotations

import re


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
