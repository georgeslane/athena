"""The dashboard: a web page for what Athena is doing, how much it's used, and which tools it has.

The bot serves it, on the Pi only unless [dashboard] host says otherwise, and Tailscale
Serve passes requests to it from your own devices (see README, "Dashboard"). The page
itself is public, but everything it shows or changes needs you to sign in with the
dashboard's password, DASHBOARD_TOKEN in .env. Signing in sets a cookie that lasts 90
days; changing the password signs every device out.

The page talks to Athena with JSON:

  GET    /api/overview          status, usage statistics, memory and connections
  GET    /api/status            what Athena is doing; ?wait=25&after=<version> waits for a change
  POST   /api/session           start a new session
  POST   /api/forget            {"confirm": "forget"}: delete every memory
  GET    /api/tools             every tool, with its settings
  POST   /api/tools/<id>        change one: {"enabled", "fields", "secrets", "tools"}
  POST   /api/tools             add an MCP server
  DELETE /api/tools/<id>        remove a server that isn't one of the recommended ones
  POST   /api/reconnect         read config.toml again and reconnect every MCP server
  POST   /api/login             {"password": ...}, and /api/logout

Requests that change something must send JSON, from the dashboard's own page. A web page
elsewhere can't, so it can't use your cookie to change anything.
"""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import gzip
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import deque
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pi_assistant.config import Config
from pi_assistant.settings import EnvFile, Running, Settings, SettingsError
from pi_assistant.status import StatusFeed
from pi_assistant.status_api import wait_seconds
from pi_assistant.webserver import HTTPError, Request, Response, Server

if TYPE_CHECKING:
    from pi_assistant.app import Services

log = logging.getLogger(__name__)

