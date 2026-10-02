"""Conservative display cleanup for Obsidian copies; never edits source articles."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import unquote
import xml.etree.ElementTree as ET


@dataclass
class ImageCleanup:
    placeholders_removed: int = 0
    alt_text_fixed: int = 0
    remote_images_remaining: int = 0


EMPTY_SVG = re.compile(r"!\[[^\]\n]*\]\((data:image/svg\+xml;[^\n)]*)\)", re.I)
NESTED_ALT = re.compile(r"!\[((?:\\.|[^\[\]\n]|\[[^\]\n]*\])*)\]\(([^)\n]*)\)")
FRONT_MATTER = re.compile(r"\A---\r?\n.*?\r?\n---(?:\r?\n|$)", re.S)
PROTECTED = re.compile(r"(`+).*?\1|<(pre|code)\b[^>]*>.*?</\2>", re.S | re.I)


def clean_markdown_images(text: str) -> tuple[str, ImageCleanup]:
    """移除真正空白的SVG占位图，转义图片alt；保留元数据、代码和实际图片。"""
    stats = ImageCleanup()

    def clean(chunk):
        def svg(match):
            try:
                value = unquote(match.group(1).split(",", 1)[1])
                element = ET.fromstring(value)
            except (ValueError, IndexError, ET.ParseError):
                return match.group(0)
            allowed = {"width", "height", "viewBox", "version"}
            if (element.tag != "{http://www.w3.org/2000/svg}svg"
                    or list(element) or (element.text or "").strip()
                    or not set(element.attrib).issubset(allowed)):
                return match.group(0)
            stats.placeholders_removed += 1
            return ""

        def alt(match):
            label = match.group(1)
            # 已转义的方括号保持原样；防止 ![[表情]] 被当成Obsidian嵌入。
            escaped = re.sub(r"(?<!\\)([\[\]])", r"\\\1", label)
            if escaped != label:
                stats.alt_text_fixed += 1
            return f"![{escaped}]({match.group(2)})"

        result = NESTED_ALT.sub(alt, EMPTY_SVG.sub(svg, chunk))
        stats.remote_images_remaining += len(re.findall(
            r"!\[(?:\\.|[^\]])*\]\((?:https?://|//)[^)]+\)|<img\b[^>]*?\bsrc=[\"'](?:https?://|//)",
            result, re.I))
        return result

    prefix = ""
    match = FRONT_MATTER.match(text)
    if match:
        prefix, text = text[:match.end()], text[match.end():]
    # 先隔离代码块和行内代码，代码示例中的图片写法不清理。
    fence = None
    normal = []
    output = [prefix]

    def flush():
        chunk = "".join(normal)
        start = 0
        for protected in PROTECTED.finditer(chunk):
            output.append(clean(chunk[start:protected.start()]))
            output.append(protected.group(0))
            start = protected.end()
        output.append(clean(chunk[start:]))
        normal.clear()

    for line in text.splitlines(keepends=True):
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            output.append(line)
            if re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*$", line):
                fence = None
        elif opening:
            flush()
            marker = opening.group(1)
            fence = (marker[0], len(marker))
            output.append(line)
        else:
            normal.append(line)
    flush()
    return "".join(output), stats
