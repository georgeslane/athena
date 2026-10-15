"""Alerts: Athena tells you in Telegram when something it needs stops working, and when it's back.

Every [alerts] every_minutes it checks:
  - the model server (on your Mac), and the embeddings server, answer
  - each MCP server that's switched on is connected, and each built-in integration started
  - there's room left on the drive its data is on

Once something has failed after_failures checks in a row, so a blip doesn't bother you, you
get one message about it, and another when it works again.

When Athena itself stops, it can't tell you: deploy/pi-assistant-stopped.service does that
(scripts/tell-stopped.py), which systemd starts when Athena gives up restarting.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pi_assistant.integrations import BUILTINS

if TYPE_CHECKING:
    from pi_assistant.app import Services
    from pi_assistant.config import AlertsConfig

log = logging.getLogger(__name__)

CHECK_SECONDS = 20.0  # how long each check may take
FIRST_CHECK_SECONDS = 90.0  # after starting: MCP servers take a while to connect
# What's checked, by a key for each thing: (what it is, what's wrong with it, or None if nothing).
Health = dict[str, tuple[str, str | None]]


class Problem(Exception):
    """Something a check found wrong, said as it should be shown."""


async def _answers(check: Callable[[], Awaitable[object]]) -> str | None:
    """None if ``check`` works in time, else why not."""
    try:
        await asyncio.wait_for(check(), CHECK_SECONDS)
    except TimeoutError:
        return f"it didn't answer within {CHECK_SECONDS:g} seconds"
    except Problem as exc:
        return str(exc)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return None


async def check_health(s: Services) -> Health:
    """How everything Athena needs is, now."""
    cfg = s.config
    health: Health = {}

    async def model() -> None:
        models = await s.llm.list_models()
        if cfg.llm.model not in models:
            raise Problem(f"it doesn't have the model {cfg.llm.model}")

    async def embeddings() -> None:
        await s.memory.embedder.embed_one("Are you there?", "query")

    model_problem, embeddings_problem = await asyncio.gather(_answers(model), _answers(embeddings))
    health["model"] = ("The model server", model_problem)
    if cfg.embeddings.base_url != cfg.llm.base_url or embeddings_problem != model_problem:
        health["embeddings"] = ("The embeddings server", embeddings_problem)  # said once if they're one server
    for st in s.mcp.status():
        problem = st.error if not st.connected else None
        health[f"mcp:{st.name}"] = (f"The MCP server “{st.name}”", problem)
    for kind in BUILTINS:
        if kind.wanted(cfg):
            health[f"builtin:{kind.entry.id}"] = (kind.entry.title, s.builtins.problems.get(kind.entry.id))
    data = cfg.resolve(cfg.data_dir)
    try:
        free = shutil.disk_usage(data).free / 1e9
    except OSError as exc:
        health["disk"] = ("The drive Athena's data is on", f"couldn't check it: {exc}")
    else:
        low = f"only {free:.1f} GB is free" if free < s.config.alerts.min_free_gb else None
        health["disk"] = ("The drive Athena's data is on", low)
    return health


class HealthMonitor:
    """Checks how things are now and then, and tells you what's stopped working, and what's back."""

    def __init__(
        self,
        cfg: AlertsConfig,
        check: Callable[[], Awaitable[Health]],
        tell: Callable[[str], Awaitable[bool]],
    ):
        self.cfg = cfg
        self.check = check
        self.tell = tell  # sends you a message: False if it couldn't
        self.failing: dict[str, int] = {}  # checks failed in a row, by key
        self.told: dict[str, str] = {}  # what you've been told isn't working: key -> what it is
        self._loop: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._loop is None:
            self._loop = asyncio.create_task(self._forever(), name="alerts")

    async def stop(self) -> None:
        if self._loop:
            self._loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop
            self._loop = None

    async def _forever(self) -> None:
        await asyncio.sleep(FIRST_CHECK_SECONDS)
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # a bug here shouldn't stop the checks
                log.exception("Couldn't check how things are")
            await asyncio.sleep(self.cfg.every_minutes * 60)

    async def check_once(self) -> None:
        health = await self.check()
        broke: list[tuple[str, str, str]] = []  # (key, what, why)
        fixed: list[tuple[str, str]] = []  # (key, what)
        for key, (what, problem) in health.items():
            if problem:
                self.failing[key] = self.failing.get(key, 0) + 1
                if self.failing[key] >= self.cfg.after_failures and key not in self.told:
                    broke.append((key, what, problem))
            else:
                self.failing.pop(key, None)
                if key in self.told:
                    fixed.append((key, what))
        for key in set(self.told) - set(health):  # not checked any more, such as a server you've removed
            self.told.pop(key)
        for key in set(self.failing) - set(health):
            self.failing.pop(key)
        if not broke and not fixed:
            return
        for _, what, why in broke:
            log.warning("Alert: %s isn't working: %s", what, why)
        lines = [f"⚠️ {what} isn't working: {why}" for _, what, why in broke]
        lines += [f"✅ {what} is working again." for _, what in fixed]
        if await self.tell("\n\n".join(lines)):
            self.told.update((key, what) for key, what, _ in broke)
            for key, _ in fixed:
                self.told.pop(key, None)
