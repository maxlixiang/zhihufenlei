from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import unquote

import yaml


FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(([^)]+)\)|<img[^>]+src=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
REQUIRED_FIELDS = {
    "title", "category", "tags", "summary", "source_hash", "taxonomy_version", "status"
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_body(text: str) -> str:
    match = FRONT_MATTER_RE.match(text)
    return text[match.end():] if match else text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--database", type=Path, default=Path("data/classification.db"))
    args = parser.parse_args()

    errors: list[str] = []
    markdown_files = local_references = attachment_files = 0
    copy_run_ids: list[int] = []
    for manifest_path in args.manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        copy_run_ids.append(int(manifest["copy_run_id"]))
        for item in manifest["items"]:
            markdown_files += 1
            source = Path(item["source_path"])
            target = Path(item["destination_path"])
            try:
                source_text = source.read_bytes().decode("utf-8-sig")
                target_text = target.read_bytes().decode("utf-8")
                match = FRONT_MATTER_RE.match(target_text)
                if not match:
                    raise ValueError("缺少 YAML Front Matter")
                frontmatter = yaml.safe_load(match.group(1))
                if not isinstance(frontmatter, dict):
                    raise ValueError("Front Matter 顶层不是对象")
                missing = REQUIRED_FIELDS - set(frontmatter)
                if missing:
                    raise ValueError("Front Matter 缺少字段：" + ", ".join(sorted(missing)))
                target_body = target_text[match.end():]
                if target_body.startswith("\n"):
                    target_body = target_body[1:]
                if target_body != source_body(source_text):
                    raise ValueError("Front Matter 之后的正文与原文不一致")
                if frontmatter["source_hash"] != item["content_hash"]:
                    raise ValueError("Front Matter 源哈希不一致")
                if sha256(source) != item["content_hash"]:
                    raise ValueError("原文哈希不一致")
                if sha256(target) != item["output_content_hash"]:
                    raise ValueError("生成副本哈希不一致")
                if "\ufffd" in target_text:
                    raise ValueError("检测到 Unicode 替换字符")
                for image_match in IMAGE_RE.finditer(target_text):
                    reference = image_match.group(1) or image_match.group(2)
                    if reference.startswith(("data:", "http:", "https:", "//")):
                        continue
                    local_references += 1
                    relative = unquote(re.split(r"[?#]", reference, maxsplit=1)[0])
                    if not (target.parent / relative).is_file():
                        raise ValueError(f"本地图片引用失效：{relative}")
            except Exception as exc:
                errors.append(f"{target}: {exc}")

    connection = sqlite3.connect(args.database)
    placeholders = ",".join("?" for _ in copy_run_ids)
    rows = connection.execute(
        f"SELECT destination_path, content_hash FROM copy_attachments "
        f"WHERE copy_run_id IN ({placeholders})",
        copy_run_ids,
    ).fetchall()
    connection.close()
    for destination, expected_hash in rows:
        attachment_files += 1
        path = Path(destination)
        if not path.is_file() or sha256(path) != expected_hash:
            errors.append(f"附件缺失或哈希不一致：{path}")

    result = {
        "markdown_files": markdown_files,
        "attachment_files": attachment_files,
        "local_image_references": local_references,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
