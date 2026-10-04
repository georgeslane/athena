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
import contextlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs

from pi_assistant.config import SiriConfig

log = logging.getLogger(__name__)

AskFn = Callable[[str], Awaitable[str]]

# Added to the context block of questions from Siri, because the reply is read aloud.
VOICE_NOTE = (
    "This message was spoken to Siri, and your reply will be read aloud. Keep it to a few short "
    "sentences of plain text, without Markdown, lists, links or emoji."
)

MIN_TOKEN_CHARS = 16
MAX_REQUEST_BYTES = 16_384  # per line, and for the body
MAX_DISCARD_BYTES = 1_048_576  # how much of a too-long body is read before saying so
MAX_HEADERS = 64
READ_TIMEOUT_SECONDS = 10.0
_REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Content Too Large",
    500: "Internal Server Error",
}


class HTTPError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status
        self.text = text


class SiriServer:
    """Answers POST /ask with ``ask(prompt)``, or with ``still_working()`` if that takes too long."""

    def __init__(self, cfg: SiriConfig, ask: AskFn, still_working: Callable[[], str]):
        if len(cfg.token) < MIN_TOKEN_CHARS:
            raise ValueError(f"its token must be at least {MIN_TOKEN_CHARS} characters (SIRI_TOKEN in .env)")
        self.cfg = cfg
        self.ask = ask
        self.still_working = still_working
        self._expected = f"Bearer {cfg.token}".encode()
        self._server: asyncio.Server | None = None
        # Answers in progress. They carry on after Siri gives up, and are posted to Telegram.
        self._answering: set[asyncio.Task[str]] = set()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.cfg.host, self.cfg.port, limit=MAX_REQUEST_BYTES)
        log.info("Siri endpoint listening on %s:%d", self.cfg.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            with contextlib.suppress(AttributeError):  # Python 3.13+
                self._server.close_clients()
            await self._server.wait_closed()
        for task in self._answering:
            task.cancel()
        await asyncio.gather(*self._answering, return_exceptions=True)

    # -- requests ---------------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                status, text = 200, await self._route(reader, writer)
            except HTTPError as exc:
                status, text = exc.status, exc.text
            except (ValueError, asyncio.IncompleteReadError):  # malformed, too long, or cut short
                status, text = 400, "Bad request."
            body = text.encode()
            head = (
                f"HTTP/1.1 {status} {_REASONS[status]}\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(head.encode() + body)
            await writer.drain()
        except (ConnectionError, TimeoutError):
            pass  # the other end went away or was too slow to send its request
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _route(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
        method, path, headers, body = await self._read_request(reader, writer)
        if path == "/health":
            if method != "GET":
                raise HTTPError(405, "Use GET.")
            return "ok"
        if path != "/ask":
            raise HTTPError(404, "Not found.")
        if method != "POST":
            raise HTTPError(405, "Use POST.")
        if not hmac.compare_digest(headers.get("authorization", "").encode(), self._expected):
            log.warning("Siri: refused a request with a missing or wrong token")
            raise HTTPError(401, "Athena didn't recognise this shortcut. Check the token in its Authorization header.")
        prompt = _prompt_from(body, headers.get("content-type", ""))
        if not prompt:
            raise HTTPError(400, "I didn't catch a question.")
        return await self._answer(prompt)

    async def _read_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> tuple[str, str, dict[str, str], bytes]:
        async with asyncio.timeout(READ_TIMEOUT_SECONDS):
            method, target, version = (await reader.readline()).decode("latin-1").rstrip("\r\n").split(" ")
            if not version.startswith("HTTP/1."):
                raise HTTPError(400, "Bad request.")
            headers: dict[str, str] = {}
            while line := (await reader.readline()).decode("latin-1").rstrip("\r\n"):
                name, colon, value = line.partition(":")
                if not colon or len(headers) >= MAX_HEADERS:
                    raise HTTPError(400, "Bad request.")
                headers[name.strip().lower()] = value.strip()
            body = await _read_body(reader, writer, headers)
        return method, target.split("?", 1)[0], headers, body

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


async def _read_body(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, headers: dict[str, str]) -> bytes:
    if headers.get("expect", "").lower() == "100-continue":
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        await writer.drain()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = b""
        while size := int((await reader.readline()).split(b";")[0].strip(), 16):
            if len(body) + size > MAX_REQUEST_BYTES:
                raise HTTPError(413, "That's too long for me.")
            body += await reader.readexactly(size)
            await reader.readline()  # the line break after each chunk
        while (await reader.readline()).strip():  # trailers, if any
            pass
        return body
    length = int(headers.get("content-length", "0"))
    if length < 0:
        raise ValueError("negative Content-Length")
    if length > MAX_REQUEST_BYTES:
        # Read it anyway, up to a point: closing with data still arriving would reset the
        # connection, and the client would never see this reply.
        with contextlib.suppress(asyncio.IncompleteReadError):
            await reader.readexactly(min(length, MAX_DISCARD_BYTES))
        raise HTTPError(413, "That's too long for me.")
    return await reader.readexactly(length)


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
