"""Locked environments for the MCP servers config.toml starts with uvx.

`uvx mcp-server-fetch==2026.8.18` pins the server, but not the 44 packages it depends on, which
uvx takes at whatever version is newest when it installs them. So each server started that way
gets a small uv project of its own, in data/mcp/<name>/, whose uv.lock records every package
and its hash, and Athena runs the server from that project's environment:

  * The lock comes from mcp-locks/<name>/ in this repo if there's one for the same version (the
    recommended servers' locks are there, so changes to them show up in pull requests and CI
    checks them for known vulnerabilities). Otherwise it's made when the server is first used.
    Either way it only changes when the server's version in config.toml does.
  * Installing checks every hash, installs only ready-built wheels (so no package's own code
    runs while it's installed), and asks OSV whether any locked package is known malware,
    stopping if it is or if OSV can't be asked. That happens once for each lock: after that the
    environment is reused, so starting a server needs no network.

Servers started some other way (ssh, npx, a URL) are left as they are.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_assistant.config import Config

log = logging.getLogger(__name__)

INSTALL_TIMEOUT = 600  # seconds: the first install of a big server on a Pi takes a while
_STAMP = ".athena-lock"  # in the environment: the hash of the uv.lock it was installed from
# A requirement uvx accepts: name, optional [extras], optional ==version or @version.
_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9,._-]+\])?(?:(?:==|@)([A-Za-z0-9.+!_-]+))?$")
_COMMAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ServerEnvError(Exception):
    """A server's environment couldn't be made. The message says why, for the dashboard."""


@dataclass
class Audit:
    clean: bool | None  # None if it couldn't be checked
    summary: str
    affected: list[str] = field(default_factory=list)  # "idna 2.7", for each package with known vulnerabilities


@dataclass(frozen=True)
class UvxServer:
    requirement: str  # e.g. "edgartools[ai]==5.60.0"
    command: str  # the executable uvx would run, e.g. "edgartools-mcp"
    args: tuple[str, ...] = ()

    @property
    def pinned(self) -> bool:
        return "==" in self.requirement


def parse_uvx(args: list[str]) -> UvxServer | None:
    """What ``uvx ARGS`` runs, if it's in a form that can be locked: ``[--from REQUIREMENT] COMMAND [ARGS...]``
    or ``REQUIREMENT [ARGS...]``. None for anything else, such as other uvx options."""
    args = list(args)
    source = None
    if len(args) >= 3 and args[0] == "--from":
        source, args = args[1], args[2:]
    elif args and args[0].startswith("--from="):
        source, args = args[0].removeprefix("--from="), args[1:]
    if not args or args[0].startswith("-"):
        return None
    target, rest = args[0], tuple(args[1:])
    if source is None:
        source = target
        match = _REQUIREMENT.match(target)
        command = match.group(1) if match else ""
    else:
        match = _REQUIREMENT.match(source)
        command = target if _COMMAND.match(target) else ""
    if not match or not command:
        return None
    name, extras, version = match.groups()
    return UvxServer(name + (extras or "") + (f"=={version}" if version else ""), command, rest)


def project_text(name: str, server: UvxServer) -> str:
    """The pyproject.toml for a server's environment. A lock is reused only for exactly this text."""
    return (
        f"# Made by Athena for the MCP server '{name}'. Don't edit it: change the server in config.toml.\n"
        "[project]\n"
        f'name = "athena-mcp-{name.replace("_", "-")}"\n'
        'version = "0"\n'
        'requires-python = ">=3.11"\n'
        f'dependencies = ["{server.requirement}"]\n'
        "\n"
        "[tool.uv]\n"
        "package = false\n"
    )


