"""The agent loop: build the prompt, call the model, run tools, repeat until it answers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from athena.compaction import KEEP_MESSAGES, MIN_MESSAGES, summarise
from athena.config import AgentConfig
from athena.history import ConversationStore
from athena.llm import LLMClient, LLMReply, ToolCall
from athena.memory import MemoryHit, MemoryService
from athena.photos import Photo, label, with_photos
from athena.privacy import Privacy, refusal, warning
from athena.stats import DECLINED, FAILED, RAN, QueryRecord, UsageStats
from athena.status import StatusTracker, Task, describe_error
from athena.timeline import MESSAGE, Timeline, ToolEvent, steps
from athena.tool_search import USE_TOOL, unwrap
from athena.tools import NEVER_UNASKED, ToolError, ToolRegistry, Turn, begin_turn

log = logging.getLogger(__name__)

# Asks the user to approve a tool call: (tool name, arguments, what it will do if the
# tool can say) -> approved?
ConfirmFn = Callable[[str, dict[str, Any], str | None], Awaitable[bool]]
# After each tool call: what it was given and gave back, what happened, and whether it needed approval.
ToolEventFn = Callable[[ToolEvent], Awaitable[None]]

WINDOW_SECONDS = 600.0  # how long the model's context window, as its server says, is taken to stay the same

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
class Background:
    """How Athena works on a task by itself (tasks.py), rather than answering you."""

    label: str  # what the status board shows, such as "Task: Morning briefing"
    # Takes each step, a call to the model: it waits while you're chatting, and stops the call if you
    # start, to take it again after. It raises to stop the task.
    step: Callable[[Task, Callable[[], Awaitable[LLMReply]]], Awaitable[LLMReply]]
    thinking: str | None = None  # instead of [llm] thinking
    max_rounds: int | None = None  # instead of [agent] max_tool_rounds
    # What it's doing, as what it saves by itself says it came from, such as "your task “Briefing”".
    source: str = ""
    only_tools: frozenset[str] | None = None  # only these tools may run (they're all offered, for the cache)
    review: bool = False  # looking back at how a skill went (tasks.py), rather than doing something


@dataclass
class AgentResult:
    text: str
    tools_used: list[str] = field(default_factory=list)
    model_calls: int = 0
    elapsed: float = 0.0
    saved: bool = False  # the exchange went into the history
    # Tokens of the context window the conversation takes now: what its first request read, and the answer.
    context_tokens: int = 0
    notes: dict[str, list[str]] = field(default_factory=dict)  # what tools noted (tools.Turn)
    # Everything the model saw and said, kept when it read a skill, to look back at how that went.
    transcript: list[dict[str, Any]] | None = None


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
        stats: UsageStats | None = None,
    ):
        self.cfg = cfg
        self.llm = llm
        self.tools = tools
        self.history = history
        self.memory = memory
        self.status = status or StatusTracker()
        self.stats = stats
        self.auto_recall = auto_recall and memory is not None
        # Rendered once and never changed, so the model server can cache it.
        self.system_prompt = render_system_prompt(system_prompt_template, cfg)
        # Added to the end of it, such as the list of skills: it changes rarely, so the cache mostly holds.
        self.prompt_extra: Callable[[], str] | None = None
        # Called after each exchange is saved to the history, once the chat is free again.
        self.on_saved: Callable[[], None] | None = None
        # Called when a chat's been compacted, so what the model sees of it has changed.
        self.on_compacted: Callable[[], None] | None = None
        # Called with each answer, and the background work it was for (None: you), once it's finished.
        self.on_answered: Callable[[AgentResult, Background | None], None] | None = None
        # Looks through what each tool would send for personal details and secrets (privacy.py).
        self.privacy: Privacy | None = None
        # Where your messages, and what was done for them, are noted for the dashboard (timeline.py).
        self.timeline: Timeline | None = None
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._compact_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._compacting: dict[str, asyncio.Task[None]] = {}  # compacting after an answer, by chat
        self._window: tuple[LLMClient, float, int] | None = None  # (for which client, when, the context window)
        # While paused, new messages wait; `busy` counts the ones being answered.
        self._open = asyncio.Event()
        self._open.set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._busy = 0
        self._pausing = asyncio.Lock()
        # You, rather than a task: how many of your messages are being answered, and when one last was.
        self.answering_you = 0
        self.last_answered_you = float("-inf")  # time.monotonic()
        self.chatting = asyncio.Event()  # set while one of your messages is being answered

    # -- pausing --------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy > 0

    @contextlib.asynccontextmanager
    async def paused(self) -> AsyncIterator[None]:
        """Wait for messages being answered to finish, and hold new ones until this ends.

        For changing the tools: a message is always answered with the same set throughout.
        """
        async with self._pausing:
            self._open.clear()
            try:
                await self._idle.wait()
                yield
            finally:
                self._open.set()

    @contextlib.asynccontextmanager
    async def _working(self) -> AsyncIterator[None]:
        while not self._open.is_set():
            await self._open.wait()
        self._busy += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._busy -= 1
            if not self._busy:
                self._idle.set()

    # -- prompt construction ----------------------------------------------------------

    def prompt(self) -> str:
        """The system prompt, with what's added to it now."""
        extra = self.prompt_extra() if self.prompt_extra else ""
        return f"{self.system_prompt}\n\n{extra}" if extra else self.system_prompt

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

    async def _run_tool(
        self,
        call: ToolCall,
        confirm: ConfirmFn | None,
        task: Task,
        only: frozenset[str] | None = None,
        allowed: frozenset[str] = frozenset(),
    ) -> ToolEvent:
        """Run a tool call. A tool that asks first runs without asking if it's in ``allowed``, unless it always asks
        (NEVER_UNASKED). Returns what the model is told, what happened (RAN, FAILED or DECLINED), whether it was
        asked about or allowed, and what the privacy check found."""
        args = call.arguments or {}
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolEvent(call.name, args, f"Error: there is no tool called '{call.name}'.", FAILED)
        if only is not None and call.name not in only:
            return ToolEvent(call.name, args, f"Error: only {', '.join(sorted(only))} can be used now.", FAILED)
        if call.arguments is None:
            return ToolEvent(
                call.name, args, f"Error: the arguments were not valid JSON: {call.raw_arguments[:200]}", FAILED
            )
        verdict = self.privacy.check(tool.name, call.arguments, asks=tool.needs_confirmation) if self.privacy else None
        flagged = verdict.describe() if verdict and verdict.findings else ""
        if verdict and verdict.refused:
            return ToolEvent(tool.name, args, f"Error: {refusal(verdict)}", FAILED, flagged=flagged)
        approval = ""
        unasked = tool.name in allowed and tool.name not in NEVER_UNASKED
        if tool.needs_confirmation:
            if confirm is None and not unasked:
                said = "Error: this action needs the user's approval, which can't be requested here."
                return ToolEvent(tool.name, args, said, FAILED, flagged=flagged)
            summary = None
            if tool.preview:
                try:
                    summary = await asyncio.wait_for(tool.preview(call.arguments), self.cfg.tool_timeout_seconds)
                except Exception as exc:  # nothing has been done, and the user hasn't been asked
                    return ToolEvent(tool.name, args, self._tool_error(call.name, exc), FAILED, flagged=flagged)
            if verdict and verdict.findings:
                summary = warning(verdict) + (f"\n{summary}" if summary else "")
            if unasked:
                approval = "allowed"
                log.info("%s ran without asking, as it may here", tool.name)
            else:
                approval = "you"
                try:
                    with task.approval(tool.name):
                        approved = await confirm(tool.name, call.arguments, summary)  # type: ignore[misc]
                except Exception as exc:
                    log.warning("Confirmation request failed: %s", exc)
                    approved = False
                if not approved:
                    said = "The user declined this action. Don't retry it unless they ask."
                    return ToolEvent(tool.name, args, said, DECLINED, approval, flagged)
        task.using(tool.name)
        try:
            result = await asyncio.wait_for(tool.handler(call.arguments), self.cfg.tool_timeout_seconds)
        except Exception as exc:
            return ToolEvent(tool.name, args, self._tool_error(call.name, exc), FAILED, approval, flagged)
        result = result if isinstance(result, str) else json.dumps(result, default=str)
        limit = self.cfg.max_tool_result_chars
        if len(result) > limit:
            result = result[:limit] + f"\n[...truncated {len(result) - limit} characters]"
        # An MCP server's own errors come back as text starting "Error:" (see mcp_manager.result_to_text).
        outcome = FAILED if result.startswith("Error:") else RAN
        return ToolEvent(tool.name, args, result or "(no output)", outcome, approval, flagged)

    def _tool_error(self, name: str, exc: Exception) -> str:
        """What the model is told when a tool fails. Call it from the `except` block."""
        if isinstance(exc, TimeoutError):
            return f"Error: the tool timed out after {self.cfg.tool_timeout_seconds:.0f}s."
        if isinstance(exc, ToolError):
            return f"Error: {exc}"
        log.exception("Tool %s failed", name)
        return f"Error: {type(exc).__name__}: {exc}"

    # -- compacting --------------------------------------------------------------------

    async def compact(self, chat_id: str, keep: int = KEEP_MESSAGES) -> int:
        """Summarise the older part of a chat, keeping about the last ``keep`` messages as they were (compaction.py).
        Returns how many messages were summarised: 0 if there weren't any, or the chat moved on meanwhile."""
        async with self._compact_locks[chat_id]:
            older = await asyncio.to_thread(self.history.older, chat_id, keep)
            if older is None:
                return 0
            task = self.status.begin("Compacting our conversation")
            try:
                task.thinking()
                summary = await summarise(self.llm, self.cfg, older)
            except BaseException as exc:
                task.finish(exc)
                raise
            task.finish()
            if not await asyncio.to_thread(self.history.compact, chat_id, older, summary):
                log.info("Didn't compact: the conversation was cleared or reset meanwhile")
                return 0
        log.info("Compacted %d messages into a summary of %d characters", len(older.messages), len(summary))
        if self.on_compacted:
            self.on_compacted()
        return len(older.messages)

    def _compact_soon(self, chat_id: str, tokens: int) -> None:
        """After an answer, compact the chat in the background if it's grown long, or filled the context window."""
        running = self._compacting.get(chat_id)
        if running and not running.done():
            return
        self._compacting[chat_id] = asyncio.create_task(self._compact_if_full(chat_id, tokens), name="compact")

    async def _compact_if_full(self, chat_id: str, tokens: int) -> None:
        length = await asyncio.to_thread(self.history.length, chat_id)
        full = length >= self.cfg.max_history_messages
        if not full and self.cfg.compact_at and tokens and length >= MIN_MESSAGES:
            window = await self.context_window()
            full = bool(window) and tokens >= self.cfg.compact_at * window
        if not full:
            return
        try:
            await self.compact(chat_id)
        except Exception as exc:  # the model may be down: the history is trimmed instead, if it grows much longer
            log.warning("Couldn't compact the conversation: %s", describe_error(exc))

    async def context_window(self) -> int | None:
        """The most the model can read at once, in tokens, if it's known: from [llm] context_window, or its server."""
        now = time.monotonic()
        if self._window and self._window[0] is self.llm and now - self._window[1] < WINDOW_SECONDS:
            return self._window[2]
        try:
            window = await asyncio.wait_for(self.llm.context_window(), 10)
        except Exception as exc:
            log.info("Couldn't ask the model server for its context window: %s", describe_error(exc))
            return None
        if window:
            self._window = (self.llm, now, window)
        return window

    async def close(self) -> None:
        """Stop compacting, if it is."""
        for task in self._compacting.values():
            task.cancel()
        await asyncio.gather(*self._compacting.values(), return_exceptions=True)

    # -- main entry point -------------------------------------------------------------

    async def warm_up(self, chat_id: str) -> LLMReply | None:
        """Have the model server read the start of this chat's next request: the system prompt,
        the tools and the conversation so far. It caches what it reads, so the next message
        only has to be read itself. Skipped while a message is being answered, which does the same.
        """
        lock = self._locks[chat_id]
        if lock.locked():
            return None
        async with self._working(), lock:
            messages = [
                {"role": "system", "content": self.prompt()},
                *self.history.load(chat_id),
                {"role": "user", "content": "Hi"},  # the next message goes here; only what's before it is reused
            ]
            session = self.history.session.id
            reply = await self.llm.chat(messages, self.tools.schemas() or None, max_tokens=1)
            if self.stats and reply.prompt_tokens:  # how much the conversation takes up, for the dashboard
                self.stats.note_context(session, reply.prompt_tokens)
            return reply

    async def respond(
        self,
        chat_id: str,
        text: str,
        *,
        confirm: ConfirmFn | None = None,
        allowed: Collection[str] = (),
        on_tool: ToolEventFn | None = None,
        note: str | None = None,
        channel: str = "",
        background: Background | None = None,
        photos: list[Photo] | None = None,
    ) -> AgentResult:
        """Answer ``text``, and ``photos`` if it came with any. ``note`` goes in the context block for this message
        only, e.g. how it was sent. Tools that ask first run without asking if they're in ``allowed``, unless they
        always ask.

        ``channel`` says where it came from, such as "Siri", for the usage statistics. ``background``
        is for a task Athena works on by itself: it doesn't hold up changes to the tools, which it
        could for many minutes, and doesn't count as you chatting.
        """
        async with contextlib.AsyncExitStack() as stack:
            if background is None:
                await stack.enter_async_context(self._working())
                self.answering_you += 1
                self.chatting.set()
                stack.callback(self._answered_you)
            await stack.enter_async_context(self._locks[chat_id])
            shown = text or (label(len(photos)) if photos else "")  # a photo without a caption
            task = self.status.begin(background.label if background else shown)
            record = QueryRecord(session=self.history.session.id, channel=channel or self.status.channel)
            # Your messages go on the timeline here; what Athena does by itself, where it's done (tasks/).
            noting = background is None and self.timeline is not None
            taken, collect = steps()
            if noting:
                on_tool = _both(collect, on_tool)
            result: AgentResult | None = None
            try:
                result = await self._respond(
                    chat_id, text, task, confirm, frozenset(allowed), on_tool, note, record, background, photos
                )
            except BaseException as exc:  # including cancellation, so the board never sticks on "working"
                task.finish(exc)
                record.error = describe_error(exc)
                raise
            finally:
                self._record(record)
                if noting:
                    self._note(shown, record, result, taken)
            task.finish()
        if result.saved and self.on_saved:
            self.on_saved()
        if result.saved:
            self._compact_soon(chat_id, result.context_tokens)
        if self.on_answered:
            self.on_answered(result, background)
        return result

    async def follow_up(self, transcript: list[dict[str, Any]], text: str, background: Background) -> AgentResult:
        """Ask the model ``text`` after what it did in ``transcript`` (from AgentResult.transcript), working by
        itself: to look back at how a skill went. Nothing's kept in the history."""
        task = self.status.begin(background.label)
        record = QueryRecord(session=self.history.session.id, channel="Skills")
        messages = [*transcript, {"role": "user", "content": text}]
        try:
            result = await self._work(messages, task, None, None, record, background)
        except BaseException as exc:
            task.finish(exc)
            record.error = describe_error(exc)
            raise
        finally:
            self._record(record)
        task.finish()
        return result

    def _answered_you(self) -> None:
        self.answering_you -= 1
        self.last_answered_you = time.monotonic()
        if not self.answering_you:
            self.chatting.clear()

    def _record(self, record: QueryRecord) -> None:
        record.seconds = time.time() - record.started  # for the timeline too
        if not self.stats:
            return
        try:
            self.stats.record(record)
        except Exception:  # statistics mustn't get in the way of an answer
            log.exception("Couldn't record usage statistics")

    def _note(self, text: str, record: QueryRecord, result: AgentResult | None, taken: list[dict[str, str]]) -> None:
        """Put your message, and how it was answered, on the timeline."""
        assert self.timeline is not None
        outcome = "answered"
        if record.error:
            outcome = "stopped" if record.error == "Stopped" else f"failed: {record.error}"
        try:
            self.timeline.add(
                MESSAGE,
                text,
                outcome,
                said=result.text if result and not record.error else "",
                channel=record.channel,
                seconds=record.seconds,
                steps=taken,
            )
        except Exception:  # like the statistics, it mustn't get in the way of an answer
            log.exception("Couldn't note the message on the timeline")

    async def _chat(
        self, record: QueryRecord, messages: list[dict[str, Any]], schemas: Any, thinking: str | None = None
    ) -> LLMReply:
        reply = await self.llm.chat(messages, schemas, thinking=thinking)
        record.model_calls += 1
        record.prompt_tokens = max(record.prompt_tokens, reply.prompt_tokens or 0)
        record.completion_tokens += reply.completion_tokens or 0
        if reply.prompt_tokens:
            record.context_tokens = reply.prompt_tokens + (reply.completion_tokens or 0)
        return reply

    async def _respond(
        self,
        chat_id: str,
        text: str,
        task: Task,
        confirm: ConfirmFn | None,
        allowed: frozenset[str],
        on_tool: ToolEventFn | None,
        note: str | None,
        record: QueryRecord,
        background: Background | None = None,
        photos: list[Photo] | None = None,
    ) -> AgentResult:
        memories = await self._recall(text) if text.strip() else []
        if photos and not text.startswith(label(len(photos))):  # as it is when /retry sends it again
            text = f"{label(len(photos))} {text}".rstrip()  # so the history, memory and skills know of them
        # Dynamic context goes in the newest user message, not the system prompt, so
        # everything before it is byte-identical to the previous request (prompt cache hit).
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompt()},
            *self.history.load(chat_id),
            {"role": "user", "content": with_photos(f"{self.build_context(memories, note)}\n\n{text}", photos or [])},
        ]
        result = await self._work(messages, task, confirm, on_tool, record, background, allowed)
        if not result.text.strip():
            result.text = "Sorry, I didn't manage to produce an answer. Could you rephrase that?"
            record.error = "No answer"
        else:
            self.history.append_exchange(
                chat_id, text, result.text, tools=result.tools_used, photos=photos, channel=record.channel
            )
            result.saved = True
        return result

    async def _work(
        self,
        messages: list[dict[str, Any]],
        task: Task,
        confirm: ConfirmFn | None,
        on_tool: ToolEventFn | None,
        record: QueryRecord,
        background: Background | None,
        allowed: frozenset[str] = frozenset(),
    ) -> AgentResult:
        """Call the model, and the tools it asks for, until it answers. Adds to ``messages`` as it goes."""
        started = time.monotonic()
        schemas = self.tools.schemas() or None
        result = AgentResult(text="")
        thinking = background.thinking if background else None
        only = background.only_tools if background else None
        turn = Turn(alone=(background.source or background.label) if background else None)
        end_turn = begin_turn(turn)

        def step(tools: Any) -> Awaitable[LLMReply]:
            call = lambda: self._chat(record, messages, tools, thinking)  # noqa: E731
            return background.step(task, call) if background else call()

        try:
            for _ in range((background and background.max_rounds) or self.cfg.max_tool_rounds):
                task.thinking()
                reply = await step(schemas)
                result.model_calls += 1
                if result.model_calls == 1:  # what the conversation itself takes, before any tool's results
                    result.context_tokens = reply.prompt_tokens or 0
                if not reply.tool_calls:
                    result.text = reply.content
                    result.context_tokens += reply.completion_tokens or 0
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": reply.content or "",
                        "tool_calls": [c.as_message_part() for c in reply.tool_calls],
                    }
                )
                for call in reply.tool_calls:
                    # use_tool runs a tool found when needed: as that tool, for everything but the model's own record.
                    run = unwrap(call) if call.name == USE_TOOL else call
                    if isinstance(run, str):
                        event = ToolEvent(call.name, call.arguments or {}, run, FAILED)
                    else:
                        event = await self._run_tool(run, confirm, task, only, allowed)
                    output = event.output
                    result.tools_used.append(event.tool)
                    record.tools.append((event.tool, event.outcome))
                    # What a tool was given and gave back can be personal, and the journal keeps it after
                    # "forget everything": so only with --verbose.
                    log.info("tool %s: %s", event.tool, event.outcome)
                    given = json.dumps(event.args)[:200]
                    log.debug("tool %s(%s) -> %s", event.tool, given, output[:200].replace("\n", " "))
                    if on_tool:
                        await on_tool(event)
                    messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": output})
            else:
                # Out of tool rounds: ask for a final answer with tools switched off.
                task.thinking()
                reply = await step(None)
                result.model_calls += 1
                result.text = reply.content
        finally:
            end_turn()
        result.notes = turn.notes
        if turn.notes.get("skills") and result.text.strip():
            result.transcript = [*messages, {"role": "assistant", "content": result.text}]
        result.elapsed = time.monotonic() - started
        return result


def _both(first: ToolEventFn, then: ToolEventFn | None) -> ToolEventFn:
    async def on_tool(event: ToolEvent) -> None:
        await first(event)
        if then:
            await then(event)

    return on_tool
