"""The agent loop: build the prompt, call the model, run tools, repeat until it answers."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pi_assistant.config import AgentConfig
from pi_assistant.history import ConversationStore
from pi_assistant.llm import LLMClient, LLMReply, ToolCall
from pi_assistant.memory import MemoryHit, MemoryService
from pi_assistant.status import StatusTracker, Task
from pi_assistant.tools import ToolError, ToolRegistry

log = logging.getLogger(__name__)

# Asks the user to approve a tool call: (tool name, arguments, what it will do if the
# tool can say) -> approved?
ConfirmFn = Callable[[str, dict[str, Any], str | None], Awaitable[bool]]
# Notified as each tool runs: (tool name, arguments, result).
ToolEventFn = Callable[[str, dict[str, Any], str], Awaitable[None]]

FALLBACK_PROMPT = (
    "You are {assistant_name}, a helpful personal assistant for {user_name}. "
    "Be concise. Use tools when they help, and never invent their results."
)


def render_system_prompt(template: str | None, cfg: AgentConfig) -> str:
    """The system prompt, with {assistant_name}, {user_name} and {timezone} filled in."""
    prompt = template or FALLBACK_PROMPT
    for key, value in {
        "assistant_name": cfg.assistant_name,
        "user_name": cfg.user_name,
        "timezone": cfg.timezone,
    }.items():
        prompt = prompt.replace("{" + key + "}", value)
    return prompt.strip()


@dataclass
class AgentResult:
    text: str
    tools_used: list[str] = field(default_factory=list)
    model_calls: int = 0
    elapsed: float = 0.0


class Agent:
    def __init__(
        self,
        cfg: AgentConfig,
        llm: LLMClient,
        tools: ToolRegistry,
        history: ConversationStore,
        memory: MemoryService | None = None,
        system_prompt_template: str | None = None,
        auto_recall: bool = True,
        status: StatusTracker | None = None,
    ):
        self.cfg = cfg
        self.llm = llm
        self.tools = tools
        self.history = history
        self.memory = memory
        self.status = status or StatusTracker()
        self.auto_recall = auto_recall and memory is not None
        # Rendered once and never changed, so the model server can cache it.
        self.system_prompt = render_system_prompt(system_prompt_template, cfg)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # -- prompt construction ----------------------------------------------------------

    def _now(self) -> datetime:
        try:
            return datetime.now(ZoneInfo(self.cfg.timezone))
        except Exception:  # unknown timezone name
            return datetime.now().astimezone()

    def build_context(self, memories: list[MemoryHit], note: str | None = None) -> str:
        now = self._now()
        lines = ["<context>", f"Current time: {now:%A %-d %B %Y, %H:%M} ({self.cfg.timezone})"]
        if note:
            lines.append(note)
        if memories:
            lines.append("Possibly relevant memories:")
            for m in memories:
                label = f"#{m.id}, saved {m.created_at[:10]}" if m.kind == "fact" else f"#{m.id}, from {m.source}"
                lines.append(f"- [{label}] {m.text}")
        lines.append("</context>")
        return "\n".join(lines)

    async def _recall(self, text: str) -> list[MemoryHit]:
        if not self.auto_recall or not self.memory:
            return []
        try:
            return await self.memory.recall(text)
        except Exception as exc:  # embeddings server down shouldn't stop the conversation
            log.warning("Memory recall failed: %s", exc)
            return []

    # -- tools --------------------------------------------------------------------------

    async def _run_tool(self, call: ToolCall, confirm: ConfirmFn | None, task: Task) -> str:
        tool = self.tools.get(call.name)
        if tool is None:
            return f"Error: there is no tool called '{call.name}'."
        if call.arguments is None:
            return f"Error: the arguments were not valid JSON: {call.raw_arguments[:200]}"
        if tool.needs_confirmation:
            if confirm is None:
                return "Error: this action needs the user's approval, which can't be requested here."
            summary = None
            if tool.preview:
                try:
                    summary = await asyncio.wait_for(tool.preview(call.arguments), self.cfg.tool_timeout_seconds)
                except Exception as exc:  # nothing has been done, and the user hasn't been asked
                    return self._tool_error(call.name, exc)
            try:
                with task.approval(tool.name):
                    approved = await confirm(tool.name, call.arguments, summary)
            except Exception as exc:
                log.warning("Confirmation request failed: %s", exc)
                approved = False
            if not approved:
                return "The user declined this action. Don't retry it unless they ask."
        task.using(tool.name)
        try:
            result = await asyncio.wait_for(tool.handler(call.arguments), self.cfg.tool_timeout_seconds)
        except Exception as exc:
            return self._tool_error(call.name, exc)
        result = result if isinstance(result, str) else json.dumps(result, default=str)
        limit = self.cfg.max_tool_result_chars
        if len(result) > limit:
            result = result[:limit] + f"\n[...truncated {len(result) - limit} characters]"
        return result or "(no output)"

    def _tool_error(self, name: str, exc: Exception) -> str:
        """What the model is told when a tool fails. Call it from the `except` block."""
        if isinstance(exc, TimeoutError):
            return f"Error: the tool timed out after {self.cfg.tool_timeout_seconds:.0f}s."
        if isinstance(exc, ToolError):
            return f"Error: {exc}"
        log.exception("Tool %s failed", name)
        return f"Error: {type(exc).__name__}: {exc}"

    # -- main entry point -------------------------------------------------------------

    async def warm_up(self, chat_id: str) -> LLMReply | None:
        """Have the model server read the start of this chat's next request: the system prompt,
        the tools and the conversation so far. It caches what it reads, so the next message
        only has to be read itself. Skipped while a message is being answered, which does the same.
        """
        lock = self._locks[chat_id]
        if lock.locked():
            return None
        async with lock:
            messages = [
                {"role": "system", "content": self.system_prompt},
                *self.history.load(chat_id),
                {"role": "user", "content": "Hi"},  # the next message goes here; only what's before it is reused
            ]
            return await self.llm.chat(messages, self.tools.schemas() or None, max_tokens=1)

    async def respond(
        self,
        chat_id: str,
        text: str,
        *,
        confirm: ConfirmFn | None = None,
        on_tool: ToolEventFn | None = None,
        note: str | None = None,
    ) -> AgentResult:
        """Answer ``text``. ``note`` goes in the context block for this message only, e.g. how it was sent."""
        async with self._locks[chat_id]:
            task = self.status.begin(text)
            try:
                result = await self._respond(chat_id, text, task, confirm, on_tool, note)
            except BaseException as exc:  # including cancellation, so the board never sticks on "working"
                task.finish(exc)
                raise
            task.finish()
            return result

    async def _respond(
        self,
        chat_id: str,
        text: str,
        task: Task,
        confirm: ConfirmFn | None,
        on_tool: ToolEventFn | None,
        note: str | None,
    ) -> AgentResult:
        started = time.monotonic()
        memories = await self._recall(text)
        # Dynamic context goes in the newest user message, not the system prompt, so
        # everything before it is byte-identical to the previous request (prompt cache hit).
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            *self.history.load(chat_id),
            {"role": "user", "content": f"{self.build_context(memories, note)}\n\n{text}"},
        ]
        schemas = self.tools.schemas() or None
        result = AgentResult(text="")

        for _ in range(self.cfg.max_tool_rounds):
            task.thinking()
            reply = await self.llm.chat(messages, schemas)
            result.model_calls += 1
            if not reply.tool_calls:
                result.text = reply.content
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or "",
                    "tool_calls": [c.as_message_part() for c in reply.tool_calls],
                }
            )
            for call in reply.tool_calls:
                output = await self._run_tool(call, confirm, task)
                result.tools_used.append(call.name)
                log.info("tool %s(%s) -> %s", call.name, call.raw_arguments[:200], output[:200].replace("\n", " "))
                if on_tool:
                    await on_tool(call.name, call.arguments or {}, output)
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": output})
        else:
            # Out of tool rounds: ask for a final answer with tools switched off.
            task.thinking()
            reply = await self.llm.chat(messages, None)
            result.model_calls += 1
            result.text = reply.content

        if not result.text.strip():
            result.text = "Sorry, I didn't manage to produce an answer. Could you rephrase that?"
        else:
            self.history.append_exchange(chat_id, text, result.text)
        result.elapsed = time.monotonic() - started
        return result
