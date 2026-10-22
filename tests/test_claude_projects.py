"""Projects for Claude: handing them over as pull requests, reading them back as a board, starting Claude again
when it couldn't start or stopped part way, and telling you what's happened. GitHub is a stand-in here, which
keeps branches, commits, pull requests and labels as the real API does."""

import base64
import hashlib
import itertools
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx
import pytest

from athena.builtin import FAIL, OK, WARN, Unavailable
from athena.claude_projects import BoardStore, ClaudeProjects, ClaudeProjectsConfig
from athena.project_files import apply_status, check_stories, folder_of, parse, render, render_more, slug
from athena.tools import ToolError

REPO = "someone/zeus-solutions"
GUIDE = Path(__file__).resolve().parents[1] / "docs" / "claude-projects" / "CLAUDE.md"
STORIES = [
    {
        "title": "Read the bank's CSV",
        "details": "Statements from the bank's export.",
        "done_when": ["Every row is read", "Dates are parsed"],
    },
    {"title": "Monthly totals", "done_when": ["A total for each month, by category"]},
]
HOUR = 3600.0


def blob_sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


class FakeGitHub:
    """GitHub's REST API, as much as Athena uses: branches of commits, pull requests and their labels."""

    def __init__(self, private=True, push=True):
        self.private, self.push = private, push
        self.token = "good-token"
        self.now = 1_800_000_000.0  # when commits are made: the tests move it on
        self.heads: dict[str, str] = {}  # branch: its last commit
        self.commits: dict[str, tuple[dict[str, str], float]] = {}  # sha: (files, when)
        self.pulls: list[dict] = []
        self.labels: dict[int, list[str]] = {}
        self.events: list[tuple[str, int, str]] = []  # (labeled or unlabeled, pull request, label)
        self.requests: list[tuple[str, str, dict | None]] = []
        self._ids = itertools.count(1)

    # -- what the tests do to it --

    def commit(self, branch: str, files: dict[str, str], base: str | None = None) -> str:
        """Commit ``files`` (path: text) to ``branch``, as Claude would; a new branch starts from ``base``."""
        start = base if base else branch
        before = dict(self.commits[self.heads[start]][0]) if start in self.heads else {}
        sha = f"c{next(self._ids):04d}"
        self.commits[sha] = ({**before, **files}, self.now)
        self.heads[branch] = sha
        return sha

    def files(self, ref: str) -> dict[str, str]:
        return self.commits[self.heads.get(ref, ref)][0]

    def labelled(self) -> list[tuple[str, int, str]]:
        return [e for e in self.events if e[0] == "labeled"]

    # -- the API --

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path, method, query = unquote(request.url.path), request.method, request.url.params
        self.requests.append((method, str(request.url), body))
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        base = f"/repos/{REPO}"
        if path == base:
            return httpx.Response(200, json={"private": self.private, "permissions": {"push": self.push}})
        if not path.startswith(base):
            return httpx.Response(404, json={"message": "Not Found"})
        path = path[len(base) :]
        if method in ("POST", "PUT", "DELETE") and not self.push:
            return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
        if m := re.fullmatch(r"/git/ref/heads/(.+)", path):
            if not self.heads:
                return httpx.Response(409, json={"message": "Git Repository is empty."})
            if m.group(1) not in self.heads:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.heads[m.group(1)]}})
        if m := re.fullmatch(r"/git/matching-refs/heads/(.*)", path):
            return httpx.Response(
                200, json=[{"ref": f"refs/heads/{b}"} for b in self.heads if b.startswith(m.group(1))]
            )
        if path == "/git/refs" and method == "POST":
            branch = body["ref"].removeprefix("refs/heads/")
            if branch in self.heads:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.heads[branch] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"]})
        if m := re.fullmatch(r"/git/blobs/(\w+)", path):
            for files, _ in self.commits.values():
                for text in files.values():
                    if blob_sha(text) == m.group(1):
                        return httpx.Response(200, json={"content": base64.b64encode(text.encode()).decode()})
            return httpx.Response(404, json={"message": "Not Found"})
        if m := re.fullmatch(r"/commits/(\w+)", path):
            when = datetime.fromtimestamp(self.commits[m.group(1)][1], UTC).isoformat().replace("+00:00", "Z")
            return httpx.Response(200, json={"sha": m.group(1), "commit": {"committer": {"date": when}}})
        if m := re.fullmatch(r"/contents/?(.*)", path):
            return self._contents(method, m.group(1), query.get("ref"), body)
        if path == "/pulls" and method == "POST":
            number = len(self.pulls) + 1
            pr = {"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}", "state": "open"}
            self.pulls.append({**pr, "title": body["title"], "body": body["body"], "branch": body["head"]})
            self.labels[number] = []
            return httpx.Response(201, json=pr)
        if path == "/pulls" and method == "GET":
            listing = [
                {
                    "number": pr["number"],
                    "html_url": pr["html_url"],
                    "state": pr["state"],
                    "merged_at": pr.get("merged_at"),
                    "head": {"ref": pr["branch"], "sha": self.heads[pr["branch"]]},
                }
                for pr in reversed(self.pulls)
            ]
            return httpx.Response(200, json=listing)
        if m := re.fullmatch(r"/issues/(\d+)/labels(?:/(.+))?", path):
            number = int(m.group(1))
            labels = self.labels.setdefault(number, [])
            if method == "DELETE":
                if m.group(2) not in labels:
                    return httpx.Response(404, json={"message": "Label does not exist"})
                labels.remove(m.group(2))
                self.events.append(("unlabeled", number, m.group(2)))
                return httpx.Response(200, json=[])
            for label in body["labels"]:
                if label not in labels:
                    labels.append(label)
                    self.events.append(("labeled", number, label))
            return httpx.Response(200, json=[{"name": label} for label in labels])
        return httpx.Response(404, json={"message": "Not Found"})

    def _contents(self, method: str, path: str, ref: str | None, body: dict | None) -> httpx.Response:
        if method == "PUT":
            branch = body["branch"]
            existing = self.files(branch).get(path)
            if existing is not None and body.get("sha") != blob_sha(existing):
                return httpx.Response(409 if body.get("sha") else 422, json={"message": "sha wasn't supplied"})
            self.commit(branch, {path: base64.b64decode(body["content"]).decode()})
            return httpx.Response(201, json={"content": {"path": path}})
        if not self.heads:
            return httpx.Response(404, json={"message": "This repository is empty."})
        files = self.files(ref or "main")
        if path in files:
            text = files[path]
            content = base64.b64encode(text.encode()).decode()
            return httpx.Response(200, json={"type": "file", "sha": blob_sha(text), "content": content})
        prefix = f"{path}/" if path else ""
        inside = {p[len(prefix) :].split("/")[0]: p for p in files if p.startswith(prefix)}
        if not inside:
            return httpx.Response(404, json={"message": "Not Found"})
        listing = [
            {"name": name, "type": "file", "sha": blob_sha(files[full])}
            if full == f"{prefix}{name}"
            else {"name": name, "type": "dir"}
            for name, full in sorted(inside.items())
        ]
        return httpx.Response(200, json=listing)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(self.handler),
            headers={"Authorization": "Bearer good-token"},
        )


