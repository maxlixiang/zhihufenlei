from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """加载简单 KEY=VALUE 文件；已有环境变量优先，且从不打印密钥。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _project_path(value: str, default: str) -> Path:
    path = Path(value or default)
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(frozen=True)
class Settings:
    project_root: Path
    taxonomy_path: Path
    database_path: Path
    export_path: Path
    source_library_override: Path | None
    api_key: str | None
    base_url: str
    model: str
    thinking: str
    timeout_seconds: int
    max_retries: int

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        source = os.getenv("SOURCE_LIBRARY", "").strip()
        return cls(
            project_root=PROJECT_ROOT,
            taxonomy_path=PROJECT_ROOT / "taxonomy.yaml",
            database_path=_project_path(os.getenv("DATABASE_PATH", ""), "data/classification.db"),
            export_path=_project_path(os.getenv("EXPORT_PATH", ""), "exports/classifications.jsonl"),
            source_library_override=Path(source) if source else None,
            api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            thinking=os.getenv("DEEPSEEK_THINKING", "disabled"),
            timeout_seconds=int(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "120")),
            max_retries=int(os.getenv("DEEPSEEK_MAX_RETRIES", "3")),
        )
