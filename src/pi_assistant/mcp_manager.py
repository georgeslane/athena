"""Connects to the configured MCP servers and exposes their tools to the agent.

Each server's connection is opened and closed inside a task of its own, because the
MCP SDK's transports must be entered and exited from the same task. That also lets
one server be added, changed or removed without touching the others.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from mcp import Client, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp_types import Implementation

from pi_assistant import __version__
from pi_assistant.config import MCPServerConfig
from pi_assistant.sandbox import Sandbox, SandboxError
from pi_assistant.server_envs import ServerEnvError, ServerEnvs, parse_uvx
from pi_assistant.tool_approvals import ToolApprovals, ToolChange, ToolVersion
from pi_assistant.tools import Tool, matches_any, safe_tool_name

log = logging.getLogger(__name__)

RESERVED_NAMES = {"remember", "search_memory", "forget_memory"}  # the memory tools'


@dataclass
class ServerStatus:
    name: str
    connected: bool = False
    tools: int = 0  # how many of its tools the model sees
    error: str | None = None
    # Every tool the server offers, as (name, description), including any the config hides.
    offered: list[tuple[str, str]] = field(default_factory=list)
    # For servers started with uvx: where the lock on its dependencies came from ("reviewed", from
    # mcp-locks/, or "first use"), or "unlocked" if uvx is given options that can't be locked.
    lock: str | None = None
    # Tools it offers that are new or have changed since you approved them, and so are held back.
    pending: list[ToolChange] = field(default_factory=list)
    sandboxed: bool = False  # running in a sandbox


def result_to_text(result: Any) -> str:
    """Flatten an MCP CallToolResult into text the model can read."""
    parts: list[str] = []
    for block in result.content or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            parts.append(block.text)
        elif kind == "image":
            parts.append(f"[image: {block.mime_type}]")
        elif kind == "audio":
            parts.append(f"[audio: {block.mime_type}]")
        elif kind == "resource":
            res = block.resource
            parts.append(getattr(res, "text", None) or f"[resource: {res.uri}]")
        elif kind == "resource_link":
            parts.append(f"[link: {block.uri}]")
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False, default=str))
    text = "\n".join(parts) or "(no output)"
    return f"Error: {text}" if result.is_error else text


class _Connection:
    """One server, connected (or trying to be) by its own task."""

    def __init__(self, name: str, cfg: MCPServerConfig):
        self.name = name
        self.cfg = cfg
        self.status = ServerStatus(name)
        self.client: Client | None = None
        self.launch: list[str] | None = None  # the command that starts it, if not the configured one
        self.remote_tools: list[Any] = []
        self.ready = asyncio.Event()  # connected, or given up
        self.closing = asyncio.Event()
        self.task: asyncio.Task[None] | None = None


class MCPManager:
    def __init__(
        self,
        servers: dict[str, MCPServerConfig],
        base_dir: Path | None = None,
        log_dir: Path | None = None,
        envs: ServerEnvs | None = None,
        approvals: ToolApprovals | None = None,
        sandbox: Sandbox | None = None,
    ):
        self.configured = dict(servers)  # including those switched off
        self.base_dir = base_dir or Path.cwd()
        # Local servers' stderr goes to <log_dir>/mcp-<name>.log (or our stderr if None).
        self.log_dir = log_dir
        # Servers started with uvx run from locked environments made by this (as given, if None).
        self.envs = envs
        # Tools that are new or changed are held back until approved here (all are used, if None).
        self.approvals = approvals
        # Local servers run in this, unless their config says not to (and nothing is, if None).
        self.sandbox = sandbox
        # Told (server, changes) when a server's tools change, once for each change.
        self.listeners: list[Callable[[str, list[ToolChange]], None]] = []
        self._told: set[tuple[str, str, str]] = set()
        self._connections: dict[str, _Connection] = {}
        self._tools: list[Tool] = []
        self._lock = asyncio.Lock()
        self._started = False

    # -- public API ---------------------------------------------------------------------

    @property
    def servers(self) -> dict[str, MCPServerConfig]:
        """The servers that are switched on, in config order."""
        return {name: cfg for name, cfg in self.configured.items() if cfg.enabled}

    def tools(self) -> list[Tool]:
        return list(self._tools)

    def status(self) -> list[ServerStatus]:
        return [
            self._connections[name].status if name in self._connections else ServerStatus(name) for name in self.servers
        ]

    def approve(self, server: str, tools: dict[str, str]) -> None:
        """Approve held-back tools, given as {tool: fingerprint}, so it's exactly the version you looked at."""
        conn = self._connections.get(server)
        held = {c.tool: c for c in conn.status.pending} if conn else {}
        for tool, seen in tools.items():
            if tool not in held or held[tool].now.fingerprint != seen:
                raise ValueError(f"{tool} isn't waiting for approval, or has changed again: have another look.")
        if self.approvals and tools:
            self.approvals.approve(server, {tool: held[tool].now for tool in tools})
            self._rebuild_tools()

    async def start(self) -> None:
        self._started = True
        await self._apply()

    async def configure(self, servers: dict[str, MCPServerConfig], *, reconnect: bool = False) -> None:
        """Switch to these servers: connect new and changed ones, disconnect removed ones, and leave
        the rest connected, unless ``reconnect``."""
        self.configured = dict(servers)
        if self._started:
            await self._apply(restart=set(self._connections) if reconnect else None)

    async def reload(self, names: set[str] | None = None) -> None:
        """Disconnect and reconnect these servers, or all of them (e.g. after the Mac wakes up)."""
        if not self._started:
            await self.start()
            return
        await self._apply(restart=set(self._connections) if names is None else names)

    async def stop(self) -> None:
        self._started = False
        for conn in self._connections.values():
            if conn.task and not conn.ready.is_set():
                conn.task.cancel()  # still connecting: there's no need to wait for it
        await self._apply()

    # -- connection handling ------------------------------------------------------------------

    async def _apply(self, restart: set[str] | None = None) -> None:
        async with self._lock:
            wanted = self.servers if self._started else {}
            old = [
                conn
                for name, conn in self._connections.items()
                if name not in wanted or wanted[name] != conn.cfg or name in (restart or set())
            ]
            for conn in old:
                del self._connections[conn.name]
            self._rebuild_tools()  # their tools go at once
            await asyncio.gather(*(self._close(conn) for conn in old))

            new = [_Connection(name, cfg) for name, cfg in wanted.items() if name not in self._connections]
            for conn in new:
                self._connections[conn.name] = conn
                conn.task = asyncio.create_task(self._run(conn), name=f"mcp-{conn.name}")
            await asyncio.gather(*(conn.ready.wait() for conn in new))
            self._connections = {name: self._connections[name] for name in wanted if name in self._connections}
            self._rebuild_tools()

    async def _close(self, conn: _Connection) -> None:
        conn.closing.set()
        if conn.task and not conn.task.done():
            done, _ = await asyncio.wait({conn.task}, timeout=15)
            if not done:
                conn.task.cancel()

    async def _run(self, conn: _Connection) -> None:
        log_path = self.log_dir / f"mcp-{conn.name}.log" if self.log_dir and conn.cfg.command else None
        try:
            if not await self._install(conn):
                return
            async with AsyncExitStack() as stack:
                try:
                    async with asyncio.timeout(conn.cfg.timeout_seconds):
                        await self._connect(stack, conn, log_path)
                except BaseException as exc:  # noqa: BLE001 - anyio may wrap errors in exception groups
                    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                        raise  # our own timeout surfaces as TimeoutError, so this is a real shutdown
                    conn.status.error = _describe(exc) + (f" (server log: {log_path})" if log_path else "")
                    log.warning("MCP server '%s' unavailable: %s", conn.name, conn.status.error)
                    return
                conn.status.connected = True
                log.info("MCP server '%s' connected", conn.name)
                conn.ready.set()
                await conn.closing.wait()
        except BaseException as exc:  # noqa: BLE001 - closing a broken connection can fail in many ways
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            log.debug("MCP server '%s' didn't close cleanly: %s", conn.name, _describe(exc))
        finally:
            conn.status.connected = False
            conn.ready.set()

    async def _install(self, conn: _Connection) -> bool:
        """Get a local server ready to start: check it can be sandboxed, and lock and install its environment
        if it's started with uvx. False if that failed, with why in its status."""
        if self._sandboxed(conn.cfg):
            assert self.sandbox
            try:
                await self.sandbox.check()
            except SandboxError as exc:
                conn.status.error = str(exc)
                log.warning("MCP server '%s' unavailable: %s", conn.name, exc)
                return False
            conn.status.sandboxed = True
        if conn.cfg.command != "uvx" or self.envs is None:
            return True
        server = parse_uvx(conn.cfg.args)
        if server is None:
            conn.status.lock = "unlocked"
            log.warning("MCP server '%s' runs with uvx options that can't be locked, so it isn't", conn.name)
            return True
        try:
            conn.launch = await self.envs.prepare(conn.name, server)
        except (ServerEnvError, TimeoutError) as exc:
            conn.status.error = str(exc) if isinstance(exc, ServerEnvError) else "installing it took too long"
            log.warning("MCP server '%s' unavailable: %s", conn.name, conn.status.error)
            return False
        conn.status.lock = self.envs.lock_origin(conn.name)
        return True

    def _sandboxed(self, cfg: MCPServerConfig) -> bool:
        return bool(cfg.command and cfg.sandbox and self.sandbox and self.sandbox.supported)

    def _client_for(self, conn: _Connection, http_client: Any, errlog: TextIO) -> Client:
        cfg = conn.cfg
        info = Implementation(name="pi-assistant", version=__version__)
        if cfg.command:
            cwd = str((self.base_dir / cfg.cwd).resolve()) if cfg.cwd else None
            command, *args = conn.launch or [cfg.command, *cfg.args]
            if conn.status.sandboxed:
                assert self.sandbox
                paths = [self.base_dir / p for p in cfg.read_only_paths]
                command, *args = self.sandbox.wrap(
                    [command, *args], network=cfg.network, read_only=paths, cwd=Path(cwd) if cwd else None
                )
            # Servers get a minimal environment (PATH, HOME, ...) plus `env` from the config,
            # so your Telegram token and API keys aren't visible to third-party code.
            params = StdioServerParameters(command=command, args=args, env=cfg.env or None, cwd=cwd)
            return Client(
                stdio_client(params, errlog=errlog), read_timeout_seconds=cfg.timeout_seconds, client_info=info
            )
        assert cfg.url
        if cfg.http_transport == "sse":
            transport = sse_client(cfg.url, headers=cfg.headers or None)
        else:
            transport = streamable_http_client(cfg.url, http_client=http_client)
        return Client(transport, read_timeout_seconds=cfg.timeout_seconds, client_info=info)

    async def _connect(self, stack: AsyncExitStack, conn: _Connection, log_path: Path | None) -> None:
        cfg = conn.cfg
        errlog: TextIO = sys.stderr
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            errlog = stack.enter_context(open(log_path, "a", buffering=1))
            command = " ".join(conn.launch or [cfg.command or "", *cfg.args])
            errlog.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} starting {command}\n")
        http_client = None
        if cfg.url and cfg.http_transport == "http":
            http_client = await stack.enter_async_context(create_mcp_http_client(headers=cfg.headers or None))
        client = await stack.enter_async_context(self._client_for(conn, http_client, errlog))
        listing = await client.list_tools()
        remote_tools = list(listing.tools)
        while listing.next_cursor:
            listing = await client.list_tools(cursor=listing.next_cursor)
            remote_tools.extend(listing.tools)
        conn.client = client
        conn.remote_tools = remote_tools
        if self.approvals and not self.approvals.knows(conn.name):
            # The first time: you've just chosen this server, and can see its tools on the dashboard.
            self.approvals.approve(conn.name, {t.name: _version(t) for t in remote_tools})
        conn.status.offered = [(t.name, (t.description or t.title or "").strip()) for t in remote_tools]

    def _rebuild_tools(self) -> None:
        """The tools the model sees, in config order. A name another server already has gets the server's name too."""
        tools: list[Tool] = []
        for conn in self._connections.values():
            if not conn.status.connected or conn.client is None:
                continue
            cfg, taken = conn.cfg, {t.name for t in tools}
            approved = self.approvals.approved(conn.name) if self.approvals else None
            mine: list[Tool] = []
            held: list[ToolChange] = []
            for remote in conn.remote_tools:
                if cfg.include and not matches_any(remote.name, cfg.include):
                    continue
                if matches_any(remote.name, cfg.exclude):
                    continue
                version = _version(remote)
                if approved is not None:
                    before = approved.get(remote.name)
                    if before is None or before.fingerprint != version.fingerprint:
                        held.append(ToolChange(remote.name, version, before))
                        continue
                tool_name = safe_tool_name(remote.name)
                if tool_name in taken or tool_name in RESERVED_NAMES:
                    tool_name = safe_tool_name(f"{conn.name}__{remote.name}")
                mine.append(
                    Tool(
                        name=tool_name,
                        description=version.description,
                        parameters=version.parameters,
                        handler=self._make_handler(conn.client, remote.name),
                        needs_confirmation=matches_any(remote.name, cfg.confirm),
                        source=f"mcp:{conn.name}",
                    )
                )
            conn.status.tools = len(mine)
            conn.status.pending = held
            tools.extend(mine)
            self._tell(conn.name, held)
        self._tools = tools

    def _tell(self, server: str, held: list[ToolChange]) -> None:
        new = [c for c in held if (server, c.tool, c.now.fingerprint) not in self._told]
        if not new:
            return
        self._told.update((server, c.tool, c.now.fingerprint) for c in new)
        log.warning(
            "MCP server '%s' has new or changed tools, held back until approved: %s",
            server,
            ", ".join(c.tool for c in new),
        )
        for listener in self.listeners:
            try:
                listener(server, new)
            except Exception:
                log.exception("Couldn't tell a listener about changed tools")

    @staticmethod
    def _make_handler(client: Client, remote_name: str):
        async def handler(args: dict[str, Any]) -> str:
            result = await client.call_tool(remote_name, args)
            return result_to_text(result)

        return handler


def _version(remote: Any) -> ToolVersion:
    """What the model is shown of a server's tool: its description and the JSON schema of its arguments."""
    schema = dict(remote.input_schema or {})
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return ToolVersion((remote.description or remote.title or remote.name).strip(), schema)


def _describe(exc: BaseException) -> str:
    """Dig the useful message out of anyio exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, TimeoutError):
        return "timed out while connecting"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
