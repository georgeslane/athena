"""Builds the assistant's components from the config, and changes them while it runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from dotenv import dotenv_values

from pi_assistant.agent import Agent
from pi_assistant.alerts import HealthMonitor, check_health
from pi_assistant.builtin import Builtin, Unavailable
from pi_assistant.config import THINKING, Config, parse_config
from pi_assistant.dashboard import DashboardServer
from pi_assistant.documents import DocumentIndex
from pi_assistant.history import ConversationStore, Exchange, Session
from pi_assistant.integrations import BUILTINS
from pi_assistant.llm import LLMClient
from pi_assistant.mcp_manager import MCPManager
from pi_assistant.memory import Embedder, MemoryService, MemoryStore
from pi_assistant.privacy import Privacy, TermStore
from pi_assistant.sandbox import Sandbox
from pi_assistant.server_envs import ServerEnvs
from pi_assistant.server_security import ServerSecurity
from pi_assistant.settings import PROTECTED_SECRETS, Settings
from pi_assistant.skills import Skills, SkillStore
from pi_assistant.stats import UsageStats
from pi_assistant.status import StatusTracker
from pi_assistant.status_api import StatusServer
from pi_assistant.tasks import Scheduler, TaskStore
from pi_assistant.tool_approvals import ToolApprovals
from pi_assistant.tools import Tool, ToolRegistry
from pi_assistant.watching import WatchStore

if TYPE_CHECKING:
    from pi_assistant.channel import Channel

log = logging.getLogger(__name__)


@dataclass
class Builtins:
    """The built-in integrations that are running, which come and go with the config (integrations.py)."""

    running: dict[str, Builtin] = field(default_factory=dict)  # by id, such as "news"
    tools: list[Tool] = field(default_factory=list)
    problems: dict[str, str] = field(default_factory=dict)  # why one that's switched on isn't running

    @classmethod
    def build(cls, cfg: Config) -> Builtins:
        b = cls()
        for kind in BUILTINS:
            if not kind.wanted(cfg):
                continue
            try:
                b.running[kind.entry.id] = kind.start(cfg)
            except Unavailable as exc:
                b.problems[kind.entry.id] = str(exc)
                log.warning("%s is switched on, but %s, so it's left out", kind.entry.title, exc)
        for builtin in b.running.values():
            b.tools.extend(builtin.tools())
        return b

    def get(self, id: str) -> Any:
        return self.running.get(id)

    def status(self) -> dict[str, tuple[list[str], str | None]]:
        """For each, the names of its tools in use and what's wrong, for the dashboard."""
        return {
            kind.entry.id: (
                [t.name for t in self.running[kind.entry.id].tools()] if kind.entry.id in self.running else [],
                self.problems.get(kind.entry.id),
            )
            for kind in BUILTINS
        }

    async def close(self) -> None:
        for builtin in self.running.values():
            await builtin.close()


