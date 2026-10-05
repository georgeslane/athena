"""The status API: what the assistant is doing, for the status board (pi-display-microservice).

One request, ``GET /v1/status``, answered with JSON:

    {
      "api": 1,                     # changes only if the format changes incompatibly
      "version": "3f9a1c2e-17",     # changes whenever anything below does
      "now": 1759651200.5,          # this machine's clock, to measure the times against
      "assistant": {"name": "Athena", "timezone": "Europe/London"},
      "status": {"state": "working", "task": "...", "step": "Using fetch", ...}
    }

"status" holds every field of status.Snapshot. New fields may be added at any level,
so readers should ignore ones they don't know.

The board asks with ``?wait=25&after=<version>``: if the version is still the same,
the answer waits up to 25 seconds (at most 30) for something to change. So the board
hears about each change straight away, while asking only about twice a minute when
nothing happens. Without ``after``, or with a different version, it answers at once.

It listens on this machine only, unless ``[display] host`` says otherwise, and then it
needs a token. Each process that runs the assistant serves it if the port is free, so
`pi-assistant chat` shows on the board when the bot isn't running.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import errno
import hmac
import ipaddress
import json
import logging
import secrets

from pi_assistant.config import DisplayConfig
from pi_assistant.status import Snapshot, StatusTracker
from pi_assistant.webserver import HTTPError, Request, Response, Server

log = logging.getLogger(__name__)

API_VERSION = 1
MAX_WAIT_SECONDS = 30.0


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # a hostname
        return False


class StatusServer(Server):
    def __init__(self, cfg: DisplayConfig, tracker: StatusTracker, *, name: str, timezone: str):
        super().__init__(cfg.host, cfg.port)
        self.cfg = cfg
        self.tracker = tracker
        self.assistant = {"name": name, "timezone": timezone}
        self._expected = f"Bearer {cfg.token}".encode() if cfg.token else None
        self._boot = secrets.token_hex(4)  # so versions from before a restart never match
        self._changes = 0
        self._snapshot = Snapshot()
        self._changed = asyncio.Event()
        self._running = False

    @property
    def version(self) -> str:
        return f"{self._boot}-{self._changes}"

    async def start(self) -> bool:
        """Start serving, unless the config forbids it or another process has the port. True if it started."""
        if not is_loopback(self.cfg.host) and not self.cfg.token:
            log.error("Status API not started: it needs [display] token to listen on %s", self.cfg.host)
            return False
        try:
            await super().start()
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:  # e.g. the bot is running and this is `pi-assistant chat`
                log.info("Status API not started: port %d is in use, so the board shows that process", self.cfg.port)
            else:
                log.warning("Status API not started: %s", exc)
            return False
        self._running = True
        self._publish(dataclasses.replace(self.tracker.snapshot(), updated=self.tracker.clock()))
        self.tracker.on_change = self._publish
        log.info("Status API listening on %s:%d", self.cfg.host, self.port)
        return True

    async def stop(self) -> None:
        if self._running:
            self.tracker.on_change = None
            self._running = False
            self._changed.set()  # so requests that are waiting end now, not when their wait runs out
        await super().stop()

    def _publish(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot
        self._changes += 1
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def payload(self) -> dict:
        return {
            "api": API_VERSION,
            "version": self.version,
            "now": self.tracker.clock(),
            "assistant": self.assistant,
            "status": dataclasses.asdict(self._snapshot),
        }

    async def handle(self, request: Request) -> Response:
        if request.path != "/v1/status":
            raise HTTPError(404, "Not found. The status is at /v1/status.")
        if request.method != "GET":
            raise HTTPError(405, "Use GET.")
        if self._expected and not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), self._expected
        ):
            raise HTTPError(401, "Wrong or missing token. Use the one in Athena's [display] token.")
        try:
            wait = min(max(float(request.query.get("wait", 0)), 0.0), MAX_WAIT_SECONDS)
        except ValueError:
            raise HTTPError(400, "wait must be a number of seconds.") from None
        if wait and request.query.get("after") == self.version and self._running:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), wait)
        return Response(json.dumps(self.payload(), ensure_ascii=False), "application/json")