class Clock:
    """Athena's clock, which is GitHub's here, so the tests can move both on together."""

    def __init__(self, github):
        self.github = github

    def __call__(self):
        return self.github.now

    def wait(self, seconds):
        self.github.now += seconds


@pytest.fixture
def github():
    hub = FakeGitHub()
    hub.commit("main", {"CLAUDE.md": "How to work"})
    return hub


@pytest.fixture
def make(github, tmp_path):
    made = []

    def build(**settings):
        cfg = ClaudeProjectsConfig(enabled=True, repo=REPO, token="good-token", **settings)
        built = ClaudeProjects(cfg, BoardStore(tmp_path / "a.db"), "Europe/London", github.client(), Clock(github))
        made.append(built)
        return built

    yield build
    for built in made:
        built.store.close()


@pytest.fixture
def projects(make):
    return make()


def tool(projects, name):
    return next(t for t in projects.tools() if t.name == name)


def today():
    return datetime.now(ZoneInfo("Europe/London")).date().isoformat()  # the fixture's time zone


async def hand(projects, title="Bank statements"):
    return await projects.hand_over({"title": title, "summary": "Sum up my spending.", "stories": STORIES})


def status(said, **stories):
    return json.dumps({"status": said, "summary": f"It's {said}.", "stories": stories})


# -- Requirements.md and status.json ------------------------------------------------------------------


