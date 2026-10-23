"""The dashboard: a web page for what Athena is doing, how much it's used, and which tools it has.

The bot serves it, on the Pi only unless [dashboard] host says otherwise, and Tailscale
Serve passes requests to it from your own devices (see docs/dashboard.md). The page
itself is public, but everything it shows or changes needs you to sign in with the
dashboard's password, DASHBOARD_TOKEN in .env. Signing in sets a cookie that lasts 90
days; changing the password signs every device out.

The page talks to Athena with JSON:

  GET    /api/overview          status, usage statistics, memory and connections
  GET    /api/status            what Athena is doing; ?wait=25&after=<version> waits for a change
  GET    /api/agents            your agents, the master agent first, then the latest to have something said in
                                them, whether Athena's answering you, and what's waiting for your OK; POST {"name"}
                                makes a new one
  GET    /api/agents/<id>       an agent's messages: the last page, or ?before=<message id>, and its settings
  POST   /api/agents/<id>       change any of {"name", "model", "muted", "guidance", "context"}
  DELETE /api/agents/<id>       delete an agent, but the master: what was said to it, its DAG, runs and files
  POST   /api/agents/<id>/messages  {"text", "allow": [tools], "attachments": [{"name", "data": base64}]}: send a
                                message or a command; /api/agents/stop stops the answer
  POST   /api/agents/<id>/tools {"allowed": [tools]}: the tools that may run without asking when you talk to it
  GET    /api/agents/<id>/files the files that come with its guidance; POST {"files": [{"name", "data": base64}]}
                                adds some, and GET or DELETE /files/<name> downloads or deletes one
  GET    /api/agents/<id>/dag   its DAG: the file, its tasks, when it runs, and what's proposed for it; POST
                                {"source"} saves a new file, /dag/check {"source"} checks one as it's written, and
                                /dag/run, /dag/pause, /dag/resume and /dag/stop do what they say
  GET    /api/agents/<id>/runs  its last 10 runs, newest first
  GET    /api/runs/<id>         a run: what each of its tasks was given, said and did, and what it proposed
  POST   /api/proposals/<id>    {"approve": true | false}: what to do with a change a run proposed
  GET    /api/models            the models the model server has, for an agent to use
  GET    /api/photos/<name>     a photo sent in a message
  POST   /api/approvals/<id>    {"allow": true | false}: answer a request for your OK, here or in Telegram; or
                                {"reply": "..."}: answer a task's question ({"allow": false} skips it)
  POST   /api/presence          {"looking": bool}: whether you're looking at the page, so devices aren't notified
  GET    /api/push              the key a device needs for notifications; POST {"subscription"} turns them on
                                for one, /api/push/off {"endpoint"} turns them off, /api/push/test sends one
  POST   /api/thinking          {"mode": "on" | "short" | "off"}: how much the model thinks before answering
  POST   /api/forget            {"confirm": "forget"}: delete every memory
  GET    /api/tools             every tool, with its settings
  POST   /api/tools/<id>        change one: {"enabled", "fields", "secrets", "tools"}
  POST   /api/tools             add an MCP server
  DELETE /api/tools/<id>        remove a server that isn't one of the recommended ones
  POST   /api/tools/<id>/approve  let the model use tools held back because they changed: {"tools": {name: fingerprint}}
  POST   /api/tools/on-demand   {"tools": [names]}: the tools the model finds when it needs them, not in every prompt
  POST   /api/reconnect         read config.toml again and reconnect every MCP server
  GET    /api/memory            what's in memory, the documents indexed from your Mac, and suggested memories
  POST   /api/memory/search     {"query", "kind"}: search memories and documents by meaning and by words
  POST   /api/memory/suggestions/<id>  {"action": "approve" | "discard", "text"}: a memory a task suggested
  GET    /api/skills            skills, and what Athena's suggested
  POST   /api/skills            add one: {"name", "description", "body"}
  POST   /api/skills/<id>       change one: {"name", "description", "body"}, or {"restore": version id}
  DELETE /api/skills/<id>       delete one
  GET    /api/skills/<id>/versions  every version of one, newest first
  POST   /api/skills/suggestions/<id>  {"action": "approve" | "discard", "name", "description", "body"}
  GET    /api/documents/browse  ?path=: the folders inside a folder on your Mac, to choose from
  POST   /api/documents/folders {"add": path} or {"remove": path}: the folders to index
  POST   /api/documents/index   index them now, and /api/documents/stop to stop
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
import inspect
import json
import logging
import re
import secrets
import time
from collections import deque
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from athena.approvals import Approval
from athena.config import Config
from athena.config_files import EnvFile
from athena.dags.api import AgentsAPI
from athena.documents import DocumentsError
from athena.memory import KINDS, relevance
from athena.push import Push
from athena.settings import Running, Settings, SettingsError, hand_written
from athena.status import StatusFeed
from athena.status_api import wait_seconds
from athena.tool_search import NAMES as META_TOOLS
from athena.tools import matches_any
from athena.triggers import PREFIX as HOOKS
from athena.triggers import Hooks
from athena.webserver import HTTPError, Request, Response, Server
from athena.webserver import json_body as _body
from athena.webserver import json_response as _json

if TYPE_CHECKING:
    from athena.app import Services
    from athena.tool_approvals import ToolChange

log = logging.getLogger(__name__)

WEB = Path(__file__).parent / "web"
ASSETS = Path(__file__).parent / "assets"
FILES = {  # what the page is made of, by path
    "/": (WEB / "index.html", "text/html; charset=utf-8"),
    "/dashboard.css": (WEB / "dashboard.css", "text/css; charset=utf-8"),
    # The script, as modules: dashboard.js starts it, core.js, steps.js, md.js, graph.js and notifications.js have
    # what the tabs share, and each tab has its own: the Agents tab's chat, DAG and runs in modules of their own.
    **{
        f"/{name}.js": (WEB / f"{name}.js", "text/javascript; charset=utf-8")
        for name in ("dashboard", "core", "steps", "md", "graph", "notifications")
        + ("status", "agents", "chat", "dag", "runs", "memory", "skills", "tools")
    },
    "/sw.js": (WEB / "sw.js", "text/javascript; charset=utf-8"),  # shows notifications (notifications.js)
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
USED_DAYS = 30  # how far back the Tools tab counts each tool's use
LOOKING_SECONDS = 40.0  # after the page last said you're looking at it, how long you're taken still to be
SENDING = re.compile(r"/api/agents/\d+/(messages|files)")  # a message, which can come with photos and files, or files
MAX_SENDING_BYTES = 40_000_000  # what they're sent as, base64, comes to: about 30 MB of them
SENDING_SECONDS = 300.0  # to send them, on a slow connection


# The API, as (method, path, handler): the first that matches a request answers it. A method of None
# means the handler answers any, and a path's (?P<name>...) parts are passed to it by name.
ROUTES = (
    ("GET", r"/api/overview", "_get_overview"),
    ("GET", r"/api/status", "_get_status"),
    ("GET", r"/api/agents", "_get_agents"),
    ("POST", r"/api/agents", "_new_agent"),
    ("POST", r"/api/agents/stop", "_stop_chat"),
    ("GET", r"/api/agents/(?P<agent>\d+)", "_get_agent"),
    ("POST", r"/api/agents/(?P<agent>\d+)", "_change_agent"),
    ("DELETE", r"/api/agents/(?P<agent>\d+)", "_delete_agent"),
    ("POST", r"/api/agents/(?P<agent>\d+)/messages", "_send"),
    ("POST", r"/api/agents/(?P<agent>\d+)/tools", "_allow_in_session"),
    ("GET", r"/api/agents/(?P<agent>\d+)/files", "_get_files"),
    ("POST", r"/api/agents/(?P<agent>\d+)/files", "_add_files"),
    ("GET", r"/api/agents/(?P<agent>\d+)/files/(?P<name>[^/]+)", "_get_file"),
    ("DELETE", r"/api/agents/(?P<agent>\d+)/files/(?P<name>[^/]+)", "_delete_file"),
    ("GET", r"/api/agents/(?P<agent>\d+)/dag", "_get_dag"),
    ("POST", r"/api/agents/(?P<agent>\d+)/dag", "_save_dag"),
    ("POST", r"/api/agents/(?P<agent>\d+)/dag/check", "_check_dag"),
    ("POST", r"/api/agents/(?P<agent>\d+)/dag/(?P<action>run|pause|resume|stop)", "_dag_action"),
    ("GET", r"/api/agents/(?P<agent>\d+)/runs", "_get_runs"),
    ("GET", r"/api/runs/(?P<run>run_[0-9A-Za-z_]{1,60})", "_get_run"),
    ("POST", r"/api/proposals/(?P<proposal>rec_[0-9a-f]{12})", "_decide"),
    ("GET", r"/api/models", "_get_models"),
    ("POST", r"/api/thinking", "_set_thinking"),
    ("POST", r"/api/forget", "_forget"),
    ("GET", r"/api/photos/(?P<name>[0-9a-f]{32}\.[a-z]+)", "_get_photo"),
    ("POST", r"/api/approvals/(?P<which>[0-9a-f]+)", "_approve"),
    ("POST", r"/api/presence", "_presence"),
    ("GET", r"/api/push", "_get_push"),
    ("POST", r"/api/push", "_push_on"),
    ("POST", r"/api/push/off", "_push_off"),
    ("POST", r"/api/push/test", "_push_test"),
    ("GET", r"/api/tools", "_get_tools"),
    ("POST", r"/api/tools", "_add_server"),
    ("POST", r"/api/reconnect", "_reconnect"),
    ("POST", r"/api/tools/on-demand", "_set_on_demand"),
    ("POST", r"/api/tools/(?P<name>[^/]+)/approve", "_approve_tools"),
    ("POST", r"/api/tools/(?P<name>[^/]+)/fix", "_fix_server"),
    ("POST", r"/api/tools/(?P<name>[^/]+)", "_change_server"),
    ("DELETE", r"/api/tools/(?P<name>[^/]+)", "_remove_server"),
    ("GET", r"/api/memory", "_get_memory"),
    ("POST", r"/api/memory/search", "_search_memory"),
    ("POST", r"/api/memory/suggestions/(?P<which>.*)", "_decide_memory"),
    ("POST", r"/api/privacy", "_set_private_words"),
    ("POST", r"/api/privacy/check", "_check_privacy"),
    (None, r"/api/skills(?:/(?P<which>.*))?", "_skills"),
    (None, r"/api/documents/(?P<action>.*)", "_documents"),
)
_ROUTES = [(method, re.compile(path), handler) for method, path, handler in ROUTES]


class DashboardServer(AgentsAPI, Server):
    def __init__(self, services: Services):
        cfg = services.config.dashboard
        super().__init__(cfg.host, cfg.port)
        self.s = services
        self.cfg = cfg
        self.feed = StatusFeed(services.status)
        self.settings = Settings(services.config.path, services.config.env_path)
        # /hooks/<event> (triggers.py): events that start agents' DAGs.
        self.hooks = Hooks(services.config.triggers, services.dags if services.config.tasks.enabled else None)
        self._files: dict[str, tuple[bytes, bytes, str]] = {}  # path: (body, gzipped, etag)
        self._failures: deque[float] = deque()
        self._lock = asyncio.Lock()  # one change at a time
        self._pending: tuple[Config | None, bool] | None = None  # a change waiting to be applied
        self._applier: asyncio.Task[None] | None = None
        self._apply_error: str | None = None
        self._model: dict[str, Any] = {"reachable": None, "context_window": None, "error": None, "checked": 0.0}
        self._model_check: asyncio.Task[None] | None = None
        self._running = False
        self._looking_until = 0.0  # while you're looking at the page, your devices aren't notified

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
            if exc.errno == errno.EADDRINUSE:  # e.g. the bot is running and this is `athena chat`
                log.info("Dashboard not started: port %d is in use", self.cfg.port)
            else:
                log.warning("Dashboard not started: %s", exc)
            return False
        self._running = True
        self.feed.start()
        # The page follows your conversation, the chat's replies and requests for your OK as it does the status.
        self.s.history.listeners.append(self.feed.poke)
        self.s.chat.listeners.append(self.feed.poke)
        self.s.approvals.listeners.append(self._approvals_changed)
        self._check_model()  # so it's known when the page first asks
        log.info("Dashboard on http://%s:%d", self.cfg.host, self.port)
        return True

    def _approvals_changed(self, approval: Approval) -> None:
        self.feed.poke()

    async def stop(self) -> None:
        for listeners, listener in (
            (self.s.history.listeners, self.feed.poke),
            (self.s.chat.listeners, self.feed.poke),
            (self.s.approvals.listeners, self._approvals_changed),
        ):
            if listener in listeners:
                listeners.remove(listener)
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

    def body_limit(self, method: str, path: str, headers: dict[str, str]) -> tuple[int, float]:
        """A message in the chat can come with photos and files, once you've signed in."""
        if method == "POST" and SENDING.fullmatch(path) and self._signed_in(Request(method, path, {}, headers, b"")):
            return MAX_SENDING_BYTES, SENDING_SECONDS
        return super().body_limit(method, path, headers)

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
        if path.startswith(HOOKS):  # events that start tasks: their own token, not the dashboard's password
            return await self.hooks.handle(request)
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
        """Answer an API request with the first route in ROUTES that matches it (see there)."""
        for wanted, pattern, handler in _ROUTES:
            match = pattern.fullmatch(path)
            if match and wanted in (None, method):
                parts = {name: part.strip("/") for name, part in match.groupdict(default="").items()}
                response = getattr(self, handler)(request, method, **parts)
                return await response if inspect.isawaitable(response) else response
        raise HTTPError(404 if method == "GET" else 405, "There's nothing like that here.")

    # -- the API's routes, as ROUTES lists them: each is given the request, its method and the parts of its path

    def _get_overview(self, request: Request, method: str) -> Response:
        return _json(self.overview())

    async def _get_status(self, request: Request, method: str) -> Response:
        if request.query.get("looking") == "1":  # the page is in front of you, as it waits for news
            self._looking_until = time.time() + LOOKING_SECONDS  # it asks again within 25 seconds while it is
        await self.feed.wait(request.query.get("after"), wait_seconds(request))
        return _json(self.status())

    @property
    def looking(self) -> bool:
        """Whether you're looking at the dashboard, on any device: then it shows what's new, and your devices aren't
        notified of it."""
        return time.time() < self._looking_until

    def _presence(self, request: Request, method: str) -> Response:
        """{"looking": false} when you switch away from the page, so your devices are notified again at once."""
        looking = _body(request).get("looking")
        if not isinstance(looking, bool):
            raise HTTPError(400, 'Send {"looking": true} or {"looking": false}.')
        self._looking_until = time.time() + LOOKING_SECONDS if looking else 0.0
        return _json({"looking": looking})

    def _the_push(self) -> Push:
        if self.s.push is None:
            raise HTTPError(409, "Notifications aren't set up.")
        return self.s.push

    def _get_push(self, request: Request, method: str) -> Response:
        """The key a device needs to take Athena's notifications, and how many devices do."""
        push = self._the_push()
        return _json({"key": push.public_key, "devices": push.subscribed()})

    def _push_on(self, request: Request, method: str) -> Response:
        """{"subscription": as the browser gives it}: notify this device from now on."""
        push = self._the_push()
        origin = request.headers.get("origin", "")
        subject = origin if origin.startswith("https://") else "mailto:athena@localhost"  # who's sending, for Apple
        try:
            push.subscribe(_body(request).get("subscription"), subject)
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None
        log.info("Dashboard: notifications on for a device")
        return _json({"devices": push.subscribed()})

    def _push_off(self, request: Request, method: str) -> Response:
        """{"endpoint"}: stop notifying a device."""
        push = self._the_push()
        endpoint = _body(request).get("endpoint")
        if not isinstance(endpoint, str):
            raise HTTPError(400, 'Send {"endpoint": the subscription\'s endpoint}.')
        push.unsubscribe(endpoint)
        return _json({"devices": push.subscribed()})

    async def _push_test(self, request: Request, method: str) -> Response:
        """{"endpoint"}: send a device a notification now, to see that they work."""
        push = self._the_push()
        endpoint = _body(request).get("endpoint")
        if not isinstance(endpoint, str) or not push.subscribed(endpoint):
            raise HTTPError(400, "That device doesn't take notifications: turn them on first.")
        name = self.s.config.agent.assistant_name
        said = f"Notifications are on. You'll hear when {name} answers you, a task has news, or it needs your OK."
        if not await push.send(name, said, url="#agents", tag="test", only=endpoint):
            raise HTTPError(502, "The push service didn't take it. Turn notifications off and on again.")
        return _json({"sent": True})

    def _set_thinking(self, request: Request, method: str) -> Response:
        try:
            self.s.set_thinking(_body(request).get("mode"))
        except (ValueError, SettingsError) as exc:
            raise HTTPError(400, str(exc)) from exc
        return _json(self.overview())

    async def _forget(self, request: Request, method: str) -> Response:
        if _body(request).get("confirm") != "forget":
            raise HTTPError(400, 'To delete every memory, send {"confirm": "forget"}.')
        async with self._lock:
            await self.s.forget_everything()
        log.warning("Dashboard: deleted every memory and the conversation history")
        return _json(self.overview())

    def _get_tools(self, request: Request, method: str) -> Response:
        return _json(self.tools())

    async def _add_server(self, request: Request, method: str) -> Response:
        body = _body(request)
        return await self._change(lambda: self.settings.add_server(body)[1])

    def _reconnect(self, request: Request, method: str) -> Response:
        self._apply(None, reconnect=True)
        return _json(self.tools())

    def _approve_tools(self, request: Request, method: str, name: str) -> Response:
        tools = _body(request).get("tools")
        if not isinstance(tools, dict) or not tools or not all(isinstance(v, str) for v in tools.values()):
            raise HTTPError(400, 'Say which tools to approve: {"tools": {"name": "fingerprint"}}.')
        try:
            self.s.approve_tools(name, tools)
        except ValueError as exc:
            raise HTTPError(409, str(exc)) from None
        return _json(self.tools())

    async def _set_on_demand(self, request: Request, method: str) -> Response:
        names = _body(request).get("tools")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise HTTPError(400, 'Say which tools to find when they\'re needed: {"tools": ["name", ...]}.')
        known = [t.name for t in self.s.tools.all()]
        return await self._change(lambda: self.settings.set_on_demand(names, known))

    async def _fix_server(self, request: Request, method: str, name: str) -> Response:
        """Check a server's dependencies now, and update any with known vulnerabilities that have a fix."""
        if name not in self.s.security.locked():
            raise HTTPError(404, "Only servers started with uvx have dependencies Athena locks, and so can fix.")
        await self.s.security.check([name], fix=True)
        return _json(self.tools())

    async def _change_server(self, request: Request, method: str, name: str) -> Response:
        body = _body(request)
        offered = [t for st in self.s.mcp.status() if st.name == name for t, _ in st.offered]
        return await self._change(lambda: self.settings.update(name, body, offered))

    async def _remove_server(self, request: Request, method: str, name: str) -> Response:
        response = await self._change(lambda: self.settings.remove_server(name))
        if self.s.mcp.approvals:
            self.s.mcp.approvals.forget(name)  # a server added later with this name starts afresh
        return response

    def _get_photo(self, request: Request, method: str, name: str) -> Response:
        photo = self.s.history.photos.load(name)
        if photo is None:
            raise HTTPError(404, "That photo isn't kept any more.")
        # Named by what's in it, so it never changes: kept by your browser only, not anything in between.
        return Response(photo.data, photo.mime, headers={"Cache-Control": "private, max-age=31536000, immutable"})

    def _approve(self, request: Request, method: str, which: str) -> Response:
        """{"allow": true or false}: answer a request for your OK; or {"reply": "..."}: a task's question."""
        body = _body(request)
        reply, allow = body.get("reply"), body.get("allow")
        if isinstance(reply, str) and reply.strip():
            answered = self.s.approvals.reply(which, reply)
        elif isinstance(allow, bool):
            answered = self.s.approvals.answer(which, allow)
        else:
            raise HTTPError(400, 'Send {"allow": true}, {"allow": false} or {"reply": "your answer"}.')
        if not answered:
            raise HTTPError(409, "That's been answered already, or it's expired.")
        log.info("Dashboard: answered %s", "a question" if reply else "a request for your OK")
        return _json(self.s.chat.state())

    def _get_memory(self, request: Request, method: str) -> Response:
        return _json(self.memory())

    async def _search_memory(self, request: Request, method: str) -> Response:
        return _json(await self.search(_body(request)))

    async def _decide_memory(self, request: Request, method: str, which: str) -> Response:
        return _json(await self._memory_suggestion(which, _body(request)))

    def _set_private_words(self, request: Request, method: str) -> Response:
        try:
            self.s.privacy.set_terms(_body(request).get("terms"))
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None
        return _json(self.privacy())

    def _check_privacy(self, request: Request, method: str) -> Response:
        text = _body(request).get("text")
        if not isinstance(text, str) or len(text) > 20_000:
            raise HTTPError(400, 'Send the text to check: {"text": "..."}, up to 20,000 characters.')
        found = self.s.privacy.find([text])
        return _json({"findings": [{"kind": f.kind, "text": "" if f.secret else f.text} for f in found]})

    async def _memory_suggestion(self, which: str, body: dict[str, Any]) -> dict[str, Any]:
        memory = self.s.memory
        if not which.isdigit():
            raise HTTPError(404, "There's no suggestion like that.")
        action, text = body.get("action"), body.get("text")
        if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > 2000):
            raise HTTPError(400, "Say what to remember, in up to 2,000 characters.")
        if action == "discard":
            if not memory.store.drop_suggestion(int(which)):
                raise HTTPError(404, "There's no suggestion like that: it may have been saved or discarded already.")
        elif action == "approve":
            try:
                await memory.approve(int(which), text)
            except KeyError:
                raise HTTPError(
                    404, "There's no suggestion like that: it may have been saved or discarded already."
                ) from None
            except Exception as exc:
                log.warning("Dashboard: couldn't remember a suggestion: %s", exc)
                raise HTTPError(
                    502, f"Couldn't save it: is the embeddings server ({memory.embedder.cfg.base_url}) up?"
                ) from None
        else:
            raise HTTPError(400, 'Say what to do: {"action": "approve" | "discard"}.')
        return self.memory()

    def _skills(self, request: Request, method: str, which: str) -> Response:
        skills = self.s.skills
        try:
            if not which and method == "GET":
                return _json(self.skills())
            if not which and method == "POST":
                body = _skill_body(request)
                skills.save(None, body["name"], body["description"], body["body"], by="you")
                return _json(self.skills())
            if which.startswith("suggestions/") and method == "POST":
                number = which.removeprefix("suggestions/")
                if not number.isdigit():
                    raise HTTPError(404, "There's no suggestion like that.")
                body = _body(request)
                if body.get("action") == "discard":
                    skills.reject(int(number))
                elif body.get("action") == "approve":
                    edited = {k: body[k] for k in ("name", "description", "body") if isinstance(body.get(k), str)}
                    skills.approve(int(number), edited or None)
                else:
                    raise ValueError('Say what to do: {"action": "approve" | "discard"}.')
                return _json(self.skills())
            number, _, rest = which.partition("/")
            if not number.isdigit():
                raise HTTPError(404, "There's no skill like that.")
            skill_id = int(number)
            if rest == "versions" and method == "GET":
                if skills.store.get(skill_id) is None:
                    raise HTTPError(404, "There's no skill like that.")
                return _json({"versions": [skills.describe_version(v) for v in skills.store.versions(skill_id)]})
            if rest:
                raise HTTPError(404, "There's nothing like that here.")
            if method == "DELETE":
                skills.delete(skill_id)
                return _json(self.skills())
            if method == "POST":
                body = _body(request)
                if "restore" in body:
                    if not isinstance(body["restore"], int):
                        raise ValueError("Say which version to go back to, by its id.")
                    skills.restore(skill_id, body["restore"])
                else:
                    fields = _skill_body(request)
                    skills.save(skill_id, fields["name"], fields["description"], fields["body"], by="you")
                return _json(self.skills())
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None
        raise HTTPError(405, "There's nothing like that here.")

    async def _documents(self, request: Request, method: str, action: str) -> Response:
        documents = self.s.documents
        try:
            if action == "browse" and method == "GET":
                return _json(await documents.browse(request.query.get("path", "")))
            if action == "folders" and method == "POST":
                body = _body(request)
                async with self._lock:
                    folders = _changed_folders(list(self.s.config.documents.folders), body)
                    try:
                        cfg = self.settings.set_values({"documents.folders": folders})
                    except SettingsError as exc:
                        raise HTTPError(400, str(exc)) from None
                self.s.config.documents = cfg.documents
                documents.configure(cfg.documents)
                return _json(self.memory())
            if action == "index" and method == "POST":
                documents.start()
                return _json(self.memory())
            if action == "stop" and method == "POST":
                await documents.stop()
                return _json(self.memory())
        except DocumentsError as exc:
            status = 502 if action == "browse" else 409 if action == "index" and documents.running else 400
            raise HTTPError(status, str(exc)) from None
        raise HTTPError(404 if method == "GET" else 405, "There's nothing like that here.")

    async def search(self, body: dict[str, Any]) -> dict[str, Any]:
        query = str(body.get("query") or "").strip()
        kind = body.get("kind") or None
        if not query or len(query) > 500:
            raise HTTPError(400, "Say what to look for, in up to 500 characters.")
        if kind not in (None, *KINDS):
            raise HTTPError(400, f"kind must be one of {', '.join(KINDS)}.")
        memory = self.s.memory
        try:
            found = await memory.find(query, 12, kind)
        except Exception as exc:
            log.warning("Dashboard search failed: %s", exc)
            raise HTTPError(
                502, f"Couldn't search: is the embeddings server ({memory.embedder.cfg.base_url}) up?"
            ) from None
        return {
            "query": query,
            "results": [
                {
                    "kind": f.kind,
                    "text": f.text,
                    "path": memory.show_path(f.path) if f.path else None,
                    "date": f.saved,
                    "relevance": relevance(f),  # None if found by its words alone
                    "words": f.words,
                    "id": f.id,
                }
                for f in found
            ],
        }

    def memory(self) -> dict[str, Any]:
        counts = self.s.memory.store.count()
        return {
            "editable": self.settings.editable,
            "facts": counts.get("fact", 0),
            "conversations": counts.get("conversation", 0),
            "documents": self.s.documents.summary(),
            # Facts Athena wanted to remember while working by itself, waiting for you to approve them.
            "suggestions": [dataclasses.asdict(s) for s in self.s.memory.store.suggestions()],
            "privacy": self.privacy(),
        }

    def privacy(self) -> dict[str, Any]:
        """Your private words, which tools can't send where nothing about you should go (privacy.py)."""
        return {
            "terms": self.s.privacy.store.all(),
            "automatic": self.s.privacy.automatic(),
            "block": self.s.config.privacy.block,
        }

    def skills(self) -> dict[str, Any]:
        skills = self.s.skills
        return {
            "skills": [skills.describe(s) for s in skills.store.all()],
            "suggestions": [skills.describe_suggestion(s) for s in skills.store.suggestions()],
            "reviews": self.s.config.tasks.enabled,  # skills are looked back on after they're used
        }

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
        chat = self.s.chat
        return {
            "version": self.feed.version,
            "now": time.time(),
            "status": dataclasses.asdict(self.feed.snapshot),
            # Enough for the page to know when to ask for more: the Agents tab's badge, and whether to fetch it again.
            "chat": {
                "master": self.s.history.master.id,
                "busy": chat.busy,
                "approvals": len(self.s.approvals.pending()),
                "last": self._last_message(),
            },
        }

    def _last_message(self) -> dict[str, Any] | None:
        """The newest message in any session, so the page can say when Athena's written."""
        last = self.s.history.last_message()
        return {"id": last[0], "role": last[1], "session": last[2]} if last else None

    def overview(self) -> dict[str, Any]:
        cfg, history = self.s.config, self.s.history
        self._check_model()
        session = history.master
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
            "model": {
                "name": cfg.llm.model,
                "thinking": cfg.llm.thinking_now,
                "short_thinking_tokens": cfg.llm.short_thinking_tokens,
                **self._model,
            },
            "memory": {
                "facts": counts.get("fact", 0),
                "documents": len(self.s.memory.store.document_sources()),
                "conversations": counts.get("conversation", 0),
            },
            "tools": {
                "count": len(self.s.tools.all()),
                "servers": len(servers),
                "connected": sum(1 for st in servers if st.connected),
                "applying": self._applying,
            },
            # What Athena suggested while working by itself, waiting for you.
            "review": {
                "memories": len(self.s.memory.store.suggestions()),
                "skills": len(self.s.skills.store.suggestions()),
            },
        }

    def tools(self) -> dict[str, Any]:
        servers = self.s.mcp.status()
        running = Running(
            servers={st.name: (st.connected, st.error, st.offered) for st in servers},
            builtins=self.s.builtins.status(),
            applying=self._applying,
            held={st.name: [_held(c) for c in st.pending] for st in servers if st.pending},
            security=self.s.security.results,
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
            "on_demand": self.on_demand(),
        }

    def on_demand(self) -> dict[str, Any]:
        """Every tool the model has, how much it adds to every prompt, how much it's used, and whether it's found
        when it's needed instead (tool_search.py)."""
        registry = self.s.tools
        since = time.time() - USED_DAYS * 86400
        used = self.s.stats.tool_calls_since(since) if self.s.stats else {}
        patterns = [p for p in registry.on_demand if hand_written(p)]
        tools = [
            {
                "name": t.name,
                "from": t.source.removeprefix("mcp:") if t.source.startswith("mcp:") else "Athena",
                "tokens": _tokens(t.schema()),
                "used": used.get(t.name, 0),
                "on_demand": matches_any(t.name, registry.on_demand),
                "pattern": next((p for p in patterns if matches_any(t.name, [p])), None),  # set in config.toml
            }
            for t in registry.all()
        ]
        sent = registry.in_prompt()
        return {
            "tools": tools,
            "days": USED_DAYS,
            "in_prompt": sum(_tokens(t.schema()) for t in sent),  # with find_tools and use_tool, if they're there
            "finding": sum(_tokens(t.schema()) for t in sent if t.name in META_TOOLS),  # what those two take
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


def _tokens(schema: dict[str, Any]) -> int:
    """Roughly how many tokens a tool's schema takes in the prompt: about four characters each."""
    return -(-len(json.dumps(schema, ensure_ascii=False)) // 4)


def _skill_body(request: Request) -> dict[str, str]:
    body = _body(request)
    fields = {key: body.get(key) for key in ("name", "description", "body")}
    if not all(isinstance(value, str) for value in fields.values()):
        raise HTTPError(400, "A skill needs a name, when to use it, and how.")
    return fields  # type: ignore[return-value]


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


def _held(change: ToolChange) -> dict[str, Any]:
    """A held-back tool as the page shows it: what it says now, and what it said when you approved it."""
    version = lambda v: {"description": v.description, "parameters": v.parameters}  # noqa: E731
    return {
        "name": change.tool,
        "fingerprint": change.now.fingerprint,
        "now": version(change.now),
        "approved": version(change.approved) if change.approved else None,
    }


def _changed_folders(folders: list[str], body: dict[str, Any]) -> list[str]:
    """The folders to index, with one added or removed. A folder is a path on your Mac: ~/… or /…"""
    if isinstance(body.get("remove"), str):
        if body["remove"] not in folders:
            raise HTTPError(400, f"{body['remove']} isn't one of the folders.")
        return [f for f in folders if f != body["remove"]]
    folder = body.get("add")
    if not isinstance(folder, str) or not folder.strip():
        raise HTTPError(400, 'Send {"add": path} or {"remove": path}.')
    folder = folder.strip().rstrip("/") or "/"
    if (
        not (folder == "~" or folder.startswith(("~/", "/")))
        or len(folder) > 1000
        or any(c in folder for c in "\0\n\r")
    ):
        raise HTTPError(400, "Give the folder's full path on your Mac, such as ~/Documents.")
    if folder in folders:
        raise HTTPError(400, f"{folder} is one of the folders already.")
    return [*folders, folder]