WEB = Path(__file__).parent / "web"
ASSETS = Path(__file__).parent / "assets"
FILES = {  # what the page is made of, by path
    "/": (WEB / "index.html", "text/html; charset=utf-8"),
    "/dashboard.css": (WEB / "dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": (WEB / "dashboard.js", "text/javascript; charset=utf-8"),
    "/manifest.webmanifest": (WEB / "manifest.webmanifest", "application/manifest+json"),
    "/athena.svg": (ASSETS / "athena.svg", "image/svg+xml"),
    "/icon-180.png": (WEB / "icon-180.png", "image/png"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "manifest-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
COOKIE = "athena"
SESSION_SECONDS = 90 * 86400
MIN_TOKEN_CHARS = 12
SIGN_IN_TRIES = 10  # wrong passwords allowed in SIGN_IN_WINDOW, after which it waits
SIGN_IN_WINDOW = 600.0
MODEL_CHECK_SECONDS = 60.0


class DashboardServer(Server):
    def __init__(self, services: Services):
        cfg = services.config.dashboard
        super().__init__(cfg.host, cfg.port)
        self.s = services
        self.cfg = cfg
        self.feed = StatusFeed(services.status)
        self.settings = Settings(services.config.path, services.config.env_path)
        self._files: dict[str, tuple[bytes, bytes, str]] = {}  # path: (body, gzipped, etag)
        self._failures: deque[float] = deque()
        self._lock = asyncio.Lock()  # one change at a time
        self._pending: tuple[Config | None, bool] | None = None  # a change waiting to be applied
        self._applier: asyncio.Task[None] | None = None
        self._apply_error: str | None = None
        self._model: dict[str, Any] = {"reachable": None, "context_window": None, "error": None, "checked": 0.0}
        self._model_check: asyncio.Task[None] | None = None
        self._running = False

    # -- starting and stopping ------------------------------------------------------------------

    async def start(self) -> bool:
        """Start serving, unless there's no password or another process has the port. True if it started."""
        if not self.cfg.token:
            self._make_token()
        if len(self.cfg.token) < MIN_TOKEN_CHARS:
            log.error(
                "Dashboard not started: its password, DASHBOARD_TOKEN in .env, must be at least %d characters",
                MIN_TOKEN_CHARS,
            )
            return False
        try:
            await super().start()
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:  # e.g. the bot is running and this is `pi-assistant chat`
                log.info("Dashboard not started: port %d is in use", self.cfg.port)
            else:
                log.warning("Dashboard not started: %s", exc)
            return False
        self._running = True
        self.feed.start()
        self._check_model()  # so it's known when the page first asks
        log.info("Dashboard on http://%s:%d", self.cfg.host, self.port)
        return True

    async def stop(self) -> None:
        self.feed.stop()
        for task in (self._applier, self._model_check):
            if task and not task.done():
                task.cancel()
        if self._running:
            await super().stop()
            self._running = False

    def _make_token(self) -> None:
        """Make a password, and keep it in .env, if Athena's config came from a file there's a .env beside."""
        if self.s.config.path is None:
            return
        token = secrets.token_urlsafe(18)
        try:
            EnvFile(self.s.config.env_path).update({"DASHBOARD_TOKEN": token})
        except (OSError, SettingsError) as exc:
            log.warning("Couldn't save a password for the dashboard in .env: %s", exc)
            return
        self.cfg.token = token
        log.warning("Made a password for the dashboard. It's DASHBOARD_TOKEN in %s", self.s.config.env_path)

    # -- requests ---------------------------------------------------------------------------------

    async def handle(self, request: Request) -> Response:
        try:
            response = await self._route(request)
        except HTTPError as exc:
            if request.path.startswith("/api/"):
                response = _json({"error": exc.text}, exc.status)
            else:
                response = Response(exc.text, status=exc.status)
        except Exception:
            log.exception("Dashboard: %s %s failed", request.method, request.path)
            response = _json({"error": "Something went wrong. Athena's log says what."}, 500)
        response.headers = {**SECURITY_HEADERS, **response.headers}
        return response

    async def _route(self, request: Request) -> Response:
        path, method = request.path, request.method
        if path == "/health":
            return Response("ok")
        if path in FILES:
            if method != "GET":
                raise HTTPError(405, "Use GET.")
            return self._file(path, request)
        if not path.startswith("/api/"):
            raise HTTPError(404, "Not found.")
        if method != "GET":
            _check_same_origin(request)
        if path == "/api/login" and method == "POST":
            return self._sign_in(request)
        if path == "/api/logout" and method == "POST":
            return _json(
                {"ok": True}, headers={"Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"}
            )
        if not self._signed_in(request):
            raise HTTPError(401, "Sign in first.")
        return await self._api(request, path, method)

    async def _api(self, request: Request, path: str, method: str) -> Response:
        if path == "/api/overview" and method == "GET":
            return _json(self.overview())
        if path == "/api/status" and method == "GET":
            await self.feed.wait(request.query.get("after"), wait_seconds(request))
            return _json(self.status())
        if path == "/api/session" and method == "POST":
            session = self.s.new_session()
            log.info("Dashboard: started session %d", session.id)
            return _json(self.overview())
        if path == "/api/forget" and method == "POST":
            if _body(request).get("confirm") != "forget":
                raise HTTPError(400, 'To delete every memory, send {"confirm": "forget"}.')
            async with self._lock:
                await self.s.forget_everything()
            log.warning("Dashboard: deleted every memory and the conversation history")
            return _json(self.overview())
        if path == "/api/tools" and method == "GET":
            return _json(self.tools())
        if path == "/api/tools" and method == "POST":
            body = _body(request)
            return await self._change(lambda: self.settings.add_server(body)[1])
        if path == "/api/reconnect" and method == "POST":
            self._apply(None, reconnect=True)
            return _json(self.tools())
        if path.startswith("/api/tools/"):
            name = path.removeprefix("/api/tools/")
            if method == "POST":
                body = _body(request)
                offered = [t for st in self.s.mcp.status() if st.name == name for t, _ in st.offered]
                return await self._change(lambda: self.settings.update(name, body, offered))
            if method == "DELETE":
                return await self._change(lambda: self.settings.remove_server(name))
        raise HTTPError(404 if method == "GET" else 405, "There's nothing like that here.")

    async def _change(self, change: Any) -> Response:
        async with self._lock:
            try:
                cfg = change()
            except SettingsError as exc:
                raise HTTPError(400, str(exc)) from None
        self._apply(cfg)
        return _json(self.tools())

    def _apply(self, cfg: Config | None, reconnect: bool = False) -> None:
        """Apply a change in the background, after any being applied now. The page asks how it went."""
        self._pending = (cfg, reconnect or bool(self._pending and self._pending[1]))
        self._apply_error = None
        if self._applier is None or self._applier.done():
            self._applier = asyncio.create_task(self._apply_pending(), name="dashboard-apply")

    async def _apply_pending(self) -> None:
        while self._pending:
            (cfg, reconnect), self._pending = self._pending, None
            try:
                await self.s.reload(cfg, reconnect=reconnect)
            except Exception as exc:
                log.exception("Dashboard: couldn't apply a change")
                self._apply_error = f"Couldn't apply the change: {exc}"

    # -- what the page shows --------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {"version": self.feed.version, "now": time.time(), "status": dataclasses.asdict(self.feed.snapshot)}

    def overview(self) -> dict[str, Any]:
        cfg, history = self.s.config, self.s.history
        self._check_model()
        session = history.session
        stats = self.s.stats
        counts = self.s.memory.store.count()
        servers = self.s.mcp.status()
        return {
            "assistant": {
                "name": cfg.agent.assistant_name,
                "user": cfg.agent.user_name,
                "timezone": cfg.agent.timezone,
            },
            **self.status(),
            "session": {"id": session.id, "started": session.started, **(stats.summary(session.id) if stats else {})},
            "total": {"sessions": history.sessions_started(), **(stats.summary() if stats else {})},
            "model": {"name": cfg.llm.model, **self._model},
            "memory": {
                "facts": counts.get("fact", 0),
                "documents": counts.get("document", 0),
                "conversations": counts.get("conversation", 0),
            },
            "tools": {
                "count": len(self.s.tools.all()),
                "servers": len(servers),
                "connected": sum(1 for st in servers if st.connected),
                "applying": self._applying,
            },
        }

    def tools(self) -> dict[str, Any]:
        running = Running(
            servers={st.name: (st.connected, st.error, st.offered) for st in self.s.mcp.status()},
            builtins=self.s.builtins.running(),
            applying=self._applying,
        )
        try:
            listed = self.settings.describe(running)
            problem = None
        except SettingsError as exc:
            listed, problem = [], str(exc)
        return {
            "editable": self.settings.editable,
            "applying": self._applying,
            "error": self._apply_error or problem,
            "tools": listed,
        }

    @property
    def _applying(self) -> bool:
        return self.s.applying or bool(self._applier and not self._applier.done())

    def _check_model(self) -> None:
        """Ask the model server how it is, in the background, at most once a minute."""
        stale = time.time() - self._model["checked"] > MODEL_CHECK_SECONDS
        if stale and (self._model_check is None or self._model_check.done()):
            self._model_check = asyncio.create_task(self._ask_model(), name="dashboard-model-check")

    async def _ask_model(self) -> None:
        llm, name = self.s.llm, self.s.config.llm.model
        try:
            models = await asyncio.wait_for(llm.list_models(), 10)
            window = await asyncio.wait_for(llm.context_window(), 10)
            error = None if name in models else f"{name} isn't one of the models it has"
            self._model.update(reachable=True, context_window=window, error=error)
        except Exception as exc:  # the Mac may be asleep
            reason = "it took too long to answer" if isinstance(exc, TimeoutError) else type(exc).__name__
            window = self.s.config.llm.context_window or self._model["context_window"]
            self._model.update(reachable=False, context_window=window, error=f"Can't reach it: {reason}")
        self._model["checked"] = time.time()

    # -- signing in -------------------------------------------------------------------------------

    def _signing_key(self) -> bytes:
        return hashlib.sha256(b"athena-dashboard\0" + self.cfg.token.encode()).digest()

    def _cookie(self) -> str:
        expires, nonce = int(time.time()) + SESSION_SECONDS, secrets.token_hex(8)
        signature = hmac.new(self._signing_key(), f"{expires}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return f"{expires}.{nonce}.{signature}"

    def _signed_in(self, request: Request) -> bool:
        bearer = request.headers.get("authorization", "")
        if bearer and hmac.compare_digest(bearer.encode(), f"Bearer {self.cfg.token}".encode()):
            return True  # for scripts, such as curl
        try:
            morsel = SimpleCookie(request.headers.get("cookie", "")).get(COOKIE)
        except CookieError:
            return False
        parts = morsel.value.split(".") if morsel else []
        if len(parts) != 3 or not parts[0].isdigit() or int(parts[0]) < time.time():
            return False
        expected = hmac.new(self._signing_key(), f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(parts[2], expected)

    def _sign_in(self, request: Request) -> Response:
        now = time.monotonic()
        while self._failures and now - self._failures[0] > SIGN_IN_WINDOW:
            self._failures.popleft()
        if len(self._failures) >= SIGN_IN_TRIES:
            raise HTTPError(429, "Too many wrong passwords. Try again in a few minutes.")
        password = _body(request).get("password")
        if not isinstance(password, str) or not hmac.compare_digest(password.encode(), self.cfg.token.encode()):
            self._failures.append(now)
            log.warning("Dashboard: someone gave the wrong password")
            raise HTTPError(401, "That's not the password.")
        secure = "; Secure" if request.headers.get("x-forwarded-proto") == "https" else ""
        cookie = f"{COOKIE}={self._cookie()}; Max-Age={SESSION_SECONDS}; Path=/; HttpOnly; SameSite=Strict{secure}"
        return _json({"ok": True}, headers={"Set-Cookie": cookie})

    # -- the page's files -------------------------------------------------------------------------

    def _file(self, path: str, request: Request) -> Response:
        if path not in self._files:
            body = FILES[path][0].read_bytes()
            etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
            self._files[path] = (body, gzip.compress(body, mtime=0), etag)
        body, zipped, etag = self._files[path]
        headers = {"Cache-Control": "no-cache", "ETag": etag}
        if request.headers.get("if-none-match") == etag:
            return Response(b"", FILES[path][1], 304, headers)
        if "gzip" in request.headers.get("accept-encoding", "") and len(body) > 1024:
            body, headers["Content-Encoding"] = zipped, "gzip"
        return Response(body, FILES[path][1], headers=headers)


def _json(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(json.dumps(payload, ensure_ascii=False), "application/json", status, headers or {})


def _body(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        raise HTTPError(400, "That isn't JSON.") from None
    if not isinstance(body, dict):
        raise HTTPError(400, "Send a JSON object.")
    return body


def _check_same_origin(request: Request) -> None:
    """Refuse changes that don't come from the dashboard's own page, so another site can't make them.

    A page elsewhere can only send JSON here, or use any method but GET and POST, after asking
    permission first, which is never given. Browsers also say where a request came from
    (Sec-Fetch-Site), which pages can't fake; older ones send Origin, which is checked against
    the address the page was opened at.
    """
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if request.method == "POST" and content_type != "application/json":
        raise HTTPError(415, "Send JSON.")
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        if site not in ("same-origin", "none"):
            raise HTTPError(403, "Only the dashboard's own page can do that.")
        return
    origin = request.headers.get("origin")
    hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")} - {None}
    if origin is not None and (origin == "null" or urlsplit(origin).netloc not in hosts):
        raise HTTPError(403, "Only the dashboard's own page can do that.")
