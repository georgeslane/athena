"""Builds the assistant's components from the config, and changes them while it runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import dotenv_values

from pi_assistant.agent import Agent
from pi_assistant.builtin import Builtin, Unavailable
from pi_assistant.config import THINKING, Config, parse_config
from pi_assistant.dashboard import DashboardServer
from pi_assistant.documents import DocumentIndex
from pi_assistant.history import ConversationStore, Exchange, Session
from pi_assistant.integrations import BUILTINS
from pi_assistant.llm import LLMClient
from pi_assistant.mcp_manager import MCPManager
from pi_assistant.memory import Embedder, MemoryService, MemoryStore
from pi_assistant.sandbox import Sandbox
from pi_assistant.server_envs import ServerEnvs
from pi_assistant.settings import PROTECTED_SECRETS, Settings
from pi_assistant.stats import UsageStats
from pi_assistant.status import StatusTracker
from pi_assistant.status_api import StatusServer
from pi_assistant.tool_approvals import ToolApprovals
from pi_assistant.tools import Tool, ToolRegistry

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
        # After the memory tools, in this order, so the tools' order (and the prompt) stays the same.
        self.tools.add_provider(lambda: self.builtins.tools)
        self.tools.add_provider(self.mcp.tools)
        # Your documents, indexed from your Mac when you ask on the dashboard.
        self.documents = DocumentIndex(self.config.documents, self.memory, self.config.log_dir)
        self.dashboard = DashboardServer(self) if self.config.dashboard.enabled else None
        # Set to have the model server read the prompt again now, e.g. after the tools change.
        self.rewarm = asyncio.Event()
        self.applying = False  # changes to the tools are being applied
        self._apply_lock = asyncio.Lock()
        self._indexer: asyncio.Task[None] | None = None
        self._index_again = False
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

    async def close(self) -> None:
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
