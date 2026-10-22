"""The dashboard: a web page for what Athena is doing, how much it's used, and which tools it has.

The bot serves it, on the Pi only unless [dashboard] host says otherwise, and Tailscale
Serve passes requests to it from your own devices (see docs/dashboard.md). The page
itself is public, but everything it shows or changes needs you to sign in with the
dashboard's password, DASHBOARD_TOKEN in .env. Signing in sets a cookie that lasts 90
days; changing the password signs every device out.

The page talks to Athena with JSON:

  GET    /api/overview          status, usage statistics, memory and connections
  GET    /api/status            what Athena is doing; ?wait=25&after=<version> waits for a change
  POST   /api/session           start a new session
  POST   /api/thinking          {"mode": "on" | "short" | "off"}: how much the model thinks before answering
  POST   /api/forget            {"confirm": "forget"}: delete every memory
  GET    /api/tools             every tool, with its settings
  POST   /api/tools/<id>        change one: {"enabled", "fields", "secrets", "tools"}
  POST   /api/tools             add an MCP server
  DELETE /api/tools/<id>        remove a server that isn't one of the recommended ones
  POST   /api/tools/<id>/approve  let the model use tools held back because they changed: {"tools": {name: fingerprint}}
  POST   /api/tools/on-demand   {"tools": [names]}: the tools the model finds when it needs them, not in every prompt
  POST   /api/reconnect         read config.toml again and reconnect every MCP server
  GET    /api/tasks             what Athena does by itself, and what a new task can use
  POST   /api/tasks             add one: {"title", "instructions", "schedule", "tools"}
  POST   /api/tasks/<id>        change one: {"title", "instructions", "schedule", "tools"}, or
                                {"action": "run" | "pause" | "resume" | "stop"}
  POST   /api/watches           watch for something: {"what", "until": "YYYY-MM-DD" or null for no end}
  POST   /api/watches/<id>      change it, the same way; /api/watches/check checks on everything now
  DELETE /api/watches/<id>      stop watching it
  DELETE /api/tasks/<id>        delete one
  GET    /api/memory            what's in memory, the documents indexed from your Mac, and suggested memories
  POST   /api/memory/search     {"query", "kind"}: search memories and documents by meaning and by words
  POST   /api/memory/suggestions/<id>  {"action": "approve" | "discard", "text"}: a memory a task suggested
  GET    /api/projects          the board: projects handed to Claude, as epics of stories
  POST   /api/projects/refresh  read them from GitHub now
  POST   /api/projects/restart  {"folder"}: start Claude again on one
  POST   /api/projects/extend   {"folder", "summary", "stories", "notes"}: more work on one, for Claude
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

from athena.config import Config
from athena.config_files import EnvFile
from athena.documents import DocumentsError
from athena.history import Busy
from athena.memory import KINDS, relevance
from athena.settings import Running, Settings, SettingsError, hand_written
from athena.status import StatusFeed
from athena.status_api import wait_seconds
from athena.tasks import EXAMPLES, NEVER_UNASKED, Scheduler
from athena.timeline import KEEP_DAYS, PAGE
from athena.timeline import KINDS as ACTIVITIES
from athena.tool_search import NAMES as META_TOOLS
from athena.tools import ToolError, matches_any
from athena.triggers import PREFIX as HOOKS
from athena.triggers import Hooks
from athena.watching import end_of
from athena.webserver import HTTPError, Request, Response, Server

if TYPE_CHECKING:
    from athena.app import Services
    from athena.tool_approvals import ToolChange

log = logging.getLogger(__name__)

WEB = Path(__file__).parent / "web"
ASSETS = Path(__file__).parent / "assets"
FILES = {  # what the page is made of, by path
    "/": (WEB / "index.html", "text/html; charset=utf-8"),
    "/dashboard.css": (WEB / "dashboard.css", "text/css; charset=utf-8"),
    # The script, as modules: dashboard.js starts it, core.js and steps.js have what the tabs share, and each tab
    # has its own.
    **{
        f"/{name}.js": (WEB / f"{name}.js", "text/javascript; charset=utf-8")
        for name in ("dashboard", "core", "steps", "status", "activity", "tasks", "memory", "skills", "board", "tools")
    },
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


# The API, as (method, path, handler): the first that matches a request answers it. A method of None
# means the handler answers any, and a path's (?P<name>...) parts are passed to it by name.
ROUTES = (
    ("GET", r"/api/overview", "_get_overview"),
    ("GET", r"/api/status", "_get_status"),
    ("POST", r"/api/session", "_new_session"),
    ("POST", r"/api/thinking", "_set_thinking"),
    ("POST", r"/api/forget", "_forget"),
    ("GET", r"/api/activity", "_get_activity"),
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
    (None, r"/api/projects(?:/(?P<action>refresh|restart|extend))?", "_projects"),
    (None, r"/api/skills(?:/(?P<which>.*))?", "_skills"),
    (None, r"/api/documents/(?P<action>.*)", "_documents"),
    (None, r"/api/tasks(?:/(?P<task>.*))?", "_tasks"),
    ("POST", r"/api/watches", "_add_watch"),
    ("POST", r"/api/watches/check", "_check_watches"),
    ("POST", r"/api/watches/(?P<which>\d+)", "_change_watch"),
    ("DELETE", r"/api/watches/(?P<which>\d+)", "_stop_watching"),
)
_ROUTES = [(method, re.compile(path), handler) for method, path, handler in ROUTES]


class DashboardServer(Server):
    def __init__(self, services: Services):
        cfg = services.config.dashboard
        super().__init__(cfg.host, cfg.port)
        self.s = services
        self.cfg = cfg
        self.feed = StatusFeed(services.status)
        self.settings = Settings(services.config.path, services.config.env_path)
        self.hooks = Hooks(services.config.triggers, services.tasks)  # /hooks/<event> (triggers.py)
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
            if exc.errno == errno.EADDRINUSE:  # e.g. the bot is running and this is `athena chat`
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
        if path.startswith(HOOKS):  # events that start tasks: their own token, not the dashboard's password
            return self.hooks.handle(request)
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
        await self.feed.wait(request.query.get("after"), wait_seconds(request))
        return _json(self.status())

    def _new_session(self, request: Request, method: str) -> Response:
        try:
            session = self.s.new_session()
        except Busy as exc:
            raise HTTPError(409, str(exc)) from None
        log.info("Dashboard: started session %d", session.id)
        return _json(self.overview())

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

    def _get_activity(self, request: Request, method: str) -> Response:
        """What Athena did, newest first: ?before=<id> for what came before, ?kinds=message,task for only those."""
        if self.s.timeline is None:
            raise HTTPError(409, "Athena isn't keeping a timeline.")
        before = request.query.get("before")
        kinds = [k for k in request.query.get("kinds", "").split(",") if k]
        if (before is not None and not before.isdigit()) or any(k not in ACTIVITIES for k in kinds):
            raise HTTPError(400, f"Ask with ?before=<an id> and ?kinds= some of {', '.join(ACTIVITIES)}.")
        entries = self.s.timeline.page(int(before) if before else None, kinds, PAGE + 1)
        return _json(
            {"entries": [e.as_dict() for e in entries[:PAGE]], "more": len(entries) > PAGE, "keep_days": KEEP_DAYS}
        )

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

    def _tasks(self, request: Request, method: str, task: str) -> Response:
        scheduler = self.s.tasks
        if scheduler is None:
            raise HTTPError(409, "Tasks are switched off ([tasks] enabled = false in config.toml).")
        try:
            if not task and method == "GET":
                return _json(self.tasks())
            if not task and method == "POST":
                scheduler.add(*_task_fields(_body(request)), created_by="you")
                return _json(self.tasks())
            if not task.isdigit():
                raise HTTPError(404, "There's no task like that.")
            if method == "DELETE":
                scheduler.delete(int(task))
                return _json(self.tasks())
            if method == "POST":
                body = _body(request)
                action = body.get("action")
                if action is None:
                    scheduler.change(int(task), *_task_fields(body))
                elif action == "stop":
                    scheduler.stop_running()
                elif action in ("run", "pause", "resume"):
                    getattr(scheduler, "run_now" if action == "run" else action)(int(task))
                else:
                    raise ValueError('Say what to do: {"action": "run" | "pause" | "resume" | "stop"}.')
                return _json(self.tasks())
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None
        raise HTTPError(405, "There's nothing like that here.")

    def _scheduler(self) -> Scheduler:
        if self.s.tasks is None:
            raise HTTPError(409, "Tasks are switched off ([tasks] enabled = false in config.toml).")
        return self.s.tasks

    def _add_watch(self, request: Request, method: str) -> Response:
        scheduler, body = self._scheduler(), _body(request)
        assert scheduler.watches is not None
        try:
            scheduler.watches.add(str(body.get("what") or ""), self._until(body), by="you")
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None
        scheduler.wake()
        return _json(self.tasks())

    def _change_watch(self, request: Request, method: str, which: str) -> Response:
        scheduler, body = self._scheduler(), _body(request)
        assert scheduler.watches is not None
        if scheduler.watches.get(int(which)) is None:
            raise HTTPError(404, f"Nothing's watched as #{which}.")
        try:
            scheduler.watches.change(int(which), str(body.get("what") or ""), self._until(body))
        except ValueError as exc:
            raise HTTPError(400, str(exc)) from None
        return _json(self.tasks())

    def _until(self, body: dict[str, Any]) -> float | None:
        """When something's watched until, from {"until": "YYYY-MM-DD"}, the end of that day; null for no end."""
        until = body.get("until")
        if until is None:
            return None
        if not isinstance(until, str):
            raise ValueError("Give the day as YYYY-MM-DD, or null for no end.")
        return end_of(until, self._scheduler().tz)

    def _check_watches(self, request: Request, method: str) -> Response:
        self._scheduler().check_now()
        return _json(self.tasks())

    def _stop_watching(self, request: Request, method: str, which: str) -> Response:
        scheduler = self._scheduler()
        assert scheduler.watches is not None
        try:
            scheduler.watches.remove(int(which))
        except ValueError as exc:
            raise HTTPError(404, str(exc)) from None
        return _json(self.tasks())

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

    async def _projects(self, request: Request, method: str, action: str) -> Response:
        projects = self.s.builtins.get("claude_projects")
        if projects is None:
            why = self.s.builtins.problems.get("claude_projects")
            off = f"Projects for Claude can't start: {why}." if why else "Projects for Claude are switched off."
            raise HTTPError(409, f"{off} Set them up on the Tools tab.")
        if action == "" and method == "GET":
            return _json(projects.board())
        if action == "refresh" and method == "POST":
            return _json(await self.s.check_projects() or projects.board())
        if action == "extend" and method == "POST":
            body = _body(request)
            folder = str(body.get("folder") or "")
            project = next((p for p in projects.projects if p.folder == folder and p.pr in ("open", "merged")), None)
            if project is None:
                raise HTTPError(400, f"There's no project {folder} on the board.")
            sent = {k: body.get(k) for k in ("summary", "stories", "notes")}
            verdict = self.s.privacy.check("extend_claude_project", sent, asks=True)
            if verdict.refused:
                raise HTTPError(
                    400, f"That has {verdict.describe()} in it. Take that out: Claude gets only what anyone could ask."
                )
            try:
                await projects.extend(project, body)
            except ToolError as exc:
                raise HTTPError(400, str(exc)) from None
            return _json(projects.board())
        if action == "restart" and method == "POST":
            try:
                await projects.restart(str(_body(request).get("folder") or ""))
            except ToolError as exc:
                raise HTTPError(400, str(exc)) from None
            return _json(projects.board())
        raise HTTPError(405, "There's nothing like that here.")

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
            "reviews": self.s.tasks is not None,  # skills are looked back on after they're used
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

    def tasks(self) -> dict[str, Any]:
        scheduler = self.s.tasks
        assert scheduler is not None
        asks = sorted(t.name for t in self.s.tools.all() if t.needs_confirmation and t.name not in NEVER_UNASKED)
        return {
            "tasks": [scheduler.describe(t) for t in scheduler.store.all()],
            "tools": asks,  # tools that ask first, which a task may be allowed to use without asking
            "examples": EXAMPLES,
            "quiet_minutes": scheduler.cfg.quiet_minutes,
            "watching": scheduler.watching(),
            # Events that start tasks, at /hooks/<event> (triggers.py): which tasks wait for each, and whether
            # the token that lets them in is set.
            "triggers": {"on": self.hooks.on, "events": scheduler.waiting_for()},
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


def _json(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(json.dumps(payload, ensure_ascii=False), "application/json", status, headers or {})


def _tokens(schema: dict[str, Any]) -> int:
    """Roughly how many tokens a tool's schema takes in the prompt: about four characters each."""
    return -(-len(json.dumps(schema, ensure_ascii=False)) // 4)


def _body(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        raise HTTPError(400, "That isn't JSON.") from None
    if not isinstance(body, dict):
        raise HTTPError(400, "Send a JSON object.")
    return body


def _task_fields(body: dict[str, Any]) -> tuple[str, str, str, list[str]]:
    """A task's title, instructions, schedule and the tools it may use without asking, as the page sends them."""
    tools = body.get("tools") or []
    if not isinstance(tools, list):
        raise ValueError("Send the tools it may use without asking as a list of names.")
    title, instructions, schedule = (str(body.get(key) or "") for key in ("title", "instructions", "schedule"))
    return title, instructions, schedule, tools


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