def test_requirements_read_back_as_they_were_written():
    text = render("Bank statements", "Sum up my spending.", STORIES, notes="Python 3.12, no network.")
    project = parse("bank-statements-2026-10-16-2", text)
    assert (project.title, project.summary, project.notes, project.date) == (
        "Bank statements",
        "Sum up my spending.",
        "Python 3.12, no network.",
        "2026-10-16",
    )
    s1, s2 = project.stories
    assert (s1.id, s1.title, s1.details, s1.done_when) == (
        "S1",
        "Read the bank's CSV",
        "Statements from the bank's export.",
        ["Every row is read", "Dates are parsed"],
    )
    assert (s2.id, s2.details, s2.done_when) == ("S2", "", ["A total for each month, by category"])
    assert project.status == "new" and all(s.status == "to do" for s in project.stories)


def test_a_requirements_file_written_by_hand_still_shows():
    text = "# Lights\n\nTurn them off.\n\n### S1 - Find the bulbs\n\nDone when:\n1. [ ] All listed\n* [x] Named\n"
    project = parse("lights", text)
    assert project.title == "Lights" and project.date == ""
    assert [(s.id, s.title, s.done_when) for s in project.stories] == [
        ("S1", "Find the bulbs", ["All listed", "Named"])
    ]
    assert parse("odd", "Nothing like the format").title == "odd"


@pytest.mark.parametrize(
    ("title", "folder"), [("Bank statements!", "bank-statements"), ("  ", "project"), ("Ünïcode & co", "n-code-co")]
)
def test_folder_names(title, folder):
    assert slug(title) == folder


def test_what_claude_says_in_status_json():
    project = parse("p-2026-10-16", render("P", "S.", STORIES))
    apply_status(project, None)
    assert project.status == "new"

    said = {"status": "In_Progress", "summary": "Reading works.", "updated": "2026-10-16T09:00:00Z"}
    said["stories"] = {"S1": {"status": "complete", "note": "With tests."}, "s2": "doing"}
    apply_status(project, json.dumps(said))
    assert (project.status, project.progress, project.updated) == ("in progress", "Reading works.", said["updated"])
    assert [(s.status, s.note) for s in project.stories] == [("done", "With tests."), ("in progress", "")]

    broken = parse("p-2026-10-16", render("P", "S.", STORIES))
    apply_status(broken, "{not json")
    assert broken.problem.startswith("status.json couldn't be read") and broken.status == "in progress"

    # Without a project status, it follows the stories.
    stuck = parse("p", render("P", "S.", STORIES))
    apply_status(stuck, json.dumps({"stories": {"S1": {"status": "stuck"}}}))
    assert stuck.status == "blocked"
    finished = parse("p", render("P", "S.", STORIES))
    apply_status(finished, json.dumps({"stories": {"S1": "done", "S2": "finished"}}))
    assert finished.status == "done"


def test_the_guide_for_claude_matches_what_athena_does():
    guide = GUIDE.read_text()
    example = re.search(r"```json\n(.+?)```", guide, re.DOTALL).group(1)
    stories = [{"title": f"Story {i}", "done_when": ["It works"]} for i in range(1, 5)]
    project = parse("p-2026-10-16", render("P", "S.", stories))
    apply_status(project, example)
    assert project.problem == "" and project.status == "in progress"
    assert [s.status for s in project.stories] == ["done", "done", "in progress", "to do"]
    assert project.stories[0].note == "Parser in src/parse.py, with tests."
    cfg = ClaudeProjectsConfig()
    for said in ("Requirements.md", "status.json", "Solution.md", "Answers.md", "project/", f"labels it `{cfg.label}`"):
        assert said in guide, said
    assert cfg.stalled_minutes == 120 and "for two hours" in guide


# -- handing a project over -----------------------------------------------------------------------------


