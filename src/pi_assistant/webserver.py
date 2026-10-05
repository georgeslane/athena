"""A small HTTP/1.1 server for the assistant's own endpoints: Siri's and the status API's.

Standard library only. Each connection carries one request, which is read in full,
within limits on its size and how long it may take, answered, and closed.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from urllib.parse import parse_qsl

MAX_REQUEST_BYTES = 16_384  # per line, and for the body
MAX_DISCARD_BYTES = 1_048_576  # how much of a too-long body is read before saying so
MAX_HEADERS = 64
READ_TIMEOUT_SECONDS = 10.0
REASONS = {
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


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]  # the first value of each parameter
    headers: dict[str, str]  # names in lower case
    body: bytes


@dataclass
class Response:
    body: str
    content_type: str = "text/plain; charset=utf-8"


class Server:
    """Listens on ``host:port`` and answers each request with ``handle()``, which subclasses provide."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.requested_port = port
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """The port it's listening on, which is chosen by the system if 0 was asked for."""
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, self.host, self.requested_port, limit=MAX_REQUEST_BYTES)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            with contextlib.suppress(AttributeError):  # Python 3.13+
                self._server.close_clients()
            await self._server.wait_closed()

    async def handle(self, request: Request) -> Response:
        raise HTTPError(404, "Not found.")

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                status, response = 200, await self.handle(await read_request(reader, writer))
            except HTTPError as exc:
                status, response = exc.status, Response(exc.text)
            except (ValueError, asyncio.IncompleteReadError):  # malformed, too long, or cut short
                status, response = 400, Response("Bad request.")
            body = response.body.encode()
            head = (
                f"HTTP/1.1 {status} {REASONS[status]}\r\n"
                f"Content-Type: {response.content_type}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\n"
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


async def read_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Request:
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
    path, _, query = target.partition("?")
    params: dict[str, str] = {}
    for name, value in parse_qsl(query):
        params.setdefault(name, value)
    return Request(method, path, params, headers, body)


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
