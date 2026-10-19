"""One GitHub repository, through GitHub's REST API, with a token for that repository alone: what Projects for
Claude reads and writes (claude_projects.py). Everything it can't do is a ToolError saying why, for the model
and the dashboard, and it never says the token.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from pi_assistant.tools import ToolError

API = "https://api.github.com"


class GitHubRepo:
    def __init__(
        self,
        repo: str,
        token: str,
        *,
        token_name: str,
        timeout: float = 30.0,
        http: httpx.AsyncClient | None = None,
    ):
        self.repo = repo  # owner/name
        self.token_name = token_name  # where the token is set, in .env, to say when GitHub refuses it
        self.http = http or httpx.AsyncClient(
            base_url=API,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pi-assistant (Athena)",
            },
        )
        self._blobs: dict[str, str] = {}  # file contents, by their SHA, so only what's changed is read again
        self._pushed: dict[str, float] = {}  # when each commit was made, by its SHA
        self._private: bool | None = None

    async def close(self) -> None:
        await self.http.aclose()

    # -- requests --

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """A request whose answer the caller looks at. ToolError if GitHub can't be reached or refuses the token."""
        try:
            response = await self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ToolError(f"Couldn't reach GitHub ({type(exc).__name__}).") from exc
        if response.status_code in (401, 403):
            raise ToolError(
                f"GitHub refused the token for {self.repo} ({response.status_code}): check {self.token_name} "
                "has Contents and Pull requests, read and write, on that repository."
            )
        if response.status_code == 404 and path == f"/repos/{self.repo}":
            raise ToolError(f"GitHub has no repository {self.repo} that the token can see.")
        return response

    async def call(self, method: str, path: str, **kwargs: Any) -> Any:
        """A request that must work: its JSON, or ToolError saying what GitHub said."""
        response = await self.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                said = response.json().get("message", "")
            except ValueError:
                said = ""
            raise ToolError(
                f"GitHub said {response.status_code} for {method} {path.split('?')[0]}: {said}".rstrip(": ")
            )
        return response.json() if response.content else {}

    async def get(self, path: str) -> Any:
        return await self.call("GET", path)

    # -- the repository --

    async def about(self) -> dict[str, Any]:
        """The repository: whether it's private, and what the token may do with it."""
        return await self.get(f"/repos/{self.repo}")

    async def check_private(self) -> None:
        """ToolError if the repository is public, as what's pushed to it can be personal."""
        if self._private is None:
            self._private = bool((await self.about()).get("private"))
        if not self._private:
            self._private = None  # look again next time: you may have made it private
            raise ToolError(f"{self.repo} is public. Make it private on GitHub first: requirements can be personal.")

    async def head(self, branch: str) -> str | None:
        """A branch's last commit. None if there's no such branch, or the repository is empty."""
        response = await self.request("GET", f"/repos/{self.repo}/git/ref/heads/{quote(branch)}")
        if response.status_code in (404, 409):
            return None
        if response.status_code >= 400:
            raise ToolError(f"GitHub said {response.status_code} for the {branch} branch.")
        return response.json()["object"]["sha"]

    async def branches(self, prefix: str) -> list[str]:
        """The branches whose names start with ``prefix``."""
        refs = await self.get(f"/repos/{self.repo}/git/matching-refs/heads/{prefix}")
        return [ref["ref"].removeprefix("refs/heads/") for ref in refs]

    async def make_branch(self, branch: str, sha: str) -> None:
        await self.call("POST", f"/repos/{self.repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})

    async def names(self, ref: str) -> set[str]:
        """What's at the root of the repository, at ``ref``."""
        return {item["name"] for item in await self.get(f"/repos/{self.repo}/contents?ref={quote(ref)}")}

    async def folder(self, folder: str, ref: str) -> dict[str, str] | None:
        """The files in a folder at ``ref``, as {name: blob SHA}. None if there's no such folder."""
        response = await self.request("GET", f"/repos/{self.repo}/contents/{quote(folder)}?ref={ref}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ToolError(f"GitHub said {response.status_code} reading {folder}.")
        return {item["name"]: item["sha"] for item in response.json() if item.get("type") == "file"}

    async def blob(self, sha: str) -> str:
        """A file's text, by its blob SHA."""
        if sha not in self._blobs:
            blob = await self.get(f"/repos/{self.repo}/git/blobs/{sha}")
            self._blobs[sha] = base64.b64decode(blob.get("content") or "").decode("utf-8", errors="replace")
        return self._blobs[sha]

    async def file(self, path: str, ref: str) -> tuple[str, str] | None:
        """A file's text, and its SHA for changing it, at ``ref``. None if it isn't there. ``path`` is quoted."""
        response = await self.request("GET", f"/repos/{self.repo}/contents/{path}?ref={quote(ref)}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ToolError(f"GitHub said {response.status_code} reading {path}.")
        found = response.json()
        return base64.b64decode(found.get("content") or "").decode("utf-8", "replace"), found["sha"]

    async def put(self, path: str, text: str, *, branch: str, message: str, sha: str | None = None) -> None:
        """Commit a file to a branch: a new one, or a change to the one whose SHA is ``sha``. ``path`` is quoted."""
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(text.encode()).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        await self.call("PUT", f"/repos/{self.repo}/contents/{path}", json=body)

    async def pushed_at(self, sha: str) -> float:
        """When a commit was made, as a Unix time."""
        if sha not in self._pushed:
            commit = await self.get(f"/repos/{self.repo}/commits/{sha}")
            when = ((commit.get("commit") or {}).get("committer") or {}).get("date") or ""
            self._pushed[sha] = _time(when)
        return self._pushed[sha]

    # -- pull requests --

    async def pulls(self) -> list[dict[str, Any]]:
        """Every pull request, open or closed, newest first."""
        return await self.get(f"/repos/{self.repo}/pulls?state=all&per_page=100&sort=created&direction=desc")

    async def open_pull(self, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        return await self.call(
            "POST", f"/repos/{self.repo}/pulls", json={"title": title, "head": head, "base": base, "body": body}
        )

    async def label(self, number: int, label: str) -> None:
        await self.call("POST", f"/repos/{self.repo}/issues/{number}/labels", json={"labels": [label]})

    async def relabel(self, number: int, label: str) -> None:
        """Take a label off a pull request and put it back, which fires what's triggered by it being added."""
        name = quote(label, safe="")
        response = await self.request("DELETE", f"/repos/{self.repo}/issues/{number}/labels/{name}")
        if response.status_code not in (200, 404):  # 404: it wasn't on
            raise ToolError(f"GitHub said {response.status_code} taking the label off pull request #{number}.")
        await self.label(number, label)


def _time(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