async def test_handing_over_asks_with_what_it_pushes_then_opens_a_labelled_pull_request(projects, github):
    hand_to_claude = tool(projects, "hand_to_claude")
    assert hand_to_claude.needs_confirmation
    args = {"title": "Bank statements", "summary": "Sum up my spending.", "stories": STORIES}
    folder = f"bank-statements-{today()}"
    said = await hand_to_claude.preview(args)
    assert said == (
        f"This opens a pull request in {REPO} on GitHub (private), from a new branch with {folder}/Requirements.md, "
        "and labels it “claude”, which starts Claude: “Bank statements”, with 2 stories, made from everything below."
    )
    assert len(github.heads) == 1 and github.pulls == []  # asking changes nothing

    result = await hand_to_claude.handler(args)
    assert result.startswith(f"Handed to Claude as pull request #1, {folder}, with 2 stories.")
    branch = f"project/{folder}"
    requirements = render("Bank statements", "Sum up my spending.", STORIES)
    assert github.files(branch) == {"CLAUDE.md": "How to work", f"{folder}/Requirements.md": requirements}
    assert github.files("main") == {"CLAUDE.md": "How to work"}  # main only changes when you merge
    (pr,) = github.pulls
    assert (pr["title"], pr["branch"]) == ("Bank statements", branch)
    assert pr["body"].startswith("Sum up my spending.") and f"push to `{branch}`" in pr["body"]
    assert github.events == [("labeled", 1, "claude")]  # what starts Claude
    assert (projects.projects[0].folder, projects.projects[0].number) == (folder, 1)  # on the board straight away

    # The same project again that day gets a folder and branch of its own,
    await hand(projects)
    assert f"project/{folder}-2" in github.heads
    # and so does one whose folder's on main already, from a merged project.
    github.commit("main", {f"recipes-{today()}/Requirements.md": "# Recipes"})
    assert (await hand(projects, "Recipes")).folder == f"recipes-{today()}-2"


async def test_without_a_main_branch_it_says_how_to_set_up(tmp_path):
    empty = FakeGitHub()
    cfg = ClaudeProjectsConfig(enabled=True, repo=REPO, token="good-token")
    projects = ClaudeProjects(cfg, BoardStore(tmp_path / "e.db"), http=empty.client())
    with pytest.raises(ToolError, match="has no main branch yet. Put CLAUDE.md on it first"):
        await hand(projects)
    assert await projects.refresh() == [] and projects.projects == []
    projects.store.close()


@pytest.mark.parametrize(
    ("args", "why"),
    [
        ({"summary": "s", "stories": STORIES}, "Give the project a title"),
        ({"title": "x" * 81, "summary": "s", "stories": STORIES}, "Give the project a title"),
        ({"title": "t", "stories": STORIES}, "summary"),
        ({"title": "t", "summary": "s", "stories": []}, "between 1 and 20 stories"),
        ({"title": "t", "summary": "s", "stories": [{"title": "a", "done_when": []}]}, "Story 1 needs"),
        ({"title": "t", "summary": "s", "stories": [{"done_when": ["x"]}]}, "Story 1 needs"),
    ],
)
async def test_requirements_that_arent_right_are_refused_before_asking(projects, github, args, why):
    with pytest.raises(ToolError, match=why):
        await tool(projects, "hand_to_claude").preview(args)
    assert github.requests == []


async def test_a_public_repo_is_refused(projects, github):
    github.private = False
    with pytest.raises(ToolError, match="is public. Make it private"):
        await tool(projects, "hand_to_claude").preview({"title": "T", "summary": "S", "stories": STORIES})
    with pytest.raises(ToolError, match="is public"):
        await hand(projects)
    assert github.pulls == []
    github.private = True  # made private: it looks again
    await hand(projects)
    assert len(github.pulls) == 1


async def test_a_token_github_refuses_says_so(projects, github):
    github.token = "another"
    with pytest.raises(ToolError, match="GitHub refused the token for someone/zeus-solutions \\(401\\)"):
        await projects.refresh()
    assert projects.board()["error"].startswith("GitHub refused the token")
    github.token, github.push = "good-token", False
    with pytest.raises(ToolError, match="GitHub refused the token"):
        await hand(projects)


# -- the board ------------------------------------------------------------------------------------------


