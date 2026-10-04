"""Builds the assistant's components from the config."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pi_assistant.agent import Agent
from pi_assistant.config import Config
from pi_assistant.history import ConversationStore
from pi_assistant.llm import LLMClient
from pi_assistant.mcp_manager import MCPManager
from pi_assistant.memory import Embedder, MemoryService, MemoryStore
from pi_assistant.news import NewsReader
from pi_assistant.status import StatusFile, StatusTracker
from pi_assistant.tools import ToolRegistry

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
    status_file: StatusFile
    news: NewsReader | None = None

    async def start(self) -> None:
        await self.mcp.start()
        self.status_file.publish(self.status)  # the status board shows "offline" until now

    async def close(self) -> None:
        self.status_file.close()
        await self.mcp.stop()
        if self.news:
            await self.news.close()
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
    mcp = MCPManager(cfg.mcp_servers, cfg.base_dir, cfg.log_dir)
    tools.add_provider(mcp.tools)

    prompt_path = cfg.resolve(cfg.agent.system_prompt_file)
    template = None
    if prompt_path.exists():
        template = prompt_path.read_text()
    else:
        log.warning("System prompt %s not found; using a minimal built-in prompt", prompt_path)

    status = StatusTracker(show_task=cfg.display.show_task)
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
        cfg, llm, memory.embedder, memory, history, tools, mcp, agent, status, StatusFile(cfg.status_path), news
    )
