"""Builds the assistant's components from the config."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pi_assistant.agent import Agent
from pi_assistant.config import Config
from pi_assistant.databases import Databases
from pi_assistant.history import ConversationStore
from pi_assistant.llm import LLMClient
from pi_assistant.mcp_manager import MCPManager
from pi_assistant.memory import Embedder, MemoryService, MemoryStore
from pi_assistant.news import NewsReader
from pi_assistant.status import StatusTracker
from pi_assistant.status_api import StatusServer
from pi_assistant.tools import ToolRegistry
from pi_assistant.trading212 import Trading212

log = logging.getLogger(__name__)


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
    news: NewsReader | None = None
    databases: Databases | None = None
    trading212: Trading212 | None = None

    async def start(self) -> None:
        await self.mcp.start()
        if self.status_api:
            await self.status_api.start()  # the status board shows "offline" until now

    async def close(self) -> None:
        if self.status_api:
            await self.status_api.stop()
        await self.mcp.stop()
        if self.news:
            await self.news.close()
        if self.trading212:
            await self.trading212.close()
        await self.llm.close()
        await self.embedder.close()
        self.memory.store.close()
        self.history.close()


def build_memory(cfg: Config) -> MemoryService:
    embedder = Embedder(cfg.embeddings)
    store = MemoryStore(cfg.db_path, cfg.embeddings.dimensions, cfg.embeddings.model)
    return MemoryService(store, embedder, cfg.memory)


def build_services(cfg: Config) -> Services:
    memory = build_memory(cfg)
    llm = LLMClient(cfg.llm)
    history = ConversationStore(cfg.db_path, cfg.agent.max_history_messages)

    tools = ToolRegistry()
    for tool in memory.tools():
        tools.add(tool)
    news = NewsReader(cfg.news, cfg.agent.timezone) if cfg.news.feeds else None
    for tool in news.tools() if news else []:
        tools.add(tool)
    databases = None
    if cfg.sqlite.databases:
        databases = Databases(cfg.sqlite, {name: cfg.resolve(path) for name, path in cfg.sqlite.databases.items()})
        for tool in databases.tools():
            tools.add(tool)
    trading212 = None
    if cfg.trading212.enabled and not cfg.trading212.api_key:
        log.warning("Trading 212 is switched on, but TRADING212_API_KEY isn't set in .env, so it's left out")
    elif cfg.trading212.enabled:
        trading212 = Trading212(cfg.trading212, cfg.agent.timezone)
        for tool in trading212.tools():
            tools.add(tool)
    mcp = MCPManager(cfg.mcp_servers, cfg.base_dir, cfg.log_dir)
    tools.add_provider(mcp.tools)

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
        news,
        databases,
        trading212,
    )
