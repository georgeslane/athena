"""Finding MCP servers: the built-in `find_mcp_servers` tool, which searches the official MCP
Registry (registry.modelcontextprotocol.io), so Athena can suggest servers for things you ask
it to do that it can't yet, say from a weekly task.

It only suggests: you add a server yourself, on the dashboard, after reading about it. The
registry lists what publishers send it without checking their code, so being listed isn't an
endorsement, and what a listing says is passed on as information. Searches go only to the
registry, with only the words searched for, so they don't ask first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

import httpx

from pi_assistant.builtin import FAIL, OK, Builtin, Report, Section
from pi_assistant.catalog import Integration
from pi_assistant.tools import Tool, ToolError

if TYPE_CHECKING:
    from pi_assistant.config import Config

MAX_RESULTS = 10
DESCRIPTION_CHARS = 300


class MCPRegistryConfig(Section):
    enabled: bool = True
    url: str = "https://registry.modelcontextprotocol.io"
    timeout_seconds: float = 60.0  # it can take half a minute to answer


class MCPRegistry(Builtin):
    section = "mcp_registry"
    Settings = MCPRegistryConfig
    entry = Integration(
        "mcp_registry",
        "Finding MCP servers",
        "Searches the official MCP Registry, so Athena can suggest servers for things it can't do yet.",
        "mcp_registry",
        builtin=True,
        asks="Never asks: searches go only to the MCP Registry. Athena only suggests servers: you add them.",
    )

    def __init__(self, cfg: MCPRegistryConfig, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.http = http or httpx.AsyncClient(
            timeout=cfg.timeout_seconds, headers={"User-Agent": "pi-assistant (Athena)"}
        )

    @classmethod
    def start(cls, cfg: Config) -> Self:
        return cls(cfg.mcp_registry)

    @classmethod
    async def check(cls, cfg: Config, report: Report, http: httpx.AsyncClient | None = None) -> None:
        registry = cls(cfg.mcp_registry, http)
        try:
            found = await registry.search("time", 3)
        except ToolError as exc:
            report(FAIL, str(exc))
        else:
            report(OK, f"the registry answers ({len(found)} servers for “time”)")
        finally:
            await registry.close()

    async def close(self) -> None:
        await self.http.aclose()

    def tools(self) -> list[Tool]:
        async def find_mcp_servers(args: dict[str, Any]) -> str:
            query = str(args.get("query") or "").strip()
            if not query:
                raise ToolError("Say what to search for, such as “calendar” or “weather”.")
            try:
                limit = int(args.get("limit") or 5)
            except (TypeError, ValueError):
                limit = 5
            found = await self.search(query, limit)
            if not found:
                return f"The MCP Registry has no servers for “{query}”."
            return "\n\n".join(self._describe(server) for server in found)

        return [
            Tool(
                name="find_mcp_servers",
                description=(
                    "Search the official MCP Registry for MCP servers, to suggest ones that would let you do "
                    "something you can't yet. The user adds them on the dashboard, so say what each would let "
                    "you do and why it looks trustworthy. Listings aren't checked by anyone: prefer known "
                    "publishers and PyPI packages, which Athena locks and checks for malware."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What it should do, in a word or two"},
                        "limit": {"type": "integer", "description": f"How many (default 5, at most {MAX_RESULTS})"},
                    },
                    "required": ["query"],
                },
                handler=find_mcp_servers,
            )
        ]

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {"search": query, "limit": 20, "version": "latest"}  # more can fail
        try:
            response = await self.http.get(f"{self.cfg.url.rstrip('/')}/v0/servers", params=params)
            response.raise_for_status()
            entries = response.json().get("servers") or []
        except (httpx.HTTPError, ValueError) as exc:
            raise ToolError(f"The MCP Registry didn't answer ({type(exc).__name__}).") from exc
        found = []
        for entry in entries:
            status = ((entry.get("_meta") or {}).get("io.modelcontextprotocol.registry/official") or {}).get("status")
            server = entry.get("server") or {}
            if server.get("name") and status in (None, "active"):
                found.append(server)
        return found[: max(1, min(limit, MAX_RESULTS))]

    def _describe(self, server: dict[str, Any]) -> str:
        """One server, in a few lines. What publishers write is only shortened, never acted on."""
        description = " ".join(str(server.get("description") or "").split())
        if len(description) > DESCRIPTION_CHARS:
            description = description[: DESCRIPTION_CHARS - 1].rstrip() + "…"
        name, version = server["name"], server.get("version", "?")
        named = f"{name} (version {version})"
        if server.get("title"):
            named = f"{server['title']} ({name}, version {version})"
        lines = [f"{named}: {description}"]
        runs = []
        for package in server.get("packages") or []:
            kind, ident, version = package.get("registryType"), package.get("identifier"), package.get("version")
            if kind == "pypi":
                runs.append(f"uvx {ident}=={version}" if version else f"uvx {ident}")
            elif kind == "npm":
                runs.append(f"npx -y {ident}@{version}" if version else f"npx -y {ident}")
            elif kind:
                runs.append(f"{kind} {ident}")
        for remote in server.get("remotes") or []:
            key = any((h.get("isRequired") or h.get("isSecret")) for h in remote.get("headers") or [])
            runs.append(f"remote: {remote.get('url')}" + (" (needs a key)" if key else ""))
        if runs:
            lines.append("Runs as: " + "; ".join(runs))
        repository = (server.get("repository") or {}).get("url") or server.get("websiteUrl")
        if repository:
            lines.append(f"Source: {repository}")
        return "\n".join(lines)
