"""Projects for Claude: coding work Athena hands to Claude through a private GitHub repo, and a board of it.

Handing a project over (`hand_to_claude`, which always asks you first, showing everything it will push):
  1. Athena makes a branch, `project/<project>-<YYYY-MM-DD>`, from the main branch, and commits one file
     to it: `<project>-<YYYY-MM-DD>/Requirements.md`, a summary, then stories, each with what "done" means.
  2. It opens a pull request from that branch, and labels it [claude_projects] label ("claude").
  3. The label starts Claude: a routine at claude.ai/code/routines with a GitHub trigger on pull requests
     being labelled. docs/claude-projects/CLAUDE.md, at the root of the repo, tells it how to work.

Claude pushes its work to the branch, with `status.json` saying where the project and each story are,
and `Solution.md` when it's done. Athena reads the pull requests every poll_minutes (and when you ask, on
the dashboard), shows each project as an epic of stories on the Board tab, and tells you in Telegram
when one is done, or Claude's stuck. You merge the pull request when you're happy with it.

More work on a project (`extend_claude_project`, which asks first too) adds stories to its Requirements.md,
numbered on from the last, under "## Changes, <date>". While its pull request is open, that's on the same
branch, and Claude's started again. Once it's merged, it's a new round: a branch `project/<folder>--<n>`
from main, and a pull request of its own. The board shows each folder as one epic, with its latest round.

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

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from athena.builtin import FAIL, OK, WARN, Builtin, Report, Section, Unavailable
from athena.catalog import Field, Integration, Secret
from athena.github_repo import GitHubRepo
from athena.project_files import (
    ANSWER_CHARS,
    ANSWERS,
    GUIDE,
    PREFIX,
    REQUIREMENTS,
    SOLUTION,
    STATUS,
    TITLE_CHARS,
    Project,
    apply_status,
    check_stories,
    folder_of,
    parse,
    project_from,
    project_round,
    pull_round,
    render,
    render_more,
    slug,
)
from athena.tools import Tool, ToolError

if TYPE_CHECKING:
    from pathlib import Path

    from athena.config import Config

log = logging.getLogger(__name__)

HAND_TO_CLAUDE, ANSWER_CLAUDE, EXTEND_CLAUDE = "hand_to_claude", "answer_claude", "extend_claude_project"


class ClaudeProjectsConfig(Section):
    # Coding projects Athena hands to Claude through a private GitHub repo (see docs/claude-projects.md).
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
        from athena.memory import connect_db  # here: memory needs config, which needs this module

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
        setup="Set up the repository, its token and Claude's routine first (see docs/claude-projects.md).",
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
        self.github = GitHubRepo(
            cfg.repo, cfg.token, token_name="PROJECTS_GITHUB_TOKEN", timeout=cfg.timeout_seconds, http=http
        )
        self.checked = 0.0
        self.error: str | None = None
        self.projects: list[Project] = []
        if kept := store.board():
            self.checked, self.error = kept.get("checked", 0.0), kept.get("error")
            self.projects = [project_from(p) for p in kept.get("projects", [])]

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
            about = await built.github.about()
            guide = await built.github.file(GUIDE, branch)
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
        if guide is None:
            report(WARN, f"{repo} has no {GUIDE} on {branch}: copy Athena's docs/claude-projects/CLAUDE.md")

    async def close(self) -> None:
        await self.github.close()
        self.store.close()

    async def _pulls(self) -> list[dict[str, Any]]:
        """Projects' pull requests, newest first. One closed without merging is a project called off: left out."""
        return [
            pr
            for pr in await self.github.pulls()
            if str((pr.get("head") or {}).get("ref", "")).startswith(PREFIX)
            and (pr.get("state") == "open" or pr.get("merged_at"))
        ]

    async def _restart(self, number: int) -> None:
        """Start Claude again, by taking the label off the pull request and putting it back."""
        await self.github.relabel(number, self.cfg.label)

    # -- the board --

    async def refresh(self) -> list[str]:
        """Read the pull requests again, and start Claude again where it should be. Returns what to tell you."""
        try:
            rounds: dict[str, list[dict[str, Any]]] = {}  # each folder's pull requests, newest first
            for pr in await self._pulls():
                rounds.setdefault(folder_of(pr["head"]["ref"])[0], []).append(pr)
            read = []
            for folder, prs in rounds.items():
                current = next((pr for pr in prs if pr.get("state") == "open"), prs[0])
                if found := await self._read(current, folder):
                    found[0].rounds = [
                        pull_round(pr) for pr in sorted(prs, key=lambda pr: pr["number"]) if pr is not current
                    ]
                    read.append(found)
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

    async def _read(self, pr: dict[str, Any], folder: str) -> tuple[Project, str] | None:
        """A pull request's project, and the branch's last commit. None if it isn't one of Athena's."""
        sha = pr["head"]["sha"]
        files = await self.github.folder(folder, sha)
        if files is None or REQUIREMENTS not in files:
            return None  # a branch called project/... that isn't one of Athena's
        project = parse(folder, await self.github.blob(files[REQUIREMENTS]))
        apply_status(project, await self.github.blob(files[STATUS]) if STATUS in files else None)
        project.number, project.url = pr["number"], pr.get("html_url", "")
        project.pr = "merged" if pr.get("merged_at") else str(pr.get("state") or "open")
        project.branch, project.round = pr["head"]["ref"], folder_of(pr["head"]["ref"])[1]
        if SOLUTION in files:
            project.solution_url = f"https://github.com/{self.cfg.repo}/blob/{sha}/{quote(folder)}/{SOLUTION}"
        project.last_push = await self.github.pushed_at(sha) if project.pr == "open" else 0.0
        return project, sha

    async def _keep_going(self, project: Project, head: str, watch: Watch) -> list[str]:
        """If Claude should be working on an open project and isn't, start it again. Returns what to tell you."""
        if project.pr != "open" or project.status in ("done", "blocked"):
            return []
        if head != watch.head:  # Claude's pushed (or it's new): it's working
            watch.head, watch.restarts, watch.told_waiting, watch.gave_up = head, 0, False, False
        started = project.started
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

    def _watching(self, folder: str, status: str) -> None:
        """Watch a project afresh, as Claude's just been asked to work on it."""
        watches = self.store.watches()
        watches[folder] = Watch(status=status, restarted=self.clock())
        self.store.keep_watches(watches)

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
        check_stories(stories)
        today = datetime.now(self.tz).date().isoformat()
        return title, render(title, summary, stories, str(args.get("notes") or "")), f"{slug(title)}-{today}"

    async def _main(self) -> str:
        """The main branch's last commit. ToolError if there's no main branch yet."""
        main = await self.github.head(self.cfg.branch)
        if main is None:
            raise ToolError(
                f"{self.cfg.repo} has no {self.cfg.branch} branch yet. Put CLAUDE.md on it first: see "
                "docs/claude-projects.md."
            )
        return main

    async def hand_over(self, args: dict[str, Any]) -> Project:
        title, text, folder = self._requirements(args)
        await self.github.check_private()
        main = await self._main()
        taken = await self.github.names(self.cfg.branch)
        taken |= {folder_of(branch)[0] for branch in await self.github.branches(PREFIX)}
        name, n = folder, 1
        while name in taken:
            n += 1
            name = f"{folder}-{n}"
        branch = f"{PREFIX}{name}"
        await self.github.make_branch(branch, main)
        await self.github.put(
            f"{quote(name)}/{REQUIREMENTS}", text, branch=branch, message=f"Athena: requirements for {title}"
        )
        body = (
            f"{str(args.get('summary') or '').strip()}\n\nRequirements: [`{name}/{REQUIREMENTS}`]"
            f"(../blob/{branch}/{name}/{REQUIREMENTS}). Claude: follow `{GUIDE}`, and push to `{branch}`."
        )
        pr = await self.github.open_pull(title=title, head=branch, base=self.cfg.branch, body=body)
        await self.github.label(pr["number"], self.cfg.label)
        project = parse(name, text)
        project.number, project.url, project.branch = pr["number"], pr.get("html_url", ""), branch
        project.last_push = self.clock()
        self.projects.insert(0, project)
        self._watching(name, project.status)
        self.store.keep(self.board())
        log.info("Handed %s to Claude, as pull request #%s", name, pr["number"])
        return project

    async def answer(self, project: Project, text: str) -> None:
        """Add your answer to Answers.md on the project's branch, and start Claude again."""
        branch, path = project.branch or f"{PREFIX}{project.folder}", f"{quote(project.folder)}/{ANSWERS}"
        before, sha = await self.github.file(path, branch) or ("", None)
        when = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M")
        heading = before.rstrip() + "\n\n" if before.strip() else "# Answers\n\n"
        await self.github.put(
            path,
            f"{heading}## {when}\n\n{text.strip()}\n",
            branch=branch,
            message=f"Athena: an answer for {project.title}",
            sha=sha,
        )
        await self.restart(project.folder)

    # -- more work on a project --

    def _more(self, project: Project, args: dict[str, Any]) -> tuple[str, list[str]]:
        """What's added to Requirements.md, and the new stories' ids. ToolError if the arguments aren't right."""
        summary, stories = str(args.get("summary") or "").strip(), args.get("stories")
        if not summary:
            raise ToolError("Say what's changing, and why, in the summary.")
        check_stories(stories, len(project.stories))
        first = max((int(s.id[1:]) for s in project.stories), default=0) + 1
        when = datetime.now(self.tz).date().isoformat()
        text = render_more(first, summary, stories, when, str(args.get("notes") or ""))
        return text, [f"S{i}" for i in range(first, first + len(stories))]

    async def _next_round(self, project: Project) -> tuple[int, str]:
        """The next round's number, and its branch, which isn't taken."""
        taken = set(await self.github.branches(f"{PREFIX}{project.folder}"))
        n = max([project.round, *(r.get("round", 1) for r in project.rounds)]) + 1
        while f"{PREFIX}{project.folder}--{n}" in taken:
            n += 1
        return n, f"{PREFIX}{project.folder}--{n}"

    async def extend(self, project: Project, args: dict[str, Any]) -> Project:
        """Add stories to a project, and start Claude on them: on its branch while its pull request is open, or
        as a new round, with a pull request of its own, once it's merged."""
        more, ids = self._more(project, args)
        await self.github.check_private()
        path = f"{quote(project.folder)}/{REQUIREMENTS}"
        message = f"Athena: more for {project.title} ({', '.join(ids)})"
        if project.pr == "open":
            branch, new_round = project.branch, None
        else:
            new_round, branch = await self._next_round(project)
            await self.github.make_branch(branch, await self._main())
        found = await self.github.file(path, branch)
        if found is None:
            where = "its branch" if new_round is None else self.cfg.branch
            raise ToolError(f"{project.folder}/{REQUIREMENTS} isn't on {where}, so there's nothing to add to.")
        before, sha = found
        text = before.rstrip() + "\n\n" + more
        await self.github.put(path, text, branch=branch, message=message, sha=sha)
        extended = parse(project.folder, text)
        for field_name in ("number", "url", "pr", "branch", "round", "rounds", "solution_url", "progress", "updated"):
            setattr(extended, field_name, getattr(project, field_name))
        done = {s.id: s for s in project.stories}
        extended.stories = [done.get(s.id, s) for s in extended.stories]
        extended.status, extended.last_push = "in progress", self.clock()
        if new_round is None:
            await self._restart(project.number)
        else:
            body = (
                f"More work on [{project.title}]({project.url}): {str(args.get('summary') or '').strip()}\n\n"
                f"New stories {', '.join(ids)}, in [`{project.folder}/{REQUIREMENTS}`](../blob/{branch}/"
                f"{project.folder}/{REQUIREMENTS}). Claude: follow `{GUIDE}`, and push to `{branch}`."
            )
            title = f"{project.title} ({new_round})"
            pr = await self.github.open_pull(title=title, head=branch, base=self.cfg.branch, body=body)
            await self.github.label(pr["number"], self.cfg.label)
            extended.rounds = [*project.rounds, project_round(project)]
            extended.number, extended.url, extended.pr = pr["number"], pr.get("html_url", ""), "open"
            extended.branch, extended.round, extended.solution_url = branch, new_round, ""
        self.projects = [extended if p.folder == project.folder else p for p in self.projects]
        self._watching(project.folder, extended.status)
        self.store.keep(self.board())
        log.info("Added %s to %s, on %s", ", ".join(ids), project.folder, branch)
        return extended

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
            await self.github.check_private()
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
            await self.github.check_private()
            return (
                f"This adds the answer below to {project.folder}/{ANSWERS} on its branch in {self.cfg.repo} (private), "
                f"and starts Claude again on “{project.title}”."
            )

        async def answer_claude(args: dict[str, Any]) -> str:
            project = self._find(str(args.get("project") or ""), open_only=True)
            await self.answer(project, str(args.get("answer") or ""))
            return f"Passed the answer on, and started Claude again on “{project.title}”. {project.url}"

        async def extend_preview(args: dict[str, Any]) -> str:
            project = self._find(str(args.get("project") or ""))
            more, ids = self._more(project, dict(args))
            await self.github.check_private()
            what = f"{len(ids)} stor{'y' if len(ids) == 1 else 'ies'} ({ids[0]}{f'–{ids[-1]}' if len(ids) > 1 else ''})"
            if project.pr == "open":
                return (
                    f"This adds {what} to {project.folder}/{REQUIREMENTS} on its branch in {self.cfg.repo} (private), "
                    f"and starts Claude again on “{project.title}”, made from everything below."
                )
            return (
                f"“{project.title}” is merged, so this opens a new pull request for it in {self.cfg.repo} (private), "
                f"from a new branch adding {what} to {project.folder}/{REQUIREMENTS}, and labels it "
                f"“{self.cfg.label}”, which starts Claude: made from everything below."
            )

        async def extend_claude_project(args: dict[str, Any]) -> str:
            project = await self.extend(self._find(str(args.get("project") or "")), args)
            new = len(project.stories) - len([r for r in project.stories if r.status == "done"])
            return (
                f"Added to “{project.title}”, on pull request #{project.number}: Claude's starting on it, with {new} "
                f"stor{'y' if new == 1 else 'ies'} to do. {project.url}"
            )

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
                name=EXTEND_CLAUDE,
                description=(
                    "Ask Claude for more work on a project it has, or has finished: new stories, each with success "
                    "criteria, added to its requirements. Starts Claude on them. Asks the user first."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Its name or folder"},
                        "summary": {"type": "string", "description": "What's changing, and why"},
                        "stories": {"type": "array", "items": story},
                        "notes": {"type": "string", "description": "New constraints, if any"},
                    },
                    "required": ["project", "summary", "stories"],
                },
                handler=extend_claude_project,
                needs_confirmation=True,
                preview=extend_preview,
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
        if p.round > 1:
            line += f", round {p.round}"
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
