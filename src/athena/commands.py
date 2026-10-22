"""The commands you can send Athena, such as /session and /recall, as Telegram and the dashboard's chat both have
them: what each does, and what it says back, in Markdown. Where you send them from adds what's its own, such as
Telegram's buttons. /stop and /retry depend on what's being answered there, so each front end has its own."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import openai

from athena.config import THINKING, ConfigError
from athena.history import Busy
from athena.memory import Found
from athena.status import describe_error
from athena.tools import flags

if TYPE_CHECKING:
    from athena.app import Services

log = logging.getLogger(__name__)

COMMANDS = [
    ("session", "Start a new session: a new conversation (memories are kept)"),
    ("compact", "Summarise our conversation so far, keeping the last few messages, to make room"),
    ("stop", "Stop the answer I'm working on"),
    ("retry", "Answer your last message again"),
    ("think", "Think before answering: /think on, short or off"),
    ("tasks", "What I do by myself: run, pause or delete it"),
    ("remember", "Save a fact: /remember <text>"),
    ("recall", "Search memory: /recall <query>"),
    ("forget", "Delete a memory: /forget <id>"),
    ("tools", "List available tools and MCP servers"),
    ("reload", "Reconnect to MCP servers"),
    ("status", "Check the model, memory and tools"),
]
THINKING_SAYS = {
    "on": "I think things through before answering, for as long as it takes.",
    "short": "I think briefly before answering, for up to {tokens} tokens.",
    "off": "I answer straight away, without thinking first.",
}


def parse(line: str) -> tuple[str, str] | None:
    """A command and what follows it, as ("recall", "boiler"), or None if ``line`` isn't a command. Telegram's
    form, /recall@my_bot, is understood too."""
    if not line.startswith("/") or len(line) < 2:
        return None
    word, _, rest = line[1:].partition(" ")
    name = word.split("@", 1)[0].lower()
    return (name, rest.strip()) if name else None


async def run(services: Services, name: str, rest: str, *, source: str, chat_id: str | None = None) -> str | None:
    """Run a command, and what it says: None if it isn't one of these (/stop and /retry are each front end's own).
    ``chat_id`` is the chat /compact compacts, the current session's if it's not given."""
    if name in ("help", "start"):
        return help_text(services)
    if name in ("session", "reset"):
        return new_session(services)
    if name == "compact":
        return await compact(services, chat_id or services.history.current_chat)
    if name == "think":
        return think(services, rest)
    if name == "tasks":
        return tasks(services)[0]
    if name == "remember":
        return await remember(services, rest, source)
    if name == "recall":
        return await recall(services, rest)
    if name == "forget":
        return forget(services, rest)
    if name == "tools":
        return tools(services)
    if name == "reload":
        try:
            await services.reload(reconnect=True)  # with any changes to config.toml's tools since it started
        except ConfigError as exc:
            return f"I kept the tools as they were. {exc}"
        return tools(services)
    if name == "status":
        return await status(services)
    return None


def failure(services: Services, exc: Exception, photos: bool = False) -> str:
    """Why your message didn't get an answer, for you."""
    if isinstance(exc, openai.APITimeoutError):
        return f"The model took longer than {services.config.llm.timeout_seconds:.0f}s to answer, so I gave up."
    if isinstance(exc, openai.APIConnectionError):
        return f"I can't reach the model server at {services.config.llm.base_url}. Is the Mac awake and oMLX running?"
    if isinstance(exc, openai.APIStatusError):
        said = f"The model server returned an error ({exc.status_code}): {exc.message}"
        return said + ("\nMaybe the model can't take photos: `athena doctor` checks." if photos else "")
    log.error("Couldn't answer", exc_info=exc)
    return f"Something went wrong: {type(exc).__name__}: {exc}"


def help_text(services: Services) -> str:
    lines = [f"Hi! I'm {services.config.agent.assistant_name}. Just send me a message.", ""]
    return "\n".join(lines + [f"/{c} - {d}" for c, d in COMMANDS])


def new_session(services: Services) -> str:
    try:
        session = services.new_session()
    except Busy as exc:
        return str(exc)
    return (
        f"Started session {session.id}. I've cleared our conversation from my context. "
        "Memories are kept, and I can still search what we said before."
    )


async def compact(services: Services, chat_id: str) -> str:
    try:
        summarised = await services.agent.compact(chat_id)
    except Exception as exc:
        log.warning("Couldn't compact the conversation: %s", exc)
        return f"I couldn't compact our conversation: {describe_error(exc)}. It's as it was."
    if summarised:
        return f"Done: I've summarised the {summarised} messages before the last few, and kept them as they were."
    return "There's nothing to compact yet: I see only the last few messages as they are."


