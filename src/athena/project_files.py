"""What's in a project's folder in the repo, and how Athena reads and writes it: Requirements.md, which Athena
writes, and status.json, which Claude does. docs/claude-projects/CLAUDE.md tells Claude the same format.

    <folder>/Requirements.md   # title, summary, "## Stories" of "### S1: ..." with "**Done when:**" lists,
                               # "## Notes", and "## Changes, <date>" with the stories asked for since
    <folder>/status.json       # {"status": ..., "summary": ..., "stories": {"S1": {"status": ..., "note": ...}}}

Nothing here talks to GitHub, so it's all tested without it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from athena.tools import ToolError

PREFIX = "project/"  # each project's branch
ROUND = re.compile(r"--(\d+)$")  # on a project's branch after the first round: folder names never have "--"
REQUIREMENTS, STATUS, SOLUTION, ANSWERS, GUIDE = (
    "Requirements.md",
    "status.json",
    "Solution.md",
    "Answers.md",
    "CLAUDE.md",
)
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
    branch: str = ""  # the pull request's
    round: int = 1  # 2 and on: more work after the first pull request was merged
    rounds: list[dict[str, Any]] = field(default_factory=list)  # the earlier rounds' pull requests
    fresh: list[str] = field(default_factory=list)  # the stories asked for last, under "## Changes", if any
    started: bool = False  # Claude's started on what's asked now: status.json, mentioning any fresh stories


def folder_of(branch: str) -> tuple[str, int]:
    """A project branch's folder, and its round."""
    name = branch.removeprefix(PREFIX)
    match = ROUND.search(name)
    return (name[: match.start()], int(match.group(1))) if match else (name, 1)


def slug(title: str) -> str:
    """A folder name for a title: lower case, words joined by hyphens."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    name = "-".join(words)[:50].rstrip("-")
    return name or "project"


def render(title: str, summary: str, stories: list[dict[str, Any]], notes: str = "") -> str:
    """Requirements.md, as Athena writes it: what parse() reads back."""
    lines = [f"# {title.strip()}", "", summary.strip(), "", "## Stories", "", *_stories(stories, 1)]
    if notes.strip():
        lines += ["## Notes", "", notes.strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def render_more(first: int, summary: str, stories: list[dict[str, Any]], when: str, notes: str = "") -> str:
    """What's added to Requirements.md for more work: stories numbered from S<first>, under a dated heading."""
    lines = [f"## Changes, {when}", "", summary.strip(), ""]
    if notes.strip():
        lines += [notes.strip(), ""]
    lines += _stories(stories, first)
    return "\n".join(lines).rstrip() + "\n"


def _stories(stories: list[dict[str, Any]], first: int) -> list[str]:
    """Stories as Requirements.md has them, numbered from S<first>."""
    lines = []
    for i, story in enumerate(stories, first):
        lines += [f"### S{i}: {' '.join(str(story['title']).split())}", ""]
        if str(story.get("details") or "").strip():
            lines += [str(story["details"]).strip(), ""]
        lines.append("**Done when:**")
        lines += [f"- {' '.join(str(c).split())}" for c in story["done_when"]]
        lines.append("")
    return lines


def parse(folder: str, text: str) -> Project:
    """A project from its Requirements.md. Forgiving: one written by hand still shows on the board."""
    lines = text.splitlines()
    title, summary, notes, section = folder, [], [], ""
    fresh: list[str] = []
    stories: list[Story] = []
    story: Story | None = None
    in_criteria = False
    for line in lines:
        if line.startswith("# ") and title == folder and not section:
            title = line[2:].strip() or folder
            continue
        if line.startswith("## "):
            section, story, in_criteria = line[3:].strip().lower(), None, False
            if section.startswith("changes"):  # more work: what it's for goes with the notes
                notes += ["", f"{line[3:].strip()}:"]
                fresh = []
            continue
        match = _STORY.match(line)
        if match:
            story = Story(match.group(1).upper(), match.group(2))
            stories.append(story)
            if section.startswith("changes"):
                fresh.append(story.id)
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
        elif section.startswith(("note", "changes")):
            notes.append(line)
    for s in stories:
        s.details = s.details.strip()
    date = match.group(1) if (match := _FOLDER_DATE.search(folder)) else ""
    project = Project(folder, title, "\n".join(summary).strip(), "\n".join(notes).strip(), date, stories)
    project.fresh = fresh
    return project


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
        listed = {str(k).upper() for k in stories}
        project.started = bool(project.problem) or not project.fresh or any(i in listed for i in project.fresh)
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
            if project.status == "done" and any(s.status != "done" for s in project.stories if s.id in project.fresh):
                project.status = "in progress"  # more's been asked for since
            return
    moved = any(s.status != "to do" for s in project.stories)
    if project.stories and all(s.status == "done" for s in project.stories):
        project.status = "done"
    elif any(s.status == "blocked" for s in project.stories):
        project.status = "blocked"
    else:
        project.status = "in progress" if moved or text is not None else "new"


def check_stories(stories: Any, already: int = 0) -> None:
    """ToolError, saying what's wrong, unless ``stories`` is a list of good ones, and with ``already`` not too many."""
    if not isinstance(stories, list) or not stories or len(stories) > MAX_STORIES:
        raise ToolError(f"Give between 1 and {MAX_STORIES} stories.")
    if already + len(stories) > 2 * MAX_STORIES:
        raise ToolError(
            f"The project would have over {2 * MAX_STORIES} stories: hand the rest over as a project of its own."
        )
    for i, story in enumerate(stories, 1):
        if (
            not isinstance(story, dict)
            or not str(story.get("title") or "").strip()
            or not isinstance(story.get("done_when"), list)
            or not [c for c in story["done_when"] if str(c).strip()]
        ):
            raise ToolError(f"Story {i} needs a title, and done_when: what must be true for it to be done.")
        story["done_when"] = [c for c in story["done_when"] if str(c).strip()]


def pull_round(pr: dict[str, Any]) -> dict[str, Any]:
    """An earlier round's pull request, as the board links to it."""
    state = "merged" if pr.get("merged_at") else str(pr.get("state") or "open")
    return {
        "number": pr["number"],
        "url": pr.get("html_url", ""),
        "pr": state,
        "round": folder_of(pr["head"]["ref"])[1],
    }


def project_round(project: Project) -> dict[str, Any]:
    """A project's current pull request, as the board links to it once it's an earlier round."""
    return {"number": project.number, "url": project.url, "pr": project.pr, "round": project.round}


def project_from(kept: dict[str, Any]) -> Project:
    """A project as the board kept it, in the database."""
    stories = [Story(**s) for s in kept.pop("stories", [])]
    known = {k: v for k, v in kept.items() if k in Project.__dataclass_fields__}
    return Project(**known, stories=stories)
