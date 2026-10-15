"""Projects for Claude: coding work Athena hands to Claude through a private GitHub repo, and a board of it.

Handing a project over (`hand_to_claude`, which always asks you first, showing everything it will push):
  1. Athena makes a branch, `project/<project>-<YYYY-MM-DD>`, from the main branch, and commits one file
     to it: `<project>-<YYYY-MM-DD>/Requirements.md`, a summary, then stories, each with what "done" means.
  2. It opens a pull request from that branch, and labels it [claude_projects] label ("claude").
  3. The label starts Claude: a routine at claude.ai/code/routines with a GitHub trigger on pull requests
     being labelled. deploy/zeus-solutions/CLAUDE.md, at the root of the repo, tells it how to work.

Claude pushes its work to the branch, with `status.json` saying where the project and each story are,
and `Solution.md` when it's done. Athena reads the pull requests every poll_minutes (and when you ask, on
the dashboard), shows each project as an epic of stories on the Board tab, and tells you in Telegram
when one is done, or Claude's stuck. You merge the pull request when you're happy with it.

When Claude can't run: a run that would start while you're at your usage limit is rejected, and one that
reaches it part way stops, and neither is tried again. So Athena watches for it:
  - no status.json start_minutes after the label: Claude didn't start
  - no push to the branch for stalled_minutes: Claude stopped part way
and starts it again by taking the label off and putting it back, waiting twice as long each time (up to
eight times as long), max_restarts times. Claude carries on from what it pushed. You're told the first
time, and if Athena gives up. When Claude's stuck, `answer_claude` adds your answer to Answers.md on the
branch, and starts it again.

The token only needs this one repo (Contents and Pull requests, read and write), so the Pi still can't
push anywhere else, and the repo must be private: Athena won't push to a public one.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from pi_assistant.builtin import FAIL, OK, WARN, Builtin, Report, Section, Unavailable
from pi_assistant.catalog import Field, Integration, Secret
from pi_assistant.tools import Tool, ToolError

if TYPE_CHECKING:
    from pathlib import Path

    from pi_assistant.config import Config

log = logging.getLogger(__name__)

API = "https://api.github.com"
PREFIX = "project/"  # each project's branch
REQUIREMENTS, STATUS, SOLUTION, ANSWERS, GUIDE = (
    "Requirements.md",
    "status.json",
    "Solution.md",
    "Answers.md",
    "CLAUDE.md",
)
HAND_TO_CLAUDE, ANSWER_CLAUDE = "hand_to_claude", "answer_claude"
STATES = ("to do", "in progress", "blocked", "done")  # a story's, on the board
PROJECT_STATES = ("new", "in progress", "blocked", "done")
MAX_STORIES, TITLE_CHARS, ANSWER_CHARS = 20, 80, 4000
_SAME = {  # what Claude might write for each state
    "todo": "to do",
    "to-do": "to do",
    "new": "to do",
    "not started": "to do",
    "in_progress": "in progress",
    "in-progress": "in progress",
    "doing": "in progress",
    "started": "in progress",
    "complete": "done",
    "completed": "done",
    "finished": "done",
    "stuck": "blocked",
}
_FOLDER_DATE = re.compile(r"-(\d{4}-\d{2}-\d{2})(?:-\d+)?$")
_STORY = re.compile(r"^###\s+(S\d+)\s*[:.\-–]\s*(.+?)\s*$")
_DONE_WHEN = re.compile(r"^\**\s*done when\s*:?\s*\**\s*:?\s*$", re.IGNORECASE)
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")


class ClaudeProjectsConfig(Section):
    # Coding projects Athena hands to Claude through a private GitHub repo (see README, "Projects for Claude").
    enabled: bool = False
    repo: str = "your-name/zeus-solutions"  # owner/name, on GitHub
    branch: str = "main"  # where project branches start, and pull requests go
    label: str = "claude"  # labelling a pull request with this starts Claude
    token: str = ""  # a fine-grained token for that repo only: Contents and Pull requests, read and write
    poll_minutes: float = 30.0  # how often the board reads the repo
    start_minutes: float = 30.0  # no status.json this long after the label: Claude didn't start
    stalled_minutes: float = 120.0  # no push for this long: Claude stopped part way
    max_restarts: int = 6  # then Athena gives up, and tells you
    timeout_seconds: float = 30.0


# -- what's in the repo ------------------------------------------------------------------------------


@dataclass
class Story:
    id: str  # S1, S2, ...
    title: str
    details: str = ""
    done_when: list[str] = field(default_factory=list)
    status: str = "to do"
    note: str = ""  # Claude's, from status.json


@dataclass
class Project:
    folder: str  # <project>-<YYYY-MM-DD>
    title: str
    summary: str = ""
    notes: str = ""
    date: str = ""
    stories: list[Story] = field(default_factory=list)
    status: str = "new"
    updated: str = ""  # when Claude last said, from status.json
    progress: str = ""  # Claude's one line on where it's at
    problem: str = ""  # why status.json couldn't be read, if it couldn't
    url: str = ""  # the pull request
    solution_url: str = ""
    number: int = 0  # the pull request's
    pr: str = "open"  # open, merged or closed
    last_push: float = 0.0  # when the branch last changed
    waiting: str = ""  # "not started" or "stopped": Claude isn't working on it, and Athena's restarting it
    restarts: int = 0  # how many times Athena's restarted it since Claude last pushed
    next_restart: float | None = None
    gave_up: bool = False


def slug(title: str) -> str:
    """A folder name for a title: lower case, words joined by hyphens."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    name = "-".join(words)[:50].rstrip("-")
    return name or "project"