@dataclass
class Services:
    config: Config
    llm: LLMClient
    embedder: Embedder
    memory: MemoryService
    history: ConversationStore
    tools: ToolRegistry
    mcp: MCPManager
    agent: Agent
    status: StatusTracker
    status_api: StatusServer | None
    builtins: Builtins = field(default_factory=Builtins)
    stats: UsageStats | None = None

    def __post_init__(self) -> None:
        # Set to have the model server read the prompt again now, e.g. after the tools change.
        self.rewarm = asyncio.Event()
        # How Athena reaches you when you haven't just asked it something: Telegram sets itself here.
        self.channel: Channel | None = None
        # Tasks Athena does by itself, on a schedule, and tells you about (tasks.py).
        self.tasks: Scheduler | None = None
        if self.config.tasks.enabled:
            self.tasks = Scheduler(
                self.config.tasks,
                TaskStore(self.config.db_path),
                self.agent,
                timezone=self.config.agent.timezone,
                channel=lambda: self.channel,
                tools=self.tools.all,
                on_ran=self.rewarm.set,  # your chat has the report in it now
                watches=WatchStore(self.config.db_path),  # what it keeps an eye on for you (watching.py)
            )
            for tool in self.tasks.tools():
                self.tools.add(tool)
            self.agent.on_answered = self.tasks.after_answer  # it looks back at how skills went
        # How to do things your way, which Athena reads before doing them (skills.py).
        self.skills = Skills(
            SkillStore(self.config.db_path),
            self.history,
            timezone=self.config.agent.timezone,
            chat_name=self._chat_name,
            on_change=self.rewarm.set,  # the list of them is in the prompt
        )
        self.agent.prompt_extra = self.skills.index
        for tool in self.skills.tools():
            self.tools.add(tool)
        # Keeps personal details and secrets from leaving in what tools send (privacy.py).
        self.privacy = Privacy(
            self.config.privacy,
            TermStore(self.config.db_path),
            user_name=lambda: self.config.agent.user_name,
            env_path=self.config.env_path,
        )
        self.agent.privacy = self.privacy
        # Messages you when something Athena needs stops working (alerts.py).
        self.alerts = None
        if self.config.alerts.enabled:
            self.alerts = HealthMonitor(self.config.alerts, lambda: check_health(self), self._alert)
        # After the memory and task tools, in this order, so the tools' order (and the prompt) stays the same.
        self.tools.add_provider(lambda: self.builtins.tools)
        self.tools.add_provider(self.mcp.tools)
        # Your documents, indexed from your Mac when you ask on the dashboard.
        self.documents = DocumentIndex(self.config.documents, self.memory, self.config.log_dir)
        self.dashboard = DashboardServer(self) if self.config.dashboard.enabled else None
        self.applying = False  # changes to the tools are being applied
        self._apply_lock = asyncio.Lock()
        self._indexer: asyncio.Task[None] | None = None
        self._index_again = False
        self._projects: asyncio.Task[None] | None = None  # reads Claude's projects now and then
        # Updates the MCP servers' dependencies that have known vulnerabilities (server_security.py).
        self.security = ServerSecurity(self.config.security_updates, self.mcp, self._tell_security, self._restart)
        self._security: asyncio.Task[None] | None = None
        self._projects_lock = asyncio.Lock()
        self.agent.on_saved = self._conversation_saved

    def approve_tools(self, server: str, tools: dict[str, str]) -> None:
        """Let the model use tools that were held back because they're new or changed: {tool: fingerprint}."""
        self.mcp.approve(server, tools)
        self.rewarm.set()  # the prompt has changed
        log.info("Approved %s from MCP server '%s'", ", ".join(tools), server)

    async def start(self) -> None:
        await self.mcp.start()
        if self.status_api:
            await self.status_api.start()  # the status board shows "offline" until now
        if self.dashboard:
            await self.dashboard.start()
        self.index_conversations()  # anything said while the embeddings server was down
        if self.tasks:
            self.tasks.start()
        if self.alerts:
            self.alerts.start()
        self._projects = asyncio.create_task(self._watch_projects(), name="claude-projects")
        self._security = asyncio.create_task(self.security.forever(), name="security-updates")

    async def notify(self, text: str) -> bool:
        """Tell you something you didn't ask about, such as what a task found. Logged if there's no channel.
        False if it couldn't be sent."""
        if self.channel is None:
            log.warning("Nowhere to send a message of %d characters", len(text))  # not what it says: it can be personal
            return False
        try:
            await self.channel.tell(text)
        except Exception as exc:
            log.warning("Couldn't send a message: %s", exc)
            return False
        return True

    async def _alert(self, text: str) -> bool:
        if self.channel is None:  # Telegram hasn't started: try again next time
            return False
        return await self.notify(text)

    def _chat_name(self, chat_id: str) -> str:
        """Who a chat is with, for recent_work: you, or a task by its title."""
        if not chat_id.startswith("task:"):
            return "you"
        task = self.tasks.store.get(int(chat_id[5:])) if self.tasks and chat_id[5:].isdigit() else None
        return f"task “{task.title}”" if task else "a task"

    # -- projects handed to Claude (claude_projects.py) ------------------------------------------

    async def check_projects(self) -> dict[str, Any] | None:
        """Read Claude's projects from GitHub now, start Claude again where it's stopped, and tell you what's
        changed: a project done, Claude stuck, or not working. The board, or None if they're switched off."""
        projects = self.builtins.get("claude_projects")
        if projects is None:
            return None
        async with self._projects_lock:
            try:
                news = await projects.refresh()
            except Exception as exc:  # the board shows the problem; try again next time
                log.warning("Couldn't read Claude's projects: %s", exc)
                news = []
        for text in news:
            if await self.notify(text) and self.channel:  # in your chat too, so you can reply to it
                self.history.append_exchange(self.channel.chat_id, "(Claude's projects changed.)", text)
        return projects.board()

    async def _watch_projects(self) -> None:
        await asyncio.sleep(5)  # after Telegram's started, to tell you anything
        while True:
            await self.check_projects()
            projects = self.builtins.get("claude_projects")
            await asyncio.sleep(max(1.0, projects.cfg.poll_minutes if projects else 5.0) * 60)

    async def _tell_security(self, text: str) -> None:
        if await self.notify(text) and self.channel:  # in your chat too, so you can ask about it
            self.history.append_exchange(self.channel.chat_id, "(You checked the MCP servers' dependencies.)", text)

    async def _restart(self, names: set[str]) -> None:
        """Restart these MCP servers, once Athena's finished answering, holding new messages meanwhile."""
        async with self.agent.paused():
            await self.mcp.reload(names)
        self.rewarm.set()

    async def close(self) -> None:
        for task in (self._projects, self._security):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.alerts:
            await self.alerts.stop()
        if self.tasks:
            await self.tasks.stop()
            self.tasks.store.close()
            if self.tasks.watches:
                self.tasks.watches.close()
        self.skills.store.close()
        self.privacy.close()
        if self.dashboard:
            await self.dashboard.stop()
        if self.status_api:
            await self.status_api.stop()
        if self._indexer:
            self._indexer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._indexer
        await self.documents.stop()
        await self.mcp.stop()
        if self.mcp.approvals:
            self.mcp.approvals.close()
        await self.builtins.close()
        await self.llm.close()
        await self.embedder.close()
        self.memory.store.close()
        self.history.close()
        if self.stats:
            self.stats.close()

    # -- changes, from Telegram and the dashboard ---------------------------------------------

    async def reload(self, cfg: Config | None = None, *, reconnect: bool = False) -> None:
        """Apply the tools' settings: built-in tools and MCP servers, from ``cfg`` or config.toml as it is now.

        Waits for messages being answered to finish and holds new ones meanwhile, so each is
        answered with one set of tools. MCP servers whose settings haven't changed stay
        connected, unless ``reconnect``. Other settings only change when Athena restarts.
        """
        async with self._apply_lock:
            cfg = cfg or reread_config(self.config)
            self.applying = True
            try:
                async with self.agent.paused():
                    old, self.builtins = self.builtins, Builtins.build(cfg)
                    for section in (*(b.section for b in BUILTINS), "mcp_servers", "documents"):
                        setattr(self.config, section, getattr(cfg, section))
                    await self.mcp.configure(cfg.mcp_servers, reconnect=reconnect)
                    self.documents.configure(cfg.documents)
                await old.close()
            finally:
                self.applying = False
        self.rewarm.set()

    def set_thinking(self, mode: str) -> bool:
        """Have the model think before answering ("on"), not ("off"), or only briefly ("short"), from now on.

        It's saved in config.toml, so it stays after a restart. False if there's no config.toml to save it in.
        """
        if mode not in THINKING:
            raise ValueError(f"Thinking can be {', '.join(THINKING)}, not {mode!r}.")
        settings = Settings(self.config.path, self.config.env_path)
        if settings.editable:
            settings.set_values({"llm.thinking": mode})
        self.config.llm.thinking = mode  # type: ignore[assignment]
        log.info("Thinking is now %s", mode)
        return settings.editable

    async def take_back_last_exchange(self, chat_id: str) -> Exchange | None:
        """Take your last message in a chat and the reply to it out of the conversation, and out of memory,
        to answer it again. None if the model doesn't see one, say because a new session has started."""
        if self._indexer and not self._indexer.done():
            await asyncio.wait({self._indexer})  # so the reply isn't added to memory just after it's taken out
        exchange = self.history.take_last(chat_id)
        if exchange:
            self.memory.forget_conversation(exchange)
        return exchange

    def new_session(self) -> Session:
        """Clear the conversation from the model's context, in every chat. Memories are kept."""
        session = self.history.new_session()
        self.rewarm.set()
        return session

    async def forget_everything(self) -> Session:
        """Delete every memory and the conversation history, and start a new session. Usage statistics stay."""
        if self.tasks:
            self.tasks.stop_running()  # it would save what it's doing after everything's deleted
            self.tasks.reviews.clear()  # they hold what was said
        async with self.agent.paused():
            if self._indexer:
                self._indexer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._indexer
            await self.documents.stop()
            await asyncio.to_thread(self.memory.store.clear)
            session = await asyncio.to_thread(self.history.clear)
        self.rewarm.set()
        return session

    def index_conversations(self) -> None:
        """Add new exchanges to memory in the background, so they can be searched later."""
        if self._indexer and not self._indexer.done():
            self._index_again = True
            return
        self._indexer = asyncio.create_task(self._index(), name="index-conversations")

    async def _index(self) -> None:
        while True:
            self._index_again = False
            try:
                added = await self.memory.index_conversations(self.history)
                if added:
                    log.info("Added %d exchange%s to memory", added, "" if added == 1 else "s")
            except Exception as exc:  # the embeddings server may be down: try again after the next message
                log.warning("Couldn't add the conversation to memory yet: %s", exc)
                return
            if not self._index_again:
                return

    def _conversation_saved(self) -> None:
        self.index_conversations()
        self.rewarm.set()  # so the model server reads the new exchange now, not when the next message comes


