from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        thinking: str = "disabled",
        timeout_seconds: int = 120,
        max_retries: int = 3,
    ):
        if not api_key:
            raise DeepSeekError("DEEPSEEK_API_KEY 未配置")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.thinking = thinking
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
            "max_tokens": 2000,
        }
        if self.thinking in {"enabled", "disabled"}:
            payload["thinking"] = {"type": self.thinking}

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            request = Request(
                f"{self.base_url}/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    envelope = json.loads(response.read().decode("utf-8"))
                choice = envelope["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise DeepSeekError("模型 JSON 因长度限制被截断")
                content = choice.get("message", {}).get("content", "")
                if not content or not content.strip():
                    raise DeepSeekError("模型返回空 content")
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise DeepSeekError("模型 content 不是 JSON 对象")
                safe_envelope = {
                    "id": envelope.get("id"),
                    "model": envelope.get("model"),
                    "usage": envelope.get("usage"),
                    "finish_reason": choice.get("finish_reason"),
                    "content": parsed,
                }
                return parsed, safe_envelope
            except HTTPError as exc:
                response_text = exc.read().decode("utf-8", errors="replace")[:1000]
                last_error = DeepSeekError(f"DeepSeek HTTP {exc.code}: {response_text}")
                if exc.code not in {408, 429, 500, 502, 503, 504}:
                    break
            except (URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, DeepSeekError) as exc:
                last_error = exc
            if attempt + 1 < self.max_retries:
                time.sleep(min(2**attempt, 8))
        raise DeepSeekError(f"DeepSeek 调用失败：{last_error}")

    def classify(self, *, system_prompt: str, user_prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """兼容旧调用方。新流程使用 complete_json。"""
        return self.complete_json(system_prompt=system_prompt, user_prompt=user_prompt)