def render(title: str, summary: str, stories: list[dict[str, Any]], notes: str = "") -> str:
    """Requirements.md, as Athena writes it: what parse() reads back."""
    lines = [f"# {title.strip()}", "", summary.strip(), "", "## Stories", ""]
    for i, story in enumerate(stories, 1):
        lines.append(f"### S{i}: {' '.join(str(story['title']).split())}")
        lines.append("")
        if str(story.get("details") or "").strip():
            lines += [str(story["details"]).strip(), ""]
        lines.append("**Done when:**")
        lines += [f"- {' '.join(str(c).split())}" for c in story["done_when"]]
        lines.append("")
    if notes.strip():
        lines += ["## Notes", "", notes.strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def parse(folder: str, text: str) -> Project:
    """A project from its Requirements.md. Forgiving: one written by hand still shows on the board."""
    lines = text.splitlines()
    title, summary, notes, section = folder, [], [], ""
    stories: list[Story] = []
    story: Story | None = None
    in_criteria = False
    for line in lines:
        if line.startswith("# ") and title == folder and not section:
            title = line[2:].strip() or folder
            continue
        if line.startswith("## "):
            section, story, in_criteria = line[3:].strip().lower(), None, False
            continue
        match = _STORY.match(line)
        if match:
            story = Story(match.group(1).upper(), match.group(2))
            stories.append(story)
            in_criteria = False
            continue
        if story is not None:
            if _DONE_WHEN.match(line.strip()):
                in_criteria = True
            elif in_criteria and (item := _ITEM.match(line)):
                story.done_when.append(item.group(1))
            elif not in_criteria:
                story.details = f"{story.details}\n{line}" if story.details else line
            continue
        if not section:
            summary.append(line)
        elif section.startswith("note"):
            notes.append(line)
    for s in stories:
        s.details = s.details.strip()
    date = match.group(1) if (match := _FOLDER_DATE.search(folder)) else ""
    return Project(folder, title, "\n".join(summary).strip(), "\n".join(notes).strip(), date, stories)


def state(value: Any, allowed: tuple[str, ...]) -> str:
    said = " ".join(str(value or "").strip().lower().split())
    said = _SAME.get(said, said)
    if said in allowed:
        return said
    return allowed[0]


def apply_status(project: Project, text: str | None) -> None:
    """Add what Claude says in status.json. A project without one is new, or in progress if a story's moved."""
    if text is not None:
        try:
            status = json.loads(text)
            if not isinstance(status, dict):
                raise ValueError("it isn't a JSON object")
        except ValueError as exc:
            project.problem = f"{STATUS} couldn't be read: {exc}"
            status = {}
        stories = status.get("stories") if isinstance(status.get("stories"), dict) else {}
        for story in project.stories:
            said = stories.get(story.id) or stories.get(story.id.lower())
            if isinstance(said, dict):
                story.status = state(said.get("status"), STATES)
                story.note = str(said.get("note") or "").strip()
            elif isinstance(said, str):
                story.status = state(said, STATES)
        project.updated = str(status.get("updated") or "")
        project.progress = str(status.get("summary") or "").strip()
        if status.get("status"):
            project.status = state(status["status"], PROJECT_STATES)
            if project.status == "new":  # status.json means Claude has started
                project.status = "in progress"
            return
    moved = any(s.status != "to do" for s in project.stories)
    if project.stories and all(s.status == "done" for s in project.stories):
        project.status = "done"
    elif any(s.status == "blocked" for s in project.stories):
        project.status = "blocked"
    else:
        project.status = "in progress" if moved or text is not None else "new"


# -- what Athena remembers between reads ---------------------------------------------------------------


@dataclass
class Watch:
    """What Athena knows of a project between reads: what it last told you, and how restarting it is going."""

    status: str = ""
    head: str = ""  # the branch's last commit, last time: a new one means Claude's working
    restarts: int = 0
    restarted: float = 0.0
    told_waiting: bool = False
    gave_up: bool = False


class BoardStore:
    """The last board read, so it shows straight after a restart, and what Athena knows of each project."""

    def __init__(self, db_path: Path):
        from pi_assistant.memory import connect_db  # here: memory needs config, which needs this module

        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS claude_projects (folder TEXT PRIMARY KEY, watch TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS claude_board (id INTEGER PRIMARY KEY CHECK (id = 1), board TEXT NOT NULL);
                """
            )

    def watches(self) -> dict[str, Watch]:
        with self._lock:
            rows = self._conn.execute("SELECT folder, watch FROM claude_projects").fetchall()
        return {folder: Watch(**json.loads(watch)) for folder, watch in rows}

    def keep_watches(self, watches: dict[str, Watch]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO claude_projects VALUES (?, ?) ON CONFLICT(folder) DO UPDATE SET watch = excluded.watch",
                [(folder, json.dumps(asdict(w))) for folder, w in watches.items()],
            )

    def board(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT board FROM claude_board WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else None

    def keep(self, board: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO claude_board VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET board = excluded.board",
                (json.dumps(board, ensure_ascii=False),),
            )

    def close(self) -> None:
        self._conn.close()


# -- the built-in ---------------------------------------------------------------------------------------


class ClaudeProjects(Builtin):
    section = "claude_projects"
    Settings = ClaudeProjectsConfig
    entry = Integration(
        "claude_projects",
        "Projects for Claude",
        "Hands coding projects to Claude through a private GitHub repo, and shows them on the Board tab.",
        "claude_projects",
        builtin=True,
        fields=(
            Field(
                "repo",
                "Repository",
                help="owner/name on GitHub. It must be private, with CLAUDE.md on its main branch.",
                required=True,
                example=True,
            ),
            Field("branch", "Main branch"),
            Field("label", "Label that starts Claude", help="The label your routine's GitHub trigger looks for."),
        ),
        secrets=(
            Secret(
                "PROJECTS_GITHUB_TOKEN",
                "GitHub token",
                "A fine-grained token for that repository only, with Contents and Pull requests: read and write.",
                path="token",
                template="${PROJECTS_GITHUB_TOKEN}",
            ),
        ),
        setup="Set up the repository, its token and Claude's routine first (see README, “Projects for Claude”).",
        asks="Handing a project to Claude, and answering it, always ask first, and show everything they'll push.",
    )

    def __init__(
        self,
        cfg: ClaudeProjectsConfig,
        store: BoardStore,
        timezone: str = "UTC",
        http: httpx.AsyncClient | None = None,
        clock: Any = time.time,
    ):
        self.cfg = cfg
        self.store = store
        self.clock = clock
        try:
            self.tz: Any = ZoneInfo(timezone)
        except Exception:  # unknown timezone name
            self.tz = ZoneInfo("UTC")
        self.http = http or httpx.AsyncClient(
            base_url=API,
            timeout=cfg.timeout_seconds,
            headers={
                "Authorization": f"Bearer {cfg.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pi-assistant (Athena)",
            },
        )
        self._blobs: dict[str, str] = {}  # file contents, by their SHA, so only what's changed is read again
        self._pushed: dict[str, float] = {}  # when each commit was made, by its SHA
        self._private: bool | None = None
        self.checked = 0.0
        self.error: str | None = None
        self.projects: list[Project] = []
        if kept := store.board():
            self.checked, self.error = kept.get("checked", 0.0), kept.get("error")
            self.projects = [_project(p) for p in kept.get("projects", [])]

    @classmethod
    def wanted(cls, cfg: Config) -> bool:
        return bool(cfg.claude_projects.enabled)

    @classmethod
    def start(cls, cfg: Config) -> Self:
        settings = cfg.claude_projects
        if not settings.token:
            raise Unavailable("PROJECTS_GITHUB_TOKEN isn't set in .env")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", settings.repo) or settings.repo.startswith("your-name/"):
            raise Unavailable("say which repository, as owner/name")
        return cls(settings, BoardStore(cfg.db_path), cfg.agent.timezone)

    @classmethod
    async def check(cls, cfg: Config, report: Report, http: httpx.AsyncClient | None = None) -> None:
        try:
            built = cls.start(cfg) if http is None else cls(cfg.claude_projects, BoardStore(cfg.db_path), http=http)
        except Unavailable as exc:
            report(FAIL, str(exc))
            return
        repo, branch = built.cfg.repo, built.cfg.branch
        try:
            about = await built._get(f"/repos/{repo}")
            guide = await built._request("GET", f"/repos/{repo}/contents/{GUIDE}?ref={quote(branch)}")
        except ToolError as exc:
            report(FAIL, str(exc))
            return
        finally:
            await built.close()
        if not about.get("private"):
            report(FAIL, f"{repo} is public: make it private, or Athena won't push to it")
        elif not (about.get("permissions") or {}).get("push"):
            report(FAIL, f"the token can read {repo} but not write to it: give it Contents, read and write")
        else:
            report(OK, f"can read and write {repo}, which is private")
        if guide.status_code != 200:
            report(WARN, f"{repo} has no {GUIDE} on {branch}: copy deploy/zeus-solutions/CLAUDE.md there (README)")

    async def close(self) -> None:
        await self.http.aclose()
        self.store.close()

    # -- talking to GitHub --

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ToolError(f"Couldn't reach GitHub ({type(exc).__name__}).") from exc
        if response.status_code in (401, 403):
            raise ToolError(
                f"GitHub refused the token for {self.cfg.repo} ({response.status_code}): check PROJECTS_GITHUB_TOKEN "
                "has Contents and Pull requests, read and write, on that repository."
            )
        if response.status_code == 404 and path == f"/repos/{self.cfg.repo}":
            raise ToolError(f"GitHub has no repository {self.cfg.repo} that the token can see.")
        return response

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        """A request that must work: its JSON, or ToolError saying what GitHub said."""
        response = await self._request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                said = response.json().get("message", "")
            except ValueError:
                said = ""
            raise ToolError(
                f"GitHub said {response.status_code} for {method} {path.split('?')[0]}: {said}".rstrip(": ")
            )
        return response.json() if response.content else {}

    async def _get(self, path: str) -> Any:
        return await self._call("GET", path)

    async def _check_private(self) -> None:
        if self._private is None:
            self._private = bool((await self._get(f"/repos/{self.cfg.repo}")).get("private"))
        if not self._private:
            self._private = None  # look again next time: you may have made it private
            raise ToolError(
                f"{self.cfg.repo} is public. Make it private on GitHub first: requirements can be personal."
            )

    async def _blob(self, sha: str) -> str:
        if sha not in self._blobs:
            blob = await self._get(f"/repos/{self.cfg.repo}/git/blobs/{sha}")
            self._blobs[sha] = base64.b64decode(blob.get("content") or "").decode("utf-8", errors="replace")
        return self._blobs[sha]

    async def _pushed_at(self, sha: str) -> float:
        if sha not in self._pushed:
            commit = await self._get(f"/repos/{self.cfg.repo}/commits/{sha}")
            when = ((commit.get("commit") or {}).get("committer") or {}).get("date") or ""
            self._pushed[sha] = _time(when)
        return self._pushed[sha]

    async def _pulls(self) -> list[dict[str, Any]]:
        """Projects' pull requests, newest first. One closed without merging is a project called off: left out."""
        pulls = await self._get(f"/repos/{self.cfg.repo}/pulls?state=all&per_page=100&sort=created&direction=desc")
        return [
            pr
            for pr in pulls
            if str((pr.get("head") or {}).get("ref", "")).startswith(PREFIX)
            and (pr.get("state") == "open" or pr.get("merged_at"))
        ]

    async def _restart(self, number: int) -> None:
        """Start Claude again, by taking the label off the pull request and putting it back."""
        label = quote(self.cfg.label, safe="")
        response = await self._request("DELETE", f"/repos/{self.cfg.repo}/issues/{number}/labels/{label}")
        if response.status_code not in (200, 404):  # 404: it wasn't on
            raise ToolError(f"GitHub said {response.status_code} taking the label off pull request #{number}.")
        await self._call("POST", f"/repos/{self.cfg.repo}/issues/{number}/labels", json={"labels": [self.cfg.label]})

    # -- the board --

    async def refresh(self) -> list[str]:
        """Read the pull requests again, and start Claude again where it should be. Returns what to tell you."""
        try:
            read = [found for pr in await self._pulls() if (found := await self._read(pr))]
        except ToolError as exc:
            self.error, self.checked = str(exc), self.clock()
            self.store.keep(self.board())
            raise
        self.projects = [project for project, _ in read]
        watches = self.store.watches()
        first = not watches
        news: list[str] = []
        for project, head in read:
            watch = watches.setdefault(project.folder, Watch())
            if not first and project.status in ("done", "blocked") and watch.status != project.status:
                news.append(self.tell(project))
            watch.status = project.status
            try:
                news += await self._keep_going(project, head, watch)
            except ToolError as exc:
                log.warning("Couldn't start Claude again on %s: %s", project.folder, exc)
        self.store.keep_watches(watches)
        self.error, self.checked = None, self.clock()
        self.store.keep(self.board())
        return news

    async def _read(self, pr: dict[str, Any]) -> tuple[Project, str] | None:
        """A pull request's project, and the branch's last commit. None if it isn't one of Athena's."""
        folder, sha = pr["head"]["ref"][len(PREFIX) :], pr["head"]["sha"]
        response = await self._request("GET", f"/repos/{self.cfg.repo}/contents/{quote(folder)}?ref={sha}")
        if response.status_code == 404:
            return None  # a branch called project/... that isn't one of Athena's
        if response.status_code >= 400:
            raise ToolError(f"GitHub said {response.status_code} reading {folder}.")
        files = {item["name"]: item["sha"] for item in response.json() if item.get("type") == "file"}
        if REQUIREMENTS not in files:
            return None
        project = parse(folder, await self._blob(files[REQUIREMENTS]))
        apply_status(project, await self._blob(files[STATUS]) if STATUS in files else None)
        project.number, project.url = pr["number"], pr.get("html_url", "")
        project.pr = "merged" if pr.get("merged_at") else str(pr.get("state") or "open")
        if SOLUTION in files:
            project.solution_url = f"https://github.com/{self.cfg.repo}/blob/{sha}/{quote(folder)}/{SOLUTION}"
        project.last_push = await self._pushed_at(sha) if project.pr == "open" else 0.0
        return project, sha

    async def _keep_going(self, project: Project, head: str, watch: Watch) -> list[str]:
        """If Claude should be working on an open project and isn't, start it again. Returns what to tell you."""
        if project.pr != "open" or project.status in ("done", "blocked"):
            return []
        if head != watch.head:  # Claude's pushed (or it's new): it's working
            watch.head, watch.restarts, watch.told_waiting, watch.gave_up = head, 0, False, False
        started = project.status != "new"
        minutes = self.cfg.stalled_minutes if started else self.cfg.start_minutes
        wait = minutes * 60 * 2 ** min(watch.restarts, 3)
        since = max(project.last_push, watch.restarted)
        now = self.clock()
        project.restarts, project.gave_up = watch.restarts, watch.gave_up
        if now - since < minutes * 60 and not watch.restarts:
            return []  # working, as far as Athena can tell
        project.waiting = "stopped" if started else "not started"
        if watch.gave_up:
            return []
        if watch.restarts >= self.cfg.max_restarts:
            watch.gave_up = project.gave_up = True
            return [
                f"⚠️ Claude still hasn't {'carried on with' if started else 'started on'} “{project.title}” after "
                f"{watch.restarts} tries. Check its routine at claude.ai/code/routines, then press Restart on the "
                f"Board tab.\n{project.url}"
            ]
        project.next_restart = since + wait
        if now < since + wait:
            return []
        await self._restart(project.number)
        watch.restarts, watch.restarted = watch.restarts + 1, now
        project.restarts, project.next_restart = watch.restarts, now + minutes * 60 * 2 ** min(watch.restarts, 3)
        log.info("Started Claude again on %s (try %d)", project.folder, watch.restarts)
        if watch.told_waiting:
            return []
        watch.told_waiting = True
        if started:
            hours = self.cfg.stalled_minutes / 60
            said = (
                f"⏳ Claude stopped part way through “{project.title}”, with nothing pushed for {hours:g} hours: "
                "maybe it reached your usage limit. I've asked it to carry on from where it got to, and I'll "
                "keep trying for a while."
            )
        else:
            said = (
                f"⏳ Claude hasn't started on “{project.title}”: maybe you're at your usage limit. I've asked it "
                "again, and I'll keep trying for a while."
            )
        return [f"{said}\n{project.url}"]

    async def restart(self, folder: str) -> Project:
        """Start Claude again on a project now, as you asked on the dashboard."""
        project = next((p for p in self.projects if p.folder == folder), None)
        if project is None or project.pr != "open":
            raise ToolError(f"There's no open project {folder}.")
        await self._restart(project.number)
        watches = self.store.watches()
        watch = watches.setdefault(folder, Watch())
        # Tries count afresh from here, without telling you again that Claude isn't working on it.
        watch.restarted, watch.restarts, watch.gave_up, watch.told_waiting = self.clock(), 0, False, True
        self.store.keep_watches(watches)
        project.gave_up, project.waiting, project.restarts = False, "", 0
        self.store.keep(self.board())
        return project

    def board(self) -> dict[str, Any]:
        return {
            "repo": self.cfg.repo,
            "checked": self.checked,
            "error": self.error,
            "poll_minutes": self.cfg.poll_minutes,
            "projects": [asdict(p) for p in self.projects],
        }

    @staticmethod
    def tell(project: Project) -> str:
        """What you're told in Telegram when a project's done, or Claude's stuck."""
        if project.status == "done":
            said = f": {project.progress}" if project.progress else "."
            return f"✅ Claude has finished “{project.title}”{said} Merge it when you're happy.\n{project.url}"
        stuck = [s for s in project.stories if s.status == "blocked"]
        why = project.progress or (f"{stuck[0].id}, {stuck[0].title}: {stuck[0].note}" if stuck else "")
        return (
            f"⛔ Claude is stuck on “{project.title}”"
            + (f": {why}" if why else ".")
            + " Tell me the answer and I'll pass it on.\n"
            + project.url
        )

    # -- handing a project over --

    def _requirements(self, args: dict[str, Any]) -> tuple[str, str, str]:
        """The title, Requirements.md and folder name, from the model's arguments. ToolError if they're wrong."""
        title = " ".join(str(args.get("title") or "").split())
        summary = str(args.get("summary") or "").strip()
        stories = args.get("stories")
        if not title or len(title) > TITLE_CHARS:
            raise ToolError(f"Give the project a title, of up to {TITLE_CHARS} characters.")
        if not summary:
            raise ToolError("Say what the project is for, in the summary.")
        if not isinstance(stories, list) or not stories or len(stories) > MAX_STORIES:
            raise ToolError(f"Give between 1 and {MAX_STORIES} stories.")
        for i, story in enumerate(stories, 1):
            if (
                not isinstance(story, dict)
                or not str(story.get("title") or "").strip()
                or not isinstance(story.get("done_when"), list)
                or not [c for c in story["done_when"] if str(c).strip()]
            ):
                raise ToolError(f"Story {i} needs a title, and done_when: what must be true for it to be done.")
            story["done_when"] = [c for c in story["done_when"] if str(c).strip()]
        today = datetime.now(self.tz).date().isoformat()
        return title, render(title, summary, stories, str(args.get("notes") or "")), f"{slug(title)}-{today}"

    async def _main(self) -> str:
        """The main branch's last commit. ToolError if there's no main branch yet."""
        response = await self._request("GET", f"/repos/{self.cfg.repo}/git/ref/heads/{quote(self.cfg.branch)}")
        if response.status_code in (404, 409):
            raise ToolError(
                f"{self.cfg.repo} has no {self.cfg.branch} branch yet. Put CLAUDE.md on it first: see the README, "
                "“Projects for Claude”."
            )
        if response.status_code >= 400:
            raise ToolError(f"GitHub said {response.status_code} for the {self.cfg.branch} branch.")
        return response.json()["object"]["sha"]

    async def hand_over(self, args: dict[str, Any]) -> Project:
        title, text, folder = self._requirements(args)
        await self._check_private()
        main = await self._main()
        repo = self.cfg.repo
        taken = {item["name"] for item in await self._get(f"/repos/{repo}/contents?ref={quote(self.cfg.branch)}")}
        branches = await self._get(f"/repos/{repo}/git/matching-refs/heads/{PREFIX}")
        taken |= {ref["ref"].removeprefix(f"refs/heads/{PREFIX}") for ref in branches}
        name, n = folder, 1
        while name in taken:
            n += 1
            name = f"{folder}-{n}"
        branch = f"{PREFIX}{name}"
        await self._call("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": main})
        await self._call(
            "PUT",
            f"/repos/{repo}/contents/{quote(name)}/{REQUIREMENTS}",
            json={
                "message": f"Athena: requirements for {title}",
                "content": base64.b64encode(text.encode()).decode(),
                "branch": branch,
            },
        )
        body = (
            f"{str(args.get('summary') or '').strip()}\n\nRequirements: [`{name}/{REQUIREMENTS}`]"
            f"(../blob/{branch}/{name}/{REQUIREMENTS}). Claude: follow `{GUIDE}`, and push to `{branch}`."
        )
        pr = await self._call(
            "POST", f"/repos/{repo}/pulls", json={"title": title, "head": branch, "base": self.cfg.branch, "body": body}
        )
        await self._call("POST", f"/repos/{repo}/issues/{pr['number']}/labels", json={"labels": [self.cfg.label]})
        project = parse(name, text)
        project.number, project.url = pr["number"], pr.get("html_url", "")
        project.last_push = self.clock()
        self.projects.insert(0, project)
        watches = self.store.watches()
        watches[name] = Watch(status=project.status, restarted=self.clock())
        self.store.keep_watches(watches)
        self.store.keep(self.board())
        log.info("Handed %s to Claude, as pull request #%s", name, pr["number"])
        return project

    async def answer(self, project: Project, text: str) -> None:
        """Add your answer to Answers.md on the project's branch, and start Claude again."""
        branch, path = f"{PREFIX}{project.folder}", f"{quote(project.folder)}/{ANSWERS}"
        response = await self._request("GET", f"/repos/{self.cfg.repo}/contents/{path}?ref={quote(branch)}")
        before, sha = "", None
        if response.status_code == 200:
            existing = response.json()
            before, sha = base64.b64decode(existing.get("content") or "").decode("utf-8", "replace"), existing["sha"]
        elif response.status_code != 404:
            raise ToolError(f"GitHub said {response.status_code} reading {ANSWERS}.")
        when = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M")
        heading = before.rstrip() + "\n\n" if before.strip() else "# Answers\n\n"
        body: dict[str, Any] = {
            "message": f"Athena: an answer for {project.title}",
            "content": base64.b64encode(f"{heading}## {when}\n\n{text.strip()}\n".encode()).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        await self._call("PUT", f"/repos/{self.cfg.repo}/contents/{path}", json=body)
        await self.restart(project.folder)

    def _find(self, name: str, open_only: bool = False) -> Project:
        wanted = name.strip().lower()
        found = [
            p
            for p in self.projects
            if (wanted in p.folder.lower() or wanted in p.title.lower()) and (p.pr == "open" or not open_only)
        ]
        if not wanted or not found:
            raise ToolError(f"No {'open ' if open_only else ''}project matches {name!r}.")
        return found[0]

    # -- the model's tools --

    def tools(self) -> list[Tool]:
        async def preview(args: dict[str, Any]) -> str:
            title, text, folder = self._requirements(dict(args))
            await self._check_private()
            stories = text.count("\n### S")
            return (
                f"This opens a pull request in {self.cfg.repo} on GitHub (private), from a new branch with "
                f"{folder}/{REQUIREMENTS}, and labels it “{self.cfg.label}”, which starts Claude: “{title}”, with "
                f"{stories} stor{'y' if stories == 1 else 'ies'}, made from everything below."
            )

        async def hand_to_claude(args: dict[str, Any]) -> str:
            project = await self.hand_over(args)
            return (
                f"Handed to Claude as pull request #{project.number}, {project.folder}, with {len(project.stories)} "
                f"stor{'y' if len(project.stories) == 1 else 'ies'}. It's on the dashboard's Board tab, and the user "
                f"hears when it's done. {project.url}"
            )

        async def answer_preview(args: dict[str, Any]) -> str:
            project = self._find(str(args.get("project") or ""), open_only=True)
            text = str(args.get("answer") or "").strip()
            if not text or len(text) > ANSWER_CHARS:
                raise ToolError(f"Give the answer, in up to {ANSWER_CHARS:,} characters.")
            await self._check_private()
            return (
                f"This adds the answer below to {project.folder}/{ANSWERS} on its branch in {self.cfg.repo} (private), "
                f"and starts Claude again on “{project.title}”."
            )

        async def answer_claude(args: dict[str, Any]) -> str:
            project = self._find(str(args.get("project") or ""), open_only=True)
            await self.answer(project, str(args.get("answer") or ""))
            return f"Passed the answer on, and started Claude again on “{project.title}”. {project.url}"

        async def claude_projects(args: dict[str, Any]) -> str:
            if not self.projects:
                return "There are no projects for Claude." + (f" ({self.error})" if self.error else "")
            if str(args.get("project") or "").strip():
                return self._describe(self._find(str(args["project"])), detail=True)
            return "\n".join(self._describe(p) for p in self.projects[:30])

        story = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "details": {"type": "string", "description": "What it is, for someone new to it"},
                "done_when": {"type": "array", "items": {"type": "string"}, "description": "Success criteria"},
            },
            "required": ["title", "done_when"],
        }
        return [
            Tool(
                name=HAND_TO_CLAUDE,
                description=(
                    "Hand a coding project to Claude, who works on it and reports back. Pushes its requirements to "
                    "the user's private GitHub repo, after asking them. Split it into stories, each with clear "
                    "success criteria, and put no secrets in it."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "A few words: it names the folder"},
                        "summary": {"type": "string", "description": "What it's for and the outcome wanted"},
                        "stories": {"type": "array", "items": story},
                        "notes": {"type": "string", "description": "Constraints: languages, where it runs, limits"},
                    },
                    "required": ["title", "summary", "stories"],
                },
                handler=hand_to_claude,
                needs_confirmation=True,
                preview=preview,
            ),
            Tool(
                name=ANSWER_CLAUDE,
                description=(
                    "Pass the user's answer to Claude, when it's stuck on a project and asked them something, and "
                    "start it again. Asks the user first."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Its name or folder"},
                        "answer": {"type": "string", "description": "The user's answer, as they gave it"},
                    },
                    "required": ["project", "answer"],
                },
                handler=answer_claude,
                needs_confirmation=True,
                preview=answer_preview,
            ),
            Tool(
                name="claude_projects",
                description=(
                    "How Claude's getting on with the coding projects you've handed it: each project's status and "
                    "stories. Give a project's name for its stories and Claude's notes."
                ),
                parameters={"type": "object", "properties": {"project": {"type": "string"}}},
                handler=claude_projects,
            ),
        ]

    def _describe(self, p: Project, detail: bool = False) -> str:
        done = sum(1 for s in p.stories if s.status == "done")
        status = {"merged": "merged", "closed": "closed"}.get(p.pr, p.status)
        line = f"{p.folder}: {p.title} ({status}, {done} of {len(p.stories)} stories done)"
        if p.waiting:
            line += f". Claude {'hasn’t started' if p.waiting == 'not started' else 'stopped part way'}"
            line += "; Athena gave up restarting it" if p.gave_up else f"; restarted {p.restarts} times"
        if p.progress:
            line += f". Claude: {p.progress}"
        if not detail:
            return line
        lines = [line, p.url]
        for s in p.stories:
            lines.append(f"- {s.id} {s.title}: {s.status}" + (f". {s.note}" if s.note else ""))
        return "\n".join(lines)


def _time(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _project(kept: dict[str, Any]) -> Project:
    stories = [Story(**s) for s in kept.pop("stories", [])]
    known = {k: v for k, v in kept.items() if k in Project.__dataclass_fields__}
    return Project(**known, stories=stories)