def reread_config(cfg: Config) -> Config:
    """config.toml and .env as they are now, to apply changes made since Athena started.

    Athena's own secrets (the Telegram token and so on) only change when it restarts.
    """
    if cfg.path is None or not cfg.path.exists():
        return cfg
    for name, value in dotenv_values(cfg.env_path, interpolate=False).items():
        if value is not None and name not in PROTECTED_SECRETS:
            os.environ[name] = value
    return parse_config(cfg.path.read_text(), cfg.path)


def build_memory(cfg: Config) -> MemoryService:
    embedder = Embedder(cfg.embeddings)
    store = MemoryStore(cfg.db_path, cfg.embeddings.dimensions, cfg.embeddings.model)
    return MemoryService(store, embedder, cfg.memory)


def build_services(cfg: Config) -> Services:
    memory = build_memory(cfg)
    llm = LLMClient(cfg.llm)
    history = ConversationStore(cfg.db_path, cfg.agent.max_history_messages)
    stats = UsageStats(cfg.db_path)

    tools = ToolRegistry()
    for tool in memory.tools():
        tools.add(tool)
    mcp = MCPManager(
        cfg.mcp_servers,
        cfg.base_dir,
        cfg.log_dir,
        ServerEnvs.for_config(cfg),
        ToolApprovals(cfg.db_path),
        Sandbox.detect(),
    )

    prompt_path = cfg.resolve(cfg.agent.system_prompt_file)
    template = None
    if prompt_path.exists():
        template = prompt_path.read_text()
    else:
        log.warning("System prompt %s not found; using a minimal built-in prompt", prompt_path)

    status = StatusTracker(show_task=cfg.display.show_task)
    status_api = None
    if cfg.display.enabled:
        status_api = StatusServer(cfg.display, status, name=cfg.agent.assistant_name, timezone=cfg.agent.timezone)
    agent = Agent(
        cfg.agent,
        llm,
        tools,
        history,
        memory,
        system_prompt_template=template,
        auto_recall=cfg.memory.auto_recall,
        status=status,
        stats=stats,
    )
    return Services(
        cfg,
        llm,
        memory.embedder,
        memory,
        history,
        tools,
        mcp,
        agent,
        status,
        status_api,
        Builtins.build(cfg),
        stats,
    )
