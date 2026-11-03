"""Backfill only the numeric word-count property; preserve body and other metadata."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

import yaml

from .preprocess import count_body_characters

HEADER = re.compile(r"\A---[ \t]*\r?\n(?P<metadata>.*?)(?P<closing>^---[ \t]*(?:\r?\n|$))", re.S | re.M)
KEY = re.compile(r"^(?:正文字数|\"正文字数\"|'正文字数'):[^\r\n]*(?:\r?\n|$)", re.M)


def set_word_count(original: bytes):
    bom = original.startswith(b"\xef\xbb\xbf")
    text = original.decode("utf-8-sig")
    if "\ufffd" in text:
        raise ValueError("笔记已有替换字符，保留原样等待检查")
    count = count_body_characters(text)
    match = HEADER.match(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    if match:
        metadata = yaml.safe_load(match["metadata"]) or {}
        if not isinstance(metadata, dict):
            raise ValueError("笔记属性必须是YAML对象")
        if type(metadata.get("正文字数")) is int and metadata["正文字数"] == count:
            return original, count
        header = match["metadata"]
        if "正文字数" in metadata:
            header, replacements = KEY.subn("正文字数: " + str(count) + newline, header)
            if replacements != 1:
                raise ValueError("正文字数属性不是可安全更新的单行字段")
        else:
            if header and not header.endswith("\n"):
                header += newline
            header += f"正文字数: {count}{newline}"
        updated = text[:match.start("metadata")] + header + text[match.end("metadata"):]
        original_body = text[match.end():]
    else:
        if text.startswith("---\n") or text.startswith("---\r\n"):
            raise ValueError("笔记属性缺少结束标记")
        metadata = {}
        original_body = text
        updated = f"---{newline}正文字数: {count}{newline}---{newline}" + text
    after = HEADER.match(updated)
    parsed = yaml.safe_load(after["metadata"])
    assert type(parsed["正文字数"]) is int and parsed["正文字数"] == count
    assert {key:value for key,value in parsed.items() if key != "正文字数"} == {
        key:value for key,value in metadata.items() if key != "正文字数"}, "其他属性发生变化"
    assert updated[after.end():] == original_body, "正文发生变化"
    output = (b"\xef\xbb\xbf" if bom else b"") + updated.encode("utf-8")
    assert set_word_count_idempotent(output, count)
    return output, count


def set_word_count_idempotent(output, count):
    text = output.decode("utf-8-sig")
    return count_body_characters(text) == count and "\ufffd" not in text


def note_paths(root):
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in {".obsidian", ".trash", ".git"}
                                and not (Path(parent) / d).is_symlink())
        for name in sorted(files):
            if name.lower().endswith(".md"):
                yield Path(parent) / name


def backfill_word_counts(vault: Path, *, apply: bool, report_root: Path):
    vault = vault.resolve(strict=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    backup_root = vault / ".obsidian" / ("codex-word-count-backup-" + run_id)
    report_path = report_root / ("WORD_COUNTS_" + run_id + ".json")
    result = {"mode": "apply" if apply else "preview", "total": 0, "updated": 0, "would_update": 0,
              "unchanged": 0, "errors": [], "items": [], "backup": str(backup_root) if apply else None}
    sha = lambda data: hashlib.sha256(data).hexdigest()
    try:
        for path in note_paths(vault):
            result["total"] += 1
            try:
                if path.is_symlink() or vault not in path.resolve().parents:
                    raise ValueError("笔记不是库内普通文件")
                original = path.read_bytes()
                output, count = set_word_count(original)
                if output == original:
                    result["unchanged"] += 1
                    continue
                if not apply:
                    result["would_update"] += 1
                    continue
                backup = backup_root / path.relative_to(vault)
                backup.parent.mkdir(parents=True, exist_ok=True)
                with backup.open("xb") as handle:
                    handle.write(original)
                if backup.read_bytes() != original:
                    raise OSError("备份校验失败")
                fd, name = tempfile.mkstemp(prefix=".word-count-", suffix=".tmp", dir=path.parent)
                temporary = Path(name)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(output)
                    if temporary.read_bytes() != output or path.read_bytes() != original:
                        raise OSError("笔记在更新期间变化，拒绝覆盖")
                    os.replace(temporary, path)
                finally:
                    # 仅删除本次创建的一个明确临时文件。
                    temporary.unlink(missing_ok=True)
                if path.read_bytes() != output:
                    raise OSError("属性写入后校验失败")
                result["updated"] += 1
                result["items"].append({"path": str(path), "backup": str(backup), "正文字数": count,
                                        "before_hash": sha(original), "after_hash": sha(output)})
            except (OSError, UnicodeError, ValueError, AssertionError, yaml.YAMLError) as exc:
                result["errors"].append({"path": str(path), "error": str(exc)})
            if result["total"] % 200 == 0:
                print(f"[字数补全] 已检查 {result['total']} 篇，更新 {result['updated']}，异常 {len(result['errors'])}", flush=True)
    finally:
        if apply:
            report_root.mkdir(parents=True, exist_ok=True)
            result["report"] = str(report_path)
            with report_path.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
    return result