async def test_the_board_follows_claude_and_tells_you_once(projects, github, make):
    assert await projects.refresh() == [] and projects.board()["projects"] == []
    project = await hand(projects)
    folder, branch = project.folder, f"project/{project.folder}"
    assert await projects.refresh() == []
    (shown,) = projects.board()["projects"]
    assert (shown["folder"], shown["status"], shown["number"], shown["pr"], shown["url"]) == (
        folder,
        "new",
        1,
        "open",
        f"https://github.com/{REPO}/pull/1",
    )

    # Claude starts: nothing to tell you yet.
    github.commit(branch, {f"{folder}/status.json": status("in progress", S1={"status": "done"})})
    assert await projects.refresh() == []
    assert [s["status"] for s in projects.board()["projects"][0]["stories"]] == ["done", "to do"]

    # Stuck: you're told, once.
    github.commit(branch, {f"{folder}/status.json": status("blocked", S2={"status": "blocked", "note": "Which?"})})
    (stuck,) = await projects.refresh()
    assert stuck == (
        "⛔ Claude is stuck on “Bank statements”: It's blocked. Tell me the answer and I'll pass it on.\n"
        f"https://github.com/{REPO}/pull/1"
    )
    assert await projects.refresh() == []

    # Done, with its solution.
    github.commit(branch, {f"{folder}/Solution.md": "# How\n", f"{folder}/status.json": status("done")})
    reads = len([r for r in github.requests if "/git/blobs/" in r[1]])
    (finished,) = await projects.refresh()
    assert finished == (
        "✅ Claude has finished “Bank statements”: It's done. Merge it when you're happy.\n"
        f"https://github.com/{REPO}/pull/1"
    )
    assert len([r for r in github.requests if "/git/blobs/" in r[1]]) - reads == 1  # only the new status.json
    assert projects.projects[0].solution_url.endswith(f"/{folder}/Solution.md")
    assert await projects.refresh() == []

    # Merged: it's done, on the board.
    github.pulls[0]["state"], github.pulls[0]["merged_at"] = "closed", "2026-10-17T10:00:00Z"
    assert await projects.refresh() == [] and projects.projects[0].pr == "merged"

    # After a restart, the board's there before GitHub's read again, and nothing's told twice.
    again = make()
    assert again.board()["projects"][0]["pr"] == "merged"
    assert await again.refresh() == []

    # A pull request closed without merging is a project called off: it leaves the board.
    called_off = await hand(again, "Boats")
    assert [p.folder for p in again.projects][:1] == [called_off.folder]
    github.pulls[-1]["state"] = "closed"
    await again.refresh()
    assert [p.folder for p in again.projects] == [folder]


async def test_the_first_read_tells_you_nothing(projects, github):
    branch = "project/old-2026-01-01"
    github.commit(branch, {"old-2026-01-01/Requirements.md": render("Old", "S.", STORIES)}, base="main")
    github.commit(branch, {"old-2026-01-01/status.json": status("done")})
    github.pulls.append({"number": 1, "html_url": "u", "state": "open", "branch": branch})
    github.commit("project/not-athenas", {"x.txt": "x"}, base="main")  # a branch that isn't one of Athena's
    github.pulls.append({"number": 2, "html_url": "u", "state": "open", "branch": "project/not-athenas"})
    assert await projects.refresh() == []  # done before Athena was watching
    assert [p.folder for p in projects.projects] == ["old-2026-01-01"]


# -- when Claude can't run ---------------------------------------------------------------------------------


async def test_claude_that_didnt_start_is_started_again_less_and_less_often(projects, github):
    clock = projects.clock
    project = await hand(projects)
    clock.wait(29 * 60)
    assert await projects.refresh() == [] and len(github.labelled()) == 1  # not yet

    clock.wait(2 * 60)  # half an hour, without status.json
    (said,) = await projects.refresh()
    assert said == (
        "⏳ Claude hasn't started on “Bank statements”: maybe you're at your usage limit. I've asked it again, "
        f"and I'll keep trying for a while.\n{project.url}"
    )
    assert github.events[-2:] == [("unlabeled", 1, "claude"), ("labeled", 1, "claude")]  # that fires the routine
    shown = projects.projects[0]
    assert (shown.waiting, shown.restarts, shown.next_restart) == ("not started", 1, github.now + HOUR)

    clock.wait(HOUR - 60)
    assert await projects.refresh() == [] and len(github.labelled()) == 2  # an hour, this time
    clock.wait(60)
    assert await projects.refresh() == [] and len(github.labelled()) == 3  # and you were told already
    clock.wait(2 * HOUR)
    await projects.refresh()
    assert len(github.labelled()) == 4

    # Claude starts: it's working, and the count starts again.
    github.commit(f"project/{project.folder}", {f"{project.folder}/status.json": status("in progress")})
    assert await projects.refresh() == []
    shown = projects.projects[0]
    assert (shown.waiting, shown.restarts) == ("", 0)


async def test_claude_that_stopped_part_way_carries_on(projects, github):
    clock = projects.clock
    project = await hand(projects)
    branch = f"project/{project.folder}"
    github.commit(branch, {f"{project.folder}/status.json": status("in progress", S1={"status": "done"})})
    await projects.refresh()
    clock.wait(119 * 60)
    assert await projects.refresh() == []  # still working, as far as Athena can tell
    clock.wait(2 * 60)  # two hours without a push
    (said,) = await projects.refresh()
    assert said.startswith(
        "⏳ Claude stopped part way through “Bank statements”, with nothing pushed for 2 hours: maybe it reached "
        "your usage limit. I've asked it to carry on from where it got to"
    )
    assert projects.projects[0].waiting == "stopped" and github.labelled()[-1] == ("labeled", 1, "claude")

    # Done or stuck, it's left alone however long it's quiet.
    github.commit(branch, {f"{project.folder}/status.json": status("blocked")})
    await projects.refresh()
    restarts = len(github.labelled())
    clock.wait(48 * HOUR)
    assert await projects.refresh() == [] and len(github.labelled()) == restarts


