"""Triggers: events from outside that start agents' DAGs, such as you arriving home.

An iPhone Shortcuts automation ("When I arrive home"), a webhook, or anything else on your tailnet that can make
an HTTP request, calls the dashboard's address:

    POST /hooks/arrive-home
    Authorization: Bearer <TRIGGERS_TOKEN>
    {"text": "anything that helps", "items": ["one thing", "another"]}   (both optional; or plain text)

and the DAGs with "arrive-home" in their triggers run as soon as Athena's free (dags/scheduler.py), as they do
for the same event from another DAG's outlets. A task with for_each=trigger runs once for each of the items.
What comes with an event is given to the run as information, never as instructions, and it's limited in size.
The token is its own, not the dashboard's password: it can start DAGs you've made, and nothing else. Wrong
tokens are counted, and too many in a while are refused, as are too many events in an hour.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from collections import deque
from typing import TYPE_CHECKING

from athena.dags.spec import EVENT, MAX_ITEMS
from athena.webserver import HTTPError, Request, Response

if TYPE_CHECKING:
    from athena.config import TriggersConfig
    from athena.dags.scheduler import Dags

log = logging.getLogger(__name__)

PREFIX = "/hooks/"
MIN_TOKEN_CHARS = 16
MAX_TEXT = 4000  # characters of what comes with an event, and of each item
WRONG_TRIES, WRONG_WINDOW = 10, 600.0  # wrong tokens allowed in WRONG_WINDOW seconds, after which it waits


class Hooks:
    def __init__(self, cfg: TriggersConfig, scheduler: Dags | None):
        self.cfg = cfg
        self.scheduler = scheduler
        self._wrong: deque[float] = deque()
        self._taken: deque[float] = deque()

    @property
    def token(self) -> str:
        """[triggers] token, or TRIGGERS_TOKEN from .env if config.toml doesn't say."""
        return self.cfg.token or os.environ.get("TRIGGERS_TOKEN", "")

    @property
    def on(self) -> bool:
        return len(self.token) >= MIN_TOKEN_CHARS and self.scheduler is not None

    def handle(self, request: Request) -> Response:
        """Answer a request to /hooks/<event>."""
        if not self.on:
            why = (
                "agents' DAGs are switched off"
                if self.scheduler is None
                else f"TRIGGERS_TOKEN isn't set in .env, or is shorter than {MIN_TOKEN_CHARS} characters"
            )
            raise HTTPError(404, f"Triggers are off: {why}.")
        assert self.scheduler is not None
        if request.method != "POST":
            raise HTTPError(405, "Use POST.")
        now = time.monotonic()
        _forget(self._wrong, now, WRONG_WINDOW)
        if len(self._wrong) >= WRONG_TRIES:
            raise HTTPError(429, "Too many wrong tokens. Try again in a few minutes.")
        given = request.headers.get("authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {self.token}".encode()):
            self._wrong.append(now)
            log.warning("Triggers: a request with the wrong token")
            raise HTTPError(401, "That's not the token.")
        name = request.path.removeprefix(PREFIX)
        if not EVENT.match(name):
            raise HTTPError(400, "Name the event in lower case, with hyphens between words, like arrive-home.")
        _forget(self._taken, now, 3600.0)
        if len(self._taken) >= self.cfg.max_per_hour:
            raise HTTPError(429, f"That's {self.cfg.max_per_hour} events this hour, the most [triggers] allows.")
        text, items = _given(request)
        waiting = self.scheduler.waiting_for().get(name)
        if not waiting:
            raise HTTPError(404, f'No agent\'s DAG starts on “{name}”. Add triggers=["{name}"] to one first.')
        self._taken.append(now)
        started = self.scheduler.fire(name, text, items)
        said = "Started." if started else "Its DAGs started on it in the last minute, or are about to, so not again."
        return Response(
            json.dumps({"event": name, "started": started, "items": len(items), "said": said}, ensure_ascii=False),
            "application/json",
        )


def _forget(times: deque[float], now: float, window: float) -> None:
    while times and now - times[0] > window:
        times.popleft()


def _given(request: Request) -> tuple[str, list[str]]:
    """What came with the event: {"text": ..., "items": [...]} as JSON, or the body as plain text. Tidied and
    limited."""
    raw = request.body.decode("utf-8", errors="replace").strip()
    items: list[str] = []
    if raw.startswith("{"):
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPError(400, 'Send JSON like {"text": "...", "items": ["..."]}, or plain text.') from None
        body = body if isinstance(body, dict) else {}
        raw = str(body.get("text") or "")
        given = body.get("items")
        if given is not None and not isinstance(given, list):
            raise HTTPError(400, 'Send items as a list, like {"items": ["one thing", "another"]}.')
        if given and len(given) > MAX_ITEMS:
            raise HTTPError(400, f"That's {len(given)} items: send at most {MAX_ITEMS}.")
        items = [text for i in given or [] if (text := _tidy(i if isinstance(i, str) else json.dumps(i)))]
    return _tidy(raw), items


def _tidy(raw: str) -> str:
    text = "".join(c for c in raw if c.isprintable() or c in "\n\t").strip()
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + " [cut short]"