def think(services: Services, choice: str) -> str:
    """Set how much the model thinks, if ``choice`` says, and say how it is now."""
    choice = choice.strip().lower()
    if not choice:
        return thinking_says(services)
    if choice not in THINKING:
        return "Usage: /think on, /think short or /think off"
    try:
        saved = services.set_thinking(choice)
    except Exception as exc:  # such as config.toml being invalid
        log.warning("Couldn't change thinking: %s", exc)
        return f"I couldn't change that: {exc}"
    return thinking_says(services) + ("" if saved else " Only until I restart: there's no config.toml to save it in.")


def thinking_says(services: Services) -> str:
    llm = services.config.llm
    return f"Thinking is {llm.thinking_now}: " + THINKING_SAYS[llm.thinking_now].format(
        tokens=f"{llm.short_thinking_tokens:,}"
    )


def tasks(services: Services) -> tuple[str, list[dict[str, Any]]]:
    """What the tasks are, and each task as the dashboard describes it, for buttons to act on them."""
    scheduler = services.tasks
    if scheduler is None:
        return "Tasks are switched off ([tasks] enabled = false in config.toml).", []
    listed = [scheduler.describe(t) for t in scheduler.store.all()]
    if not listed:
        return (
            "I've no tasks. Ask me to do something regularly, or in the background, such as "
            "“every weekday at 7:30 send me a briefing”. You can also add them on the dashboard.",
            [],
        )
    lines = ["Tasks I do by myself:"]
    for t in listed:
        mark = {"active": "▶️", "paused": "⏸", "done": "✓", "running": "⚙️"}.get(t["status"], "")
        nxt = ""
        if t["status"] == "running":
            nxt = " · working on it now"
        elif t["waiting_for_you"]:
            nxt = " · due, waiting until you've finished chatting"
        elif t["next_run"]:
            when = datetime.fromtimestamp(t["next_run"], scheduler.tz)
            nxt = f" · next {when:%a %-d %b %H:%M}"
        last = f" · last: {t['last_outcome']}" if t["last_outcome"] else ""
        where = services.history.title(t["session"]) if t["session"] is not None else ""
        within = f" (in “{where}”)" if where and where != t["title"] else ""
        lines.append(f"{mark} #{t['id']} {t['title']}{within}: {t['when']}{nxt}{last}")
    return "\n".join(lines), listed


async def remember(services: Services, text: str, source: str) -> str:
    if not text.strip():
        return "Usage: /remember <fact>"
    memory_id, created = await services.memory.remember(text.strip(), source=source)
    return f"Saved as memory #{memory_id}." if created else f"I already knew that (memory #{memory_id})."


async def recall(services: Services, query: str) -> str:
    memory = services.memory
    query = query.strip()
    if query:
        found = await memory.find(query, 8)
        header = f"Memories matching '{query}':"
    else:
        found = [Found("fact", 1.0, h.text, id=h.id) for h in memory.store.recent(10)]
        header = "Most recent memories:"
    if not found:
        return "Nothing found."
    lines = [header]
    for f in found:
        text = f.text if len(f.text) < 300 else f.text[:300] + "..."
        score = f" ({1 - f.distance:.2f})" if query and f.distance is not None else ""
        # A document goes by its path: its passages come and go with it when you index, so /forget isn't for them.
        label = f"📄 {memory.show_path(f.path)}" if f.kind == "document" and f.path else f"#{f.id}"
        lines.append(f"{label}{score} {text}")
    return "\n".join(lines)


def forget(services: Services, which: str) -> str:
    try:
        memory_id = int((which.split() or [""])[0].lstrip("#"))
    except ValueError:
        return "Usage: /forget <id> (find ids with /recall)"
    if services.memory.forget(memory_id):
        return f"Forgot memory #{memory_id}."
    return f"There's no memory #{memory_id}."


def tools(services: Services) -> str:
    lines = ["Tools:"]
    for tool in services.tools.all():
        lines.append(f"- {tool.name} [{tool.source}]{flags(tool, services.tools)}")
    lines += ["", "MCP servers:"]
    for st in services.mcp.status() or []:
        lines.append(f"- {st.name}: " + (f"connected, {st.tools} tools" if st.connected else f"down - {st.error}"))
    if not services.mcp.status():
        lines.append("- none configured")
    return "\n".join(lines)


async def status(services: Services) -> str:
    cfg = services.config
    lines = [f"Model: {cfg.llm.model} (thinking {cfg.llm.thinking_now})", f"Endpoint: {cfg.llm.base_url}"]
    try:
        models = await asyncio.wait_for(services.llm.list_models(), 10)
        ok = cfg.llm.model in models
        lines.append("Model server: reachable" + ("" if ok else f" (but '{cfg.llm.model}' isn't listed)"))
    except Exception as exc:
        lines.append(f"Model server: unreachable ({type(exc).__name__})")
    counts = services.memory.store.count()
    lines.append(f"Memory: {counts.get('fact', 0)} facts, {counts.get('document', 0)} document chunks")
    connected = sum(1 for st in services.mcp.status() if st.connected)
    lines.append(f"Tools: {len(services.tools.all())} ({connected}/{len(services.mcp.status())} MCP servers up)")
    return "\n".join(lines)