async def test_athena_gives_up_and_says_so_then_you_can_restart_it(make, github):
    projects = make(max_restarts=2)
    clock = projects.clock
    await hand(projects)
    told = []
    for _ in range(12):
        clock.wait(4 * HOUR)
        told += await projects.refresh()
    assert len(github.labelled()) == 3  # handed over, then two tries
    assert len(told) == 2 and told[1].startswith(
        "⚠️ Claude still hasn't started on “Bank statements” after 2 tries. Check its routine at "
        "claude.ai/code/routines, then ask me to start it again."
    )
    assert projects.projects[0].gave_up

    # You fix the routine and ask Athena to start it again.
    said = await tool(projects, "claude_projects").handler({"project": "bank", "restart": True})
    assert said.startswith("Started Claude again on “Bank statements”.")
    assert len(github.labelled()) == 4 and not projects.projects[0].gave_up
    clock.wait(31 * 60)
    await projects.refresh()
    assert len(github.labelled()) == 5  # and it keeps trying again
    with pytest.raises(ToolError, match="no open project"):
        await projects.restart("nothing")


async def test_your_answer_reaches_claude(projects, github):
    project = await hand(projects)
    branch = f"project/{project.folder}"
    github.commit(branch, {f"{project.folder}/status.json": status("blocked")})
    await projects.refresh()
    answer = tool(projects, "answer_claude")
    assert answer.needs_confirmation
    args = {"project": "bank", "answer": "It's Monzo."}
    assert await answer.preview(args) == (
        f"This adds the answer below to {project.folder}/Answers.md on its branch in {REPO} (private), and starts "
        "Claude again on “Bank statements”."
    )
    before = len(github.labelled())
    assert (await answer.handler(args)).startswith("Passed the answer on, and started Claude again")
    first = github.files(branch)[f"{project.folder}/Answers.md"]
    assert re.fullmatch(r"# Answers\n\n## \d{4}-\d\d-\d\d \d\d:\d\d\n\nIt's Monzo.\n", first)
    assert len(github.labelled()) == before + 1

    await answer.handler({"project": project.folder, "answer": "And the joint account."})
    second = github.files(branch)[f"{project.folder}/Answers.md"]
    assert second.startswith(first.rstrip()) and second.endswith("And the joint account.\n")
    assert f"{project.folder}/Answers.md" not in github.files("main")

    for bad, why in [
        ({"project": "boats", "answer": "x"}, "No open project"),
        ({"project": "bank"}, "Give the answer"),
    ]:
        with pytest.raises(ToolError, match=why):
            await answer.preview(bad)


async def test_asking_how_claude_is_getting_on(projects, github):
    listing = tool(projects, "claude_projects").handler
    assert await listing({}) == "There are no projects for Claude."
    project = await hand(projects)
    said = {"status": "in progress", "summary": "Halfway.", "stories": {"S1": {"status": "done", "note": "Tested."}}}
    github.commit(f"project/{project.folder}", {f"{project.folder}/status.json": json.dumps(said)})
    await projects.refresh()
    assert (
        await listing({}) == f"{project.folder}: Bank statements (in progress, 1 of 2 stories done). Claude: Halfway."
    )
    detail = await listing({"project": "bank"})
    assert detail.endswith("- S1 Read the bank's CSV: done. Tested.\n- S2 Monthly totals: to do")
    with pytest.raises(ToolError, match="No project matches"):
        await listing({"project": "boats"})


# -- setting it up --------------------------------------------------------------------------------------


