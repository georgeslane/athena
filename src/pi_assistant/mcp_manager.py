"""Connects to the configured MCP servers and exposes their tools to the agent.

All connections are opened and closed inside one long-lived task, because the MCP
SDK's transports must be entered and exited from the same task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
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
from pi_assistant.tools import Tool, matches_any, safe_tool_name

log = logging.getLogger(__name__)


@dataclass
class ServerStatus:
    name: str
    connected: bool = False
    tools: int = 0
    error: str | None = None


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


class MCPManager:
    def __init__(self, servers: dict[str, MCPServerConfig], base_dir: Path | None = None, log_dir: Path | None = None):
        self.servers = {name: cfg for name, cfg in servers.items() if cfg.enabled}
        self.base_dir = base_dir or Path.cwd()
        # Local servers' stderr goes to <log_dir>/mcp-<name>.log (or our stderr if None).
        self.log_dir = log_dir
        self._tools: list[Tool] = []
        self._status: dict[str, ServerStatus] = {}
        self._runner: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._wake = asyncio.Event()
        self._stopping = False

    # -- public API ---------------------------------------------------------------------

    def tools(self) -> list[Tool]:
        return list(self._tools)

    def status(self) -> list[ServerStatus]:
        return [self._status.get(name, ServerStatus(name)) for name in self.servers]

    async def start(self) -> None:
        if self._runner is None:
            self._runner = asyncio.create_task(self._run(), name="mcp-manager")
        await self._wait_ready()

    async def reload(self) -> None:
        """Disconnect and reconnect every server (e.g. after the Mac wakes up)."""
        if self._runner is None:
            await self.start()
            return
        self._ready.clear()
        self._wake.set()
        await self._wait_ready()

    async def stop(self) -> None:
        if self._runner is None:
            return
        self._stopping = True
        self._wake.set()
        try:
            await asyncio.wait_for(self._runner, timeout=15)
        except (TimeoutError, asyncio.CancelledError):
            self._runner.cancel()
        except Exception:
            log.exception("Error while stopping MCP servers")
        self._runner = None

    async def _wait_ready(self) -> None:
        assert self._runner is not None
        ready = asyncio.create_task(self._ready.wait())
        done, _ = await asyncio.wait({ready, self._runner}, return_when=asyncio.FIRST_COMPLETED)
        if self._runner in done:
            ready.cancel()
            self._runner.result()  # re-raise whatever killed the runner

    # -- connection handling ------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            async with AsyncExitStack() as stack:
                tools: list[Tool] = []
                for name, cfg in self.servers.items():
                    tools.extend(await self._connect(stack, name, cfg, {t.name for t in tools}))
                self._tools = tools
                self._ready.set()
                await self._wake.wait()
                self._wake.clear()
                self._tools = []
            if self._stopping:
                return

    def _client_for(self, cfg: MCPServerConfig, http_client: Any, errlog: TextIO) -> Client:
        info = Implementation(name="pi-assistant", version=__version__)
        if cfg.command:
            cwd = str((self.base_dir / cfg.cwd).resolve()) if cfg.cwd else None
            # Servers get a minimal environment (PATH, HOME, ...) plus `env` from the config,
            # so your Telegram token and API keys aren't visible to third-party code.
            params = StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env or None, cwd=cwd)
            return Client(
                stdio_client(params, errlog=errlog), read_timeout_seconds=cfg.timeout_seconds, client_info=info
            )
        assert cfg.url
        if cfg.http_transport == "sse":
            transport = sse_client(cfg.url, headers=cfg.headers or None)
        else:
            transport = streamable_http_client(cfg.url, http_client=http_client)
        return Client(transport, read_timeout_seconds=cfg.timeout_seconds, client_info=info)

    async def _connect(self, stack: AsyncExitStack, name: str, cfg: MCPServerConfig, taken: set[str]) -> list[Tool]:
        status = ServerStatus(name)
        self._status[name] = status
        server_stack = AsyncExitStack()
        log_path = self.log_dir / f"mcp-{name}.log" if self.log_dir and cfg.command else None
        try:
            async with asyncio.timeout(cfg.timeout_seconds):
                errlog: TextIO = sys.stderr
                if log_path:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    errlog = server_stack.enter_context(open(log_path, "a", buffering=1))
                    errlog.write(
                        f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} starting {cfg.command} {' '.join(cfg.args)}\n"
                    )
                http_client = None
                if cfg.url and cfg.http_transport == "http":
                    http_client = await server_stack.enter_async_context(
                        create_mcp_http_client(headers=cfg.headers or None)
                    )
                client = await server_stack.enter_async_context(self._client_for(cfg, http_client, errlog))
                listing = await client.list_tools()
                remote_tools = list(listing.tools)
                while listing.next_cursor:
                    listing = await client.list_tools(cursor=listing.next_cursor)
                    remote_tools.extend(listing.tools)
        except BaseException as exc:  # noqa: BLE001 - anyio may wrap errors in exception groups
            try:
                await server_stack.aclose()
            except BaseException:  # noqa: BLE001
                pass
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise  # our own timeout surfaces as TimeoutError, so this is a real shutdown
            status.error = _describe(exc) + (f" (server log: {log_path})" if log_path else "")
            log.warning("MCP server '%s' unavailable: %s", name, status.error)
            return []
        stack.push_async_callback(server_stack.aclose)

        tools: list[Tool] = []
        for remote in remote_tools:
            if cfg.include and not matches_any(remote.name, cfg.include):
                continue
            if matches_any(remote.name, cfg.exclude):
                continue
            tool_name = safe_tool_name(remote.name)
            if tool_name in taken or tool_name in {"remember", "search_memory", "forget_memory"}:
                tool_name = safe_tool_name(f"{name}__{remote.name}")
            schema = dict(remote.input_schema or {})
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            tools.append(
                Tool(
                    name=tool_name,
                    description=(remote.description or remote.title or remote.name).strip(),
                    parameters=schema,
                    handler=self._make_handler(client, remote.name),
                    needs_confirmation=matches_any(remote.name, cfg.confirm),
                    source=f"mcp:{name}",
                )
            )
        status.connected = True
        status.tools = len(tools)
        log.info("MCP server '%s' connected with %d tools", name, len(tools))
        return tools

    @staticmethod
    def _make_handler(client: Client, remote_name: str):
        async def handler(args: dict[str, Any]) -> str:
            result = await client.call_tool(remote_name, args)
            return result_to_text(result)

        return handler


def _describe(exc: BaseException) -> str:
    """Dig the useful message out of anyio exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, TimeoutError):
        return "timed out while connecting"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
