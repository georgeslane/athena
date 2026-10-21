"""Security updates for the MCP servers' dependencies: every [security_updates] every_hours, Athena asks OSV whether
any package locked for a server started with uvx has known vulnerabilities (server_envs.py). For each that has a
fixed version, it updates just that package, to the lowest version that fixes it, restarts the server, which is
installed again from the new lock with the usual checks, and tells you what it did and why. With automatic = false,
or for a vulnerability with no fix yet, it tells you instead, once. The Tools tab shows what was found, with a Fix
button for each server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pi_assistant.server_envs import Audit, Finding

if TYPE_CHECKING:
    from pi_assistant.config import SecurityUpdatesConfig
    from pi_assistant.mcp_manager import MCPManager

log = logging.getLogger(__name__)

FIRST_CHECK_SECONDS = 120.0  # after starting: once the servers are up, and without slowing the start


class ServerSecurity:
    def __init__(
        self,
        cfg: SecurityUpdatesConfig,
        mcp: MCPManager,
        tell: Callable[[str], Awaitable[Any]],
        restart: Callable[[set[str]], Awaitable[None]],
    ):
        self.cfg = cfg
        self.mcp = mcp
        self.tell = tell  # messages you
        self.restart = restart  # restarts servers, once Athena's finished what it's doing
        self.results: dict[str, dict[str, Any]] = {}  # each server's last check, for the dashboard
        self._told: set[tuple[str, str, str]] = set()  # (server, package, why) you've been told about
        self._lock = asyncio.Lock()

    def locked(self) -> list[str]:
        """The servers whose dependencies Athena locks: those started with uvx that have started."""
        return [st.name for st in self.mcp.status() if st.lock in ("reviewed", "first use")]

    async def check(self, names: list[str] | None = None, fix: bool | None = None) -> None:
        """Check these servers, or all of them, and fix what can be fixed if ``fix``, or [security_updates]
        automatic says so. Tells you what was done, and what needs you."""
        if self.mcp.envs is None:
            return
        fix = self.cfg.automatic if fix is None else fix
        async with self._lock:
            news, restart = [], set()
            for name in names or self.locked():
                said, restarted = await self._check(name, fix)
                news += said
                restart |= restarted
            if restart:
                await self.restart(restart)
        for text in news:
            await self.tell(text)

    async def _check(self, name: str, fix: bool) -> tuple[list[str], set[str]]:
        assert self.mcp.envs is not None
        if fix:
            result = await self.mcp.envs.fix(name)
            audit, updated, error = result.after, result.updated, result.error
            before = result.before
        else:
            audit = before = await self.mcp.envs.audit(name)
            updated, error = [], None
        self._record(name, audit, updated, error)
        news = []
        if updated:
            fixed = [f for f in before.findings if f.fixed]
            what = ", ".join(f"{p} {old} → {new}" for p, old, new in updated)
            why = "; ".join(f"{', '.join(f.ids)}: {f.summary}" for f in fixed)
            news.append(
                f"🛡️ I updated {what} for the “{name}” server, to fix known vulnerabilities ({why}). It's restarted."
            )
            log.info("Updated %s for the MCP server '%s'", what, name)
        if error:
            key = (name, "error", error)
            if key not in self._told:
                self._told.add(key)
                news.append(f"⚠️ The “{name}” server has a known vulnerability, and I couldn't update it: {error}")
        for finding in audit.findings if audit.clean is False else []:
            if finding.fixed and fix:
                continue  # it was just updated, or couldn't be: said above
            key = (name, finding.package, ",".join(finding.ids))
            if key in self._told:
                continue
            self._told.add(key)
            news.append(_needs_you(name, finding))
        return news, ({name} if updated else set())

    def _record(self, name: str, audit: Audit, updated: list[tuple[str, str, str]], error: str | None) -> None:
        assert self.mcp.envs is not None
        self.results[name] = {
            "checked": time.time(),
            "clean": audit.clean,
            "summary": audit.summary,
            "findings": [
                {"package": f.package, "version": f.version, "ids": list(f.ids), "summary": f.summary, "fixed": f.fixed}
                for f in audit.findings
            ],
            "updated": [list(u) for u in updated],
            "error": error,
            "fixed": self.mcp.envs.fixed(name),  # what's been updated here, ever
        }

    async def forever(self) -> None:
        await asyncio.sleep(FIRST_CHECK_SECONDS)
        while True:
            if self.cfg.enabled:
                try:
                    await self.check()
                except Exception:  # try again next time
                    log.exception("Couldn't check the MCP servers' dependencies")
            await asyncio.sleep(self.cfg.every_hours * 3600)


def _needs_you(name: str, finding: Finding) -> str:
    about = f"{', '.join(finding.ids)}: {finding.summary}"
    if finding.fixed:
        return (
            f"⚠️ The “{name}” server uses {finding.package} {finding.version}, which has a known vulnerability "
            f"({about}). {finding.fixed} fixes it: press Fix on the server in the dashboard's Tools tab."
        )
    return (
        f"⚠️ The “{name}” server uses {finding.package} {finding.version}, which has a known vulnerability with no "
        f"fixed version yet ({about}). Switch the server off on the Tools tab if that worries you."
    )