async def test_doctor_says_whether_athena_can_push(github, config):
    config.claude_projects = ClaudeProjectsConfig(enabled=True, repo=REPO, token="good-token")
    said = []

    async def check():
        said.clear()
        await ClaudeProjects.check(config, lambda mark, text: said.append((mark, text)), http=github.client())
        return said

    assert await check() == [(OK, f"can read and write {REPO}, which is private")]
    github.commits[github.heads["main"]][0].pop("CLAUDE.md")
    github.commits[github.heads["main"]][0]["README.md"] = "x"
    missing = f"{REPO} has no CLAUDE.md on main: copy Athena's docs/claude-projects/CLAUDE.md"
    assert (await check())[1] == (WARN, missing)
    github.push = False
    can_not = f"the token can read {REPO} but not write to it: give it Contents, read and write"
    assert (await check())[0] == (FAIL, can_not)
    github.private = False
    assert (await check())[0][0] == FAIL and "is public" in said[0][1]


def test_it_needs_a_token_and_a_repo(config):
    config.claude_projects = ClaudeProjectsConfig(enabled=True, repo=REPO)
    with pytest.raises(Unavailable, match="PROJECTS_GITHUB_TOKEN isn't set"):
        ClaudeProjects.start(config)
    config.claude_projects = ClaudeProjectsConfig(enabled=True, token="t")  # the example repo
    with pytest.raises(Unavailable, match="say which repository"):
        ClaudeProjects.start(config)


def test_a_task_always_asks_before_pushing_to_github():
    from athena.tools import NEVER_UNASKED

    assert {"hand_to_claude", "answer_claude", "extend_claude_project"} <= NEVER_UNASKED


# -- more work on a project ------------------------------------------------------------------------------

MORE = [{"title": "Export to a spreadsheet", "done_when": ["An .xlsx with a sheet per month"]}]


def merge(github, number):
    """Merge a pull request, as you would on GitHub, deleting its branch."""
    pr = github.pulls[number - 1]
    github.commit("main", github.files(pr["branch"]))
    pr["state"], pr["merged_at"] = "closed", "2026-10-17T10:00:00Z"


def test_more_stories_read_back_after_the_first():
    text = (
        render("Bank statements", "Sum it up.", STORIES)
        + "\n"
        + render_more(3, "Totals are hard to share.", MORE, "2026-10-20")
    )
    project = parse("bank-statements-2026-10-15", text)
    assert [(s.id, s.title) for s in project.stories] == [
        ("S1", "Read the bank's CSV"),
        ("S2", "Monthly totals"),
        ("S3", "Export to a spreadsheet"),
    ]
    assert project.stories[2].done_when == ["An .xlsx with a sheet per month"]
    assert project.notes == "Changes, 2026-10-20:\n\nTotals are hard to share."
    assert folder_of("project/bank-statements-2026-10-15") == ("bank-statements-2026-10-15", 1)
    assert folder_of("project/bank-statements-2026-10-15-2--3") == ("bank-statements-2026-10-15-2", 3)

    # Claude said it was done, before the new story: it isn't now, and hasn't started on the new one.
    apply_status(project, status("done", S1="done", S2="done"))
    assert (project.status, project.started) == ("in progress", False)
    apply_status(project, status("in progress", S1="done", S2="done", S3="to do"))
    assert project.started
    first_round = parse("p", render("P", "S.", STORIES))
    apply_status(first_round, status("in progress"))  # any status.json means Claude's started a first round
    assert first_round.started


async def test_more_work_on_an_open_project_goes_on_its_branch(projects, github):
    project = await hand(projects)
    branch = f"project/{project.folder}"
    github.commit(branch, {f"{project.folder}/status.json": status("done", S1="done", S2="done")})
    told = await projects.refresh()
    assert len(told) == 1 and told[0].startswith("✅")

    extend = tool(projects, "extend_claude_project")
    assert extend.needs_confirmation
    args = {"project": "bank", "summary": "Totals are hard to share.", "stories": MORE}
    assert await extend.preview(args) == (
        f"This adds 1 story (S3) to {project.folder}/Requirements.md on its branch in {REPO} (private), and starts "
        "Claude again on “Bank statements”, made from everything below."
    )
    labelled = len(github.labelled())
    said = await extend.handler(args)
    assert said.startswith(
        "Added to “Bank statements”, on pull request #1: Claude's starting on it, with 1 story to do."
    )
    requirements = github.files(branch)[f"{project.folder}/Requirements.md"]
    assert requirements.startswith(render("Bank statements", "Sum up my spending.", STORIES))
    assert f"## Changes, {today()}\n\nTotals are hard to share.\n\n### S3: Export to a spreadsheet" in requirements
    assert len(github.labelled()) == labelled + 1 and len(github.pulls) == 1  # Claude's started again
    shown = projects.projects[0]
    assert [(s.id, s.status) for s in shown.stories] == [("S1", "done"), ("S2", "done"), ("S3", "to do")]
    assert shown.status == "in progress"

    # On the board: not done, and Claude hasn't started on the new story...
    assert await projects.refresh() == []
    assert (projects.projects[0].status, projects.projects[0].started) == ("in progress", False)
    # ...until it lists it. Then when it's done, you're told again.
    github.commit(branch, {f"{project.folder}/status.json": status("done", S1="done", S2="done", S3="done")})
    (done,) = await projects.refresh()
    assert done.startswith("✅ Claude has finished “Bank statements”")


