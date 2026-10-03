"""Thin wrapper around an OpenAI-compatible chat endpoint (oMLX, LM Studio, Ollama, vLLM...)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from openai import AsyncOpenAI

from pi_assistant.config import LLMConfig

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass
class ToolCall:
    id: str
    name: str
    # None when the model produced arguments that aren't valid JSON.
    arguments: dict[str, Any] | None
    raw_arguments: str

    def as_message_part(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments},
        }


@dataclass
class LLMReply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    elapsed: float = 0.0

    @property
    def tokens_per_second(self) -> float | None:
        if self.completion_tokens and self.elapsed > 0:
            return self.completion_tokens / self.elapsed
        return None


def strip_reasoning(text: str) -> str:
    """Remove <think>...</think> blocks some models leave in their visible output."""
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text:  # opening tag was swallowed by the chat template
        text = text.split("</think>", 1)[1]
    return text.strip()


def parse_tool_calls(raw_calls: list[Any] | None) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for i, tc in enumerate(raw_calls or []):
        fn = getattr(tc, "function", None)
        if fn is None or not getattr(fn, "name", None):
            continue
        raw = fn.arguments or "{}"
        try:
            args = json.loads(raw) if raw.strip() else {}
            if not isinstance(args, dict):
                args = None
        except json.JSONDecodeError:
            args = None
        calls.append(ToolCall(id=tc.id or f"call_{i}", name=fn.name, arguments=args, raw_arguments=raw))
    return calls


class LLMClient:
    def __init__(self, cfg: LLMConfig, http_client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._client = AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key or "not-needed",
            timeout=cfg.timeout_seconds,
            max_retries=1,
            http_client=http_client,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMReply:
        kwargs: dict[str, Any] = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self.cfg.extra_body:
            kwargs["extra_body"] = self.cfg.extra_body

        started = time.monotonic()
        resp = await self._client.chat.completions.create(**kwargs)
        elapsed = time.monotonic() - started

        if not resp.choices:
            return LLMReply(content="", elapsed=elapsed)
        msg = resp.choices[0].message
        usage = resp.usage
        return LLMReply(
            content=strip_reasoning(msg.content or ""),
            tool_calls=parse_tool_calls(msg.tool_calls),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            elapsed=elapsed,
        )

    async def list_models(self) -> list[str]:
        page = await self._client.models.list()
        return [m.id for m in page.data]

    async def close(self) -> None:
        await self._client.close()
