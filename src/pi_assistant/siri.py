"""Ask the assistant from Siri: a small HTTP endpoint that an Apple Shortcut calls.

The shortcut sends what you said, with a token. The answer comes back as plain text for
Siri to read out, and the Telegram bot posts both the question and the answer to your
chat. If the answer takes longer than Siri will wait, Siri says so and the answer
arrives in Telegram when it's ready.

It handles two requests: POST /ask, and GET /health to check it's up. It listens on
localhost, and `tailscale serve` makes it reachable from your own devices and nothing
else (see README, "Siri").
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs

from pi_assistant.config import SiriConfig
from pi_assistant.webserver import HTTPError, Request, Response, Server

log = logging.getLogger(__name__)

AskFn = Callable[[str], Awaitable[str]]

# Added to the context block of questions from Siri, because the reply is read aloud.
VOICE_NOTE = (
    "This message was spoken to Siri, and your reply will be read aloud. Keep it to a few short "
    "sentences of plain text, without Markdown, lists, links or emoji."
)

MIN_TOKEN_CHARS = 16


class SiriServer(Server):
    """Answers POST /ask with ``ask(prompt)``, or with ``still_working()`` if that takes too long."""

    def __init__(self, cfg: SiriConfig, ask: AskFn, still_working: Callable[[], str]):
        if len(cfg.token) < MIN_TOKEN_CHARS:
            raise ValueError(f"its token must be at least {MIN_TOKEN_CHARS} characters (SIRI_TOKEN in .env)")
        super().__init__(cfg.host, cfg.port)
        self.cfg = cfg
        self.ask = ask
        self.still_working = still_working
        self._expected = f"Bearer {cfg.token}".encode()
        # Answers in progress. They carry on after Siri gives up, and are posted to Telegram.
        self._answering: set[asyncio.Task[str]] = set()

    async def start(self) -> None:
        await super().start()
        log.info("Siri endpoint listening on %s:%d", self.cfg.host, self.port)

    async def stop(self) -> None:
        await super().stop()
        for task in self._answering:
            task.cancel()
        await asyncio.gather(*self._answering, return_exceptions=True)

    # -- requests ---------------------------------------------------------------------------

    async def handle(self, request: Request) -> Response:
        if request.path == "/health":
            if request.method != "GET":
                raise HTTPError(405, "Use GET.")
            return Response("ok")
        if request.path != "/ask":
            raise HTTPError(404, "Not found.")
        if request.method != "POST":
            raise HTTPError(405, "Use POST.")
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), self._expected):
            log.warning("Siri: refused a request with a missing or wrong token")
            raise HTTPError(401, "Athena didn't recognise this shortcut. Check the token in its Authorization header.")
        prompt = _prompt_from(request.body, request.headers.get("content-type", ""))
        if not prompt:
            raise HTTPError(400, "I didn't catch a question.")
        return Response(await self._answer(prompt))

    async def _answer(self, prompt: str) -> str:
        started = time.monotonic()
        task = asyncio.create_task(self.ask(prompt))
        self._answering.add(task)
        task.add_done_callback(self._finished)
        try:
            answer = await asyncio.wait_for(asyncio.shield(task), self.cfg.reply_timeout_seconds)
        except TimeoutError:
            log.info("Siri: no answer after %.0fs, so it will arrive in Telegram", self.cfg.reply_timeout_seconds)
            return self.still_working()
        except Exception:  # already logged by _finished
            raise HTTPError(500, "Sorry, something went wrong. Try asking in Telegram.") from None
        log.info("Siri: answered in %.1fs", time.monotonic() - started)
        return answer

    def _finished(self, task: asyncio.Task[str]) -> None:
        self._answering.discard(task)
        if not task.cancelled() and task.exception():
            log.error("Siri: answering failed", exc_info=task.exception())


def _prompt_from(body: bytes, content_type: str) -> str:
    """The question, from a JSON body ({"prompt": ...}), a form, or plain text."""
    text = body.decode("utf-8")
    kind = content_type.split(";")[0].strip().lower()
    if kind == "application/json":
        data = json.loads(text)
        prompt = data.get("prompt") if isinstance(data, dict) else None
    elif kind == "application/x-www-form-urlencoded":
        prompt = (parse_qs(text).get("prompt") or [""])[0]
    else:
        prompt = text
    return prompt.strip() if isinstance(prompt, str) else ""