async def test_more_work_on_a_merged_project_is_a_new_round(projects, github):
    project = await hand(projects)
    branch = f"project/{project.folder}"
    github.commit(branch, {f"{project.folder}/status.json": status("done", S1="done", S2="done")})
    github.commit(branch, {f"{project.folder}/Solution.md": "# How"})
    merge(github, 1)
    await projects.refresh()
    assert projects.projects[0].pr == "merged"

    extend = tool(projects, "extend_claude_project")
    args = {"project": project.folder, "summary": "Totals are hard to share.", "stories": MORE * 2}
    assert await extend.preview(args) == (
        f"“Bank statements” is merged, so this opens a new pull request for it in {REPO} (private), from a new branch "
        f"adding 2 stories (S3–S4) to {project.folder}/Requirements.md, and labels it “claude”, which starts Claude: "
        "made from everything below."
    )
    await extend.handler(args)
    second = f"project/{project.folder}--2"
    pr = github.pulls[1]
    assert (pr["branch"], pr["title"]) == (second, "Bank statements (2)")
    assert pr["body"].startswith("More work on [Bank statements](https://github.com/someone/zeus-solutions/pull/1)")
    assert github.labelled()[-1] == ("labeled", 2, "claude")
    files = github.files(second)
    assert files[f"{project.folder}/Solution.md"] == "# How"  # it starts from what was merged
    assert "### S4: Export to a spreadsheet" in files[f"{project.folder}/Requirements.md"]
    assert "### S3" not in github.files("main")[f"{project.folder}/Requirements.md"]  # main waits for the merge

    # One epic for the folder, showing its latest round, with the first linked.
    assert await projects.refresh() == []
    (shown,) = projects.projects
    assert (shown.number, shown.round, shown.branch, shown.status, shown.started) == (
        2,
        2,
        second,
        "in progress",
        False,
    )
    assert shown.rounds == [{"number": 1, "url": f"https://github.com/{REPO}/pull/1", "pr": "merged", "round": 1}]
    assert [s.status for s in shown.stories] == ["done", "done", "to do", "to do"]
    assert "(in progress, 2 of 4 stories done), round 2" in projects._describe(shown)

    # If Claude doesn't start on it, it's started again, as for any project.
    projects.clock.wait(31 * 60)
    (said,) = await projects.refresh()
    assert said.startswith("⏳ Claude hasn't started on “Bank statements”") and github.labelled()[-1][1] == 2

    # An answer goes to this round's branch, and a third round after this one's merged gets its own.
    github.commit(second, {f"{project.folder}/status.json": status("blocked", S3="blocked", S4="to do")})
    await projects.refresh()
    await projects.answer(projects.projects[0], "Use openpyxl.")
    assert f"{project.folder}/Answers.md" in github.files(second)
    merge(github, 2)
    await projects.refresh()
    await projects.extend(projects.projects[0], {"summary": "Charts.", "stories": MORE})
    assert github.pulls[2]["branch"] == f"project/{project.folder}--3"
    await projects.refresh()
    assert [r["number"] for r in projects.projects[0].rounds] == [1, 2]

    # A new project with the same name that day still gets a folder of its own.
    assert (await hand(projects)).folder == f"{project.folder}-2"


async def test_more_work_that_isnt_right_is_refused_before_asking(projects, github):
    await hand(projects)
    extend = tool(projects, "extend_claude_project")
    before = len(github.requests)
    for args, why in [
        ({"project": "bank", "stories": MORE}, "Say what's changing"),
        ({"project": "bank", "summary": "s", "stories": []}, "between 1 and 20 stories"),
        ({"project": "boats", "summary": "s", "stories": MORE}, "No project matches"),
    ]:
        with pytest.raises(ToolError, match=why):
            await extend.preview(args)
    assert len(github.requests) == before
    with pytest.raises(ToolError, match="hand the rest over as a project of its own"):
        check_stories(MORE * 15, already=30)