class ServerEnvs:
    @classmethod
    def for_config(cls, cfg: Config) -> ServerEnvs:
        return cls(cfg.resolve(cfg.data_dir) / "mcp", cfg.base_dir / "mcp-locks")

    def __init__(
        self,
        root: Path,
        reviewed: Path | None = None,
        *,
        uv: str | None = None,
        python: str | None = None,
        malware_check: bool = True,
        uv_env: dict[str, str] | None = None,
    ):
        self.root = root  # data/mcp
        self.reviewed = reviewed  # mcp-locks in the repo
        self.uv = uv or shutil.which("uv") or "uv"
        self.python = python or sys.executable
        self.malware_check = malware_check
        self.uv_env = uv_env or {}  # extra settings for uv, e.g. a local package index in the tests
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_origin(self, name: str) -> str | None:
        """Where a server's lock came from: "reviewed" (mcp-locks/) or "first use". None if it has none."""
        path = self.root / name / "origin"
        return path.read_text().strip() if path.exists() else None

    async def prepare(self, name: str, server: UvxServer) -> list[str]:
        """Lock and install a server if that isn't done yet, and return the command to start it."""
        async with self._locks.setdefault(name, asyncio.Lock()):
            async with asyncio.timeout(INSTALL_TIMEOUT):
                return await self._prepare(name, server)

    async def _prepare(self, name: str, server: UvxServer) -> list[str]:
        project = self.root / name
        text = project_text(name, server)
        lock = project / "uv.lock"
        if not lock.exists() or _read(project / "pyproject.toml") != text:
            project.mkdir(parents=True, exist_ok=True)
            lock.unlink(missing_ok=True)
            (project / "pyproject.toml").write_text(text)
            reviewed = self.reviewed / name if self.reviewed else None
            if reviewed and _read(reviewed / "pyproject.toml") == text and (reviewed / "uv.lock").exists():
                shutil.copyfile(reviewed / "uv.lock", lock)
                origin = "reviewed"
            else:
                log.info("Locking %s for the MCP server '%s'", server.requirement, name)
                await self._uv(["lock"], project, f"Couldn't work out {server.requirement}'s dependencies")
                if not lock.exists():
                    raise ServerEnvError(f"uv didn't lock {server.requirement}'s dependencies.")
                origin = "first use"
            (project / "origin").write_text(origin + "\n")

        venv = project / ".venv"
        digest = hashlib.sha256(lock.read_bytes()).hexdigest()
        if _read(venv / _STAMP) != digest:
            log.info("Installing %s for the MCP server '%s'", server.requirement, name)
            (venv / _STAMP).unlink(missing_ok=True)
            await self._uv(
                ["sync", "--frozen", "--no-build", "--no-install-project", "--python", self.python],
                project,
                f"Couldn't install {server.requirement}",
                {"UV_MALWARE_CHECK": "1", "UV_PREVIEW_FEATURES": "malware-check"} if self.malware_check else {},
            )
            (venv / _STAMP).write_text(digest)
        executable = venv / "bin" / server.command
        if not executable.exists():
            raise ServerEnvError(f"{server.requirement} has no command called '{server.command}'.")
        return [str(executable), *server.args]

    async def audit(self, name: str) -> Audit:
        """Check a server's locked packages against OSV's list of known vulnerabilities."""
        project = self.root / name
        if not (project / "uv.lock").exists():
            return Audit(None, "not installed yet")
        try:
            code, output = await self._run(["audit", "--frozen", "--preview-features", "audit-command"], project)
        except OSError as exc:
            return Audit(None, f"couldn't run uv: {exc}")
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if code in (0, 1) and lines and lines[0].startswith("Found"):
            affected = [m.group(1) for line in lines if (m := re.match(r"^(\S+ \S+) has \d+ known vulnerabilit", line))]
            return Audit(code == 0, lines[0], affected)
        if any("unrecognized subcommand" in line for line in lines):
            return Audit(None, "this uv can't audit: update it with `uv self update`")
        return Audit(None, "couldn't check: " + next((ln for ln in lines if ln.startswith("error")), "no answer"))

    async def _uv(self, args: list[str], cwd: Path, failure: str, extra: dict[str, str] | None = None) -> None:
        code, output = await self._run(args, cwd, extra)
        if code:
            # uv's own words: its error line and the reason under it, or a malware finding.
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            said = [line for line in lines if re.match(r"(error|caused by)\b", line, re.I) or "MAL-" in line]
            detail = " ".join(said or lines[-1:]).removeprefix("error: ")
            raise ServerEnvError(f"{failure}: {detail}" if detail else f"{failure}.")

    async def _run(self, args: list[str], cwd: Path, extra: dict[str, str] | None = None) -> tuple[int, str]:
        # uv gets only what it needs: not Athena's secrets, which a package's code could otherwise read.
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR"} or k.startswith("UV_")}
        env.update(self.uv_env | (extra or {}))
        env.pop("VIRTUAL_ENV", None)
        proc = await asyncio.create_subprocess_exec(
            self.uv,
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await proc.communicate()
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        return proc.returncode or 0, output.decode(errors="replace")


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def lock_recommended(repo: Path) -> list[str]:
    """Make mcp-locks/ match the uvx servers in config.example.toml: run from the repo with
    `uv run python -m pi_assistant.server_envs`. Unchanged servers keep their locks."""
    import subprocess
    import tomllib

    servers = tomllib.loads((repo / "config.example.toml").read_text()).get("mcp_servers", {})
    locks = repo / "mcp-locks"
    wanted: dict[str, str] = {}
    for name, cfg in servers.items():
        server = parse_uvx(cfg.get("args", [])) if cfg.get("command") == "uvx" else None
        if server:
            wanted[name] = project_text(name, server)
    changed = []
    for name, text in wanted.items():
        project = locks / name
        if _read(project / "pyproject.toml") == text and (project / "uv.lock").exists():
            continue
        project.mkdir(parents=True, exist_ok=True)
        (project / "pyproject.toml").write_text(text)
        (project / "uv.lock").unlink(missing_ok=True)
        subprocess.run(["uv", "lock", "--quiet"], cwd=project, check=True)
        changed.append(name)
    for project in sorted(locks.iterdir()) if locks.exists() else []:
        if project.is_dir() and project.name not in wanted:
            shutil.rmtree(project)
            changed.append(project.name)
    return changed


if __name__ == "__main__":
    print("Changed:", ", ".join(lock_recommended(Path.cwd())) or "nothing")
