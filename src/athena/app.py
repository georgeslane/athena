"""Builds the assistant's components from the config, and changes them while it runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from dotenv import dotenv_values

from athena.agent import Agent
from athena.alerts import HealthMonitor, check_health
from athena.approvals import Approval, Approvals
from athena.builtin import Builtin, Unavailable
from athena.channel import Reach
from athena.chat import DashboardChat
from athena.config import THINKING, Config, parse_config
from athena.dags.migrate import start_afresh, upgrade
from athena.dags.service import Dags
from athena.dags.store import AgentStore
from athena.dashboard import DashboardServer
from athena.documents import DocumentIndex
from athena.history import Busy, ConversationStore, Exchange, Session
from athena.integrations import BUILTINS
from athena.llm import LLMClient
from athena.mcp_manager import MCPManager
from athena.memory import Embedder, MemoryService, MemoryStore
from athena.privacy import Privacy, TermStore
from athena.push import Push
from athena.resources import SYSTEM_PROMPT
from athena.sandbox import Sandbox
from athena.server_envs import ServerEnvs
from athena.server_security import ServerSecurity
from athena.settings import PROTECTED_SECRETS, Settings
from athena.skill_reviews import SkillReviews
from athena.skills import Skills, SkillStore
from athena.stats import UsageStats
from athena.status import StatusTracker
from athena.status_api import StatusServer
from athena.tool_approvals import ToolApprovals
from athena.tools import Tool, ToolRegistry
from athena.workspace import clear_workspace, workspace_of

if TYPE_CHECKING:
    from athena.channel import Channel

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
        # Tools waiting for your OK, which Telegram or the dashboard can give (approvals.py).
        self.approvals = Approvals()
        # Agents: each one's model, guidance and DAG, which runs by itself and tells you what it found (dags/).
        self.dags = Dags(
            self.config.tasks,
            AgentStore(self.config.db_path),
            self.agent,
            timezone=self.config.agent.timezone,
            channel=lambda: self.reach,
            state=self.config.db_path.with_name("dags.db"),  # the engine's runs and approvals
            workspace=workspace_of(self.config),  # where each agent's files are
            on_ran=self.rewarm.set,  # the master agent's conversation may have what a run found in it
        )
        for tool in self.dags.tools():
            self.tools.add(tool)
        self.agent.directory = self.dags
        # After a skill's used in a chat, looking back at how it went (skill_reviews.py).
        self.skill_reviews = SkillReviews(self.config.tasks, self.agent, channel=lambda: self.reach)
        self.agent.on_answered = self.skill_reviews.after_answer
        # How to do things your way, which Athena reads before doing them (skills.py).
        self.skills = Skills(
            SkillStore(self.config.db_path),
            self.history,
            timezone=self.config.agent.timezone,
            on_change=self.rewarm.set,  # the list of them is in the prompt
        )
        # After the system prompt: what some MCP servers are, and the skills.
        self.agent.prompt_extra = lambda: "\n\n".join(part for part in (self.mcp.about(), self.skills.index()) if part)
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
        self.chat = DashboardChat(self)  # chatting on the dashboard (chat.py)
        self.dashboard = DashboardServer(self) if self.config.dashboard.enabled else None
        # Notifications on your devices, for the dashboard installed as an app (push.py).
        self.push: Push | None = None
        if self.dashboard:
            data = self.config.resolve(self.config.data_dir)
            show_text = self.config.dashboard.notification_text
            self.push = Push(self.config.db_path, data / "push-key.pem", show_text=show_text)
            self.chat.on_answer = self._answered_on_the_dashboard
            self.approvals.listeners.append(self._asking)
        self._notifying: set[asyncio.Task[Any]] = set()
        # How Athena reaches you, through everything it has (channel.py).
        self.reach = Reach(self)
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
        self.agent.on_compacted = self.rewarm.set  # what the model sees of the chat has changed

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
        if self.config.tasks.enabled:
            await self.dags.start()
        self.skill_reviews.start()
        if self.alerts:
            self.alerts.start()
        self._projects = asyncio.create_task(self._watch_projects(), name="claude-projects")
        self._security = asyncio.create_task(self.security.forever(), name="security-updates")

    async def notify(self, text: str, *, session: int | None = None) -> bool:
        """Tell you something you didn't ask about, such as what an agent's DAG found: in Telegram, and on your
        devices unless you're looking at the dashboard. Nowhere, if it's what was said in a ``session``, an agent,
        you've muted: it's in the agent's chat. False if it couldn't reach you."""
        name = self.config.agent.assistant_name
        if session is None:
            told = await self.notify_devices(name, text, url="#agents", tag="athena")
        else:
            agent = self.history.get_session(session)
            if agent is None or agent.muted:
                return False
            told = await self._notify_about(session, text)
        if self.channel is None:
            if not told:  # not what it says: it can be personal
                log.warning("Nowhere to send a message of %d characters", len(text))
            return told
        try:
            await self.channel.tell(text)
        except Exception as exc:
            log.warning("Couldn't send a message: %s", exc)
            return told
        return True

    async def notify_devices(self, title: str, text: str, *, url: str, tag: str, urgent: bool = False) -> bool:
        """Notify your devices, unless you're looking at the dashboard, which shows it already. True if you were
        looking, or a device was reached."""
        if self.dashboard and self.dashboard.looking:
            return True
        if self.push is None:
            return False
        try:
            return await self.push.send(title, text, url=url, tag=tag, urgent=urgent) > 0
        except Exception:  # a notification mustn't stop anything
            log.exception("Couldn't send a notification")
            return False

    def _later(self, notifying: Any) -> None:
        task = asyncio.create_task(notifying, name="notify")
        self._notifying.add(task)
        task.add_done_callback(self._notifying.discard)

    async def _notify_about(self, session_id: int, text: str) -> bool:
        """Notify your devices of what was said in a session, unless it's muted, titled with where it was said."""
        session = self.history.get_session(session_id)
        if session is None or session.muted:
            return False
        name, where = self.config.agent.assistant_name, self.history.title(session_id)
        # Where it was said is part of what was said: left out with the rest, if you'd rather.
        title = f"{name} · {where}" if where and self.push and self.push.show_text else name
        return await self.notify_devices(title, text, url=f"#agents/{session_id}", tag=f"reply-{session_id}")

    def _answered_on_the_dashboard(self, reply: Any, result: Any) -> None:
        """Your devices hear of an answer to a message sent on the dashboard, in case you've gone."""
        self._later(self._notify_about(reply.session, result.text))

    def _asking(self, approval: Approval) -> None:
        """Your devices hear when Athena's waiting for your OK."""
        if approval.verdict:  # answered
            return
        said = f"{approval.why}: allow {approval.tool}?" if approval.why else f"Allow {approval.tool}?"
        if approval.summary:
            said += f" {approval.summary}"
        title = f"{self.config.agent.assistant_name} needs your OK"
        url = f"#agents/{approval.session}" if approval.session is not None else "#agents"
        self._later(self.notify_devices(title, said, url=url, tag=f"ask-{approval.id}", urgent=True))

    async def _alert(self, text: str) -> bool:
        return await self.notify(text)

    def keep(self, yours: str, said: str) -> None:
        """Keep something Athena told you by itself in the master agent's conversation, where Telegram talks to it,
        so you can reply to it: ``yours`` is a line for your side, such as "(Claude's projects changed.)"."""
        self.history.append_exchange(self.history.master_chat, yours, said, channel="Athena")

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
            if await self.notify(text):  # in your chat too, so you can reply to it
                self.keep("(Claude's projects changed.)", text)
        for project in projects.finished:  # the DAGs waiting for Claude to finish something
            said = f"Claude has finished “{project.title}”" + (f": {project.progress}" if project.progress else ".")
            if self.config.tasks.enabled:
                await self.dags.claude_done(f"{said}\n{project.url}")
        return projects.board()

    async def _watch_projects(self) -> None:
        await asyncio.sleep(5)  # after Telegram's started, to tell you anything
        while True:
            await self.check_projects()
            projects = self.builtins.get("claude_projects")
            await asyncio.sleep(max(1.0, projects.cfg.poll_minutes if projects else 5.0) * 60)

    async def _tell_security(self, text: str) -> None:
        if await self.notify(text):  # in your chat too, so you can ask about it
            self.keep("(You checked the MCP servers' dependencies.)", text)

    async def _restart(self, names: set[str]) -> None:
        """Restart these MCP servers, once Athena's finished answering, holding new messages meanwhile."""
        async with self.agent.paused():
            await self.mcp.reload(names)
        self.rewarm.set()

    async def close(self) -> None:
        await self.chat.stop()  # an answer to the dashboard, before what it uses closes
        for task in (self._projects, self._security):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.alerts:
            await self.alerts.stop()
        await self.skill_reviews.stop()
        await self.dags.stop()
        self.dags.store.close()
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
        await self.agent.close()  # before the model client and history it uses
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
        for task in self._notifying:
            task.cancel()
        if self.push:
            await self.push.close()

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
                    for section in (*(b.section for b in BUILTINS), "mcp_servers", "documents", "tools"):
                        setattr(self.config, section, getattr(cfg, section))
                    self.tools.on_demand = list(cfg.tools.on_demand)
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

    async def delete_session(self, session_id: int) -> None:
        """Delete an agent: what was said to it, here and in memory, its DAG, runs and files. Busy if Athena's
        answering you in it; KeyError if there's no such agent; ValueError if it's the master agent."""
        replying = self.chat.replying
        answering = replying.session if replying else self.history.master.id if self.agent.answering_you else None
        if answering == session_id:
            raise Busy("I'm answering you in that agent. Wait until I've finished, or stop me, first.")
        if self.history.get_session(session_id) is None:
            raise KeyError(session_id)
        if session_id == self.history.master.id:
            raise ValueError("The master agent can't be deleted: Telegram, Siri and the terminal talk to it.")
        await self.dags.delete(session_id)  # its DAG, runs and files, stopping a run if one's going
        if self._indexer and not self._indexer.done():
            await asyncio.wait({self._indexer})  # so nothing said in it is added to memory just after
        for exchange in self.history.delete_session(session_id):
            self.memory.forget_conversation(exchange)
        self.chat.failed.pop(session_id, None)
        if self.agent.last_chat == f"session:{session_id}":
            self.agent.last_chat = None
        self.rewarm.set()  # the master agent's told of the others
        log.info("Deleted agent %d", session_id)

    async def forget_everything(self) -> Session:
        """Delete every memory, the conversation history, the code's workspace, with agents' files, and what DAG
        runs found. Returns the master agent. Usage statistics, and the agents and their DAGs, stay."""
        await self.dags.stop_running()  # it would save what it's doing after everything's deleted
        self.skill_reviews.reviews.clear()  # they hold what was said
        async with self.agent.paused():
            if self._indexer:
                self._indexer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._indexer
            await self.documents.stop()
            await asyncio.to_thread(self.memory.store.clear)
            await asyncio.to_thread(self.history.clear)
            await asyncio.to_thread(clear_workspace, self.config)  # what code made from what was said
            await asyncio.to_thread(self.dags.forget)  # what runs found and kept
        self.rewarm.set()
        return self.history.master

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
    start_afresh(cfg.db_path, memory.store, cfg.db_path.parent / "photos")  # once, before the agents are read
    upgrade(cfg.db_path)  # once: runs became agentic-airflow's
    llm = LLMClient(cfg.llm)
    history = ConversationStore(cfg.db_path, cfg.agent.max_history_messages)
    stats = UsageStats(cfg.db_path)

    tools = ToolRegistry()
    tools.on_demand = list(cfg.tools.on_demand)
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

    prompt_path = cfg.resolve(cfg.agent.system_prompt_file) if cfg.agent.system_prompt_file else SYSTEM_PROMPT
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
