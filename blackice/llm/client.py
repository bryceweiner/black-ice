"""LM Studio client. OpenAI-compatible /v1, plus vision and tool calling."""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from ..config import get_settings

log = logging.getLogger("blackice.llm")

MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
}


def image_part(path: str | Path, mime: str | None = None) -> dict[str, Any]:
    """Encode an image file as an OpenAI-style content part."""
    p = Path(path)
    mime = mime or MIME_BY_SUFFIX.get(p.suffix.lower(), "image/jpeg")
    data = base64.b64encode(p.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def message_text(message: dict[str, Any]) -> str:
    """The assistant's text.

    Reasoning-style models (the abliterated Qwen3.8 build among them) can put
    their whole answer in `reasoning_content` and leave `content` empty, so an
    empty content field is not the same as an empty answer.
    """
    return (message.get("content") or message.get("reasoning_content") or "").strip()


def extract_json(message: dict[str, Any]) -> dict[str, Any]:
    """Parse a structured reply, tolerating reasoning placement and prose."""
    for source in (message.get("content"), message.get("reasoning_content")):
        if not source:
            continue
        try:
            return json.loads(source)
        except ValueError:
            match = re.search(r"\{.*\}", source, re.S)
            if match:
                try:
                    return json.loads(match.group())
                except ValueError:
                    continue
    return {}


def json_schema_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Structured-output constraint. LM Studio accepts 'json_schema' or 'text';
    it rejects OpenAI's older 'json_object'."""
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


def user_message(text: str, images: list[str | Path] | None = None) -> dict[str, Any]:
    if not images:
        return {"role": "user", "content": text}
    return {
        "role": "user",
        "content": [text_part(text), *(image_part(i) for i in images)],
    }


class LMStudioClient:
    def __init__(self, base_url: str | None = None, timeout: float = 300.0) -> None:
        s = get_settings()
        self.base_url = (base_url or s.lmstudio_base_url).rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def models(self) -> list[str]:
        r = await self._client.get(f"{self.base_url}/models")
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model or get_settings().model_primary,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
        if max_tokens:
            body["max_tokens"] = max_tokens
        if response_format:
            body["response_format"] = response_format

        r = await self._client.post(f"{self.base_url}/chat/completions", json=body)
        if r.status_code >= 400:
            log.error("LM Studio %s: %s", r.status_code, r.text[:500])
        r.raise_for_status()
        return r.json()["choices"][0]["message"]


client = LMStudioClient()
