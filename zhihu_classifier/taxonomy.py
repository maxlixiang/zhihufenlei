from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import yaml


class TaxonomyError(ValueError):
    pass


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    kind: str
    status: str
    purpose: str
    second_level: tuple[str, ...]
    uses_candidates: bool
    boundaries: Any


@dataclass(frozen=True)
class ReviewBucket:
    id: str
    name: str
    status: str
    path: str
    purpose: str
    criteria: tuple[str, ...]
    boundaries: tuple[str, ...]


@dataclass(frozen=True)
class Taxonomy:
    version: str
    status: str
    source_library: Path
    classified_library: Path
    categories: tuple[Category, ...]
    core_priority: tuple[str, ...]
    raw: dict[str, Any]
    review_buckets: tuple[ReviewBucket, ...] = ()

    @classmethod
    def load(cls, path: Path, *, allow_candidates: bool = False) -> "Taxonomy":
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise TaxonomyError(f"无法读取分类规则：{exc}") from exc
        if not isinstance(raw, dict):
            raise TaxonomyError("taxonomy.yaml 顶层必须是对象")

        version = str(raw.get("taxonomy_version", "")).strip()
        status = str(raw.get("status", "")).strip()
        source_value = raw.get("paths", {}).get("source_library", {}).get("path")
        classified_value = raw.get("paths", {}).get("classified_library", {}).get("path")
        if not version or not source_value or not classified_value:
            raise TaxonomyError("分类版本、原始库路径或分类库路径缺失")

        categories: list[Category] = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for item in raw.get("categories", []):
            category_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            if not category_id or not name or category_id in seen_ids or name in seen_names:
                raise TaxonomyError(f"分类 ID 或名称缺失/重复：{category_id or name}")
            seen_ids.add(category_id)
            seen_names.add(name)
            confirmed = tuple(str(v).strip() for v in item.get("second_level", []) if str(v).strip())
            candidates = tuple(
                str(v).strip() for v in item.get("second_level_candidates", []) if str(v).strip()
            )
            uses_candidates = not confirmed and bool(candidates)
            second_level = candidates if allow_candidates and uses_candidates else confirmed
            if len(set(second_level)) != len(second_level):
                raise TaxonomyError(f"二级分类重复：{name}")
            categories.append(
                Category(
                    id=category_id,
                    name=name,
                    kind=str(item.get("kind", "standard")),
                    status=str(item.get("status", "draft")),
                    purpose=str(item.get("purpose", "")).strip(),
                    second_level=second_level,
                    uses_candidates=uses_candidates,
                    boundaries=item.get("boundaries", {}),
                )
            )

        review_buckets: list[ReviewBucket] = []
        seen_bucket_ids: set[str] = set()
        seen_bucket_paths: set[str] = set()
        for item in raw.get("special_review_buckets", []):
            bucket_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            bucket_path = str(item.get("path", "")).strip()
            if (
                not bucket_id
                or not name
                or not bucket_path
                or bucket_id in seen_bucket_ids
                or bucket_path in seen_bucket_paths
            ):
                raise TaxonomyError(
                    f"特殊审核区 ID、名称或路径缺失/重复：{bucket_id or name or bucket_path}"
                )
            seen_bucket_ids.add(bucket_id)
            seen_bucket_paths.add(bucket_path)
            review_buckets.append(
                ReviewBucket(
                    id=bucket_id,
                    name=name,
                    status=str(item.get("status", "draft")),
                    path=bucket_path,
                    purpose=str(item.get("purpose", "")).strip(),
                    criteria=tuple(
                        str(v).strip()
                        for v in item.get("criteria", [])
                        if str(v).strip()
                    ),
                    boundaries=tuple(
                        str(v).strip()
                        for v in item.get("boundaries", [])
                        if str(v).strip()
                    ),
                )
            )

        return cls(
            version=version,
            status=status,
            source_library=Path(source_value),
            classified_library=Path(classified_value),
            categories=tuple(categories),
            core_priority=tuple(str(v) for v in raw.get("core_priority", [])),
            raw=raw,
            review_buckets=tuple(review_buckets),
        )

    def ensure_classifiable(self, *, allow_draft: bool) -> None:
        if self.status != "final" and not allow_draft:
            raise TaxonomyError(
                f"当前分类规则为 {self.version}（{self.status}）。"
                "试运行草案必须显式添加 --allow-draft。"
            )
        empty = [c.name for c in self.categories if not c.second_level]
        if empty and self.status == "final":
            raise TaxonomyError("以下一级分类尚无可用二级目录：" + "、".join(empty))
        if not any(c.second_level for c in self.categories):
            raise TaxonomyError("当前分类规则没有任何可用二级目录")

    def prompt_json(self) -> str:
        payload = {
            "taxonomy_version": self.version,
            "core_priority": list(self.core_priority),
            "categories": [
                {
                    "id": c.id,
                    "name": c.name,
                    "kind": c.kind,
                    "purpose": c.purpose,
                    "second_level": list(c.second_level),
                    "uses_unconfirmed_candidates": c.uses_candidates,
                    "boundaries": c.boundaries,
                }
                for c in self.categories
                if c.second_level
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def category_by_id(self, category_id: str) -> Category | None:
        return next((item for item in self.categories if item.id == category_id), None)

    def review_bucket_by_id(self, bucket_id: str) -> ReviewBucket | None:
        return next((item for item in self.review_buckets if item.id == bucket_id), None)
