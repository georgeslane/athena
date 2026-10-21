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
  * When a reviewed lock in mcp-locks/ changes, say for a security fix, servers using it take
    the new one. A lock fixed here (fix(), below) is kept until the reviewed one changes again.
  * audit() asks OSV whether any locked package has known vulnerabilities, and fix() updates
    just those packages, each to the lowest version that fixes all of its own, and nothing else.
    The environment's installed again from the new lock, with the same checks, when the server
    next starts. Athena does this every day by itself (app.py), and the dashboard can too.

Servers started some other way (ssh, npx, a URL) are left as they are.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from pi_assistant.config import Config

log = logging.getLogger(__name__)

INSTALL_TIMEOUT = 600  # seconds: the first install of a big server on a Pi takes a while
_STAMP = ".athena-lock"  # in the environment: the hash of the uv.lock it was installed from
_REVIEWED = "reviewed-lock"  # in the project: the hash of the reviewed lock it last took
_FIXED = "fixed"  # in the project: what fix() updated, one line each
# A requirement uvx accepts: name, optional [extras], optional ==version or @version.
_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9,._-]+\])?(?:(?:==|@)([A-Za-z0-9.+!_-]+))?$")
_COMMAND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ServerEnvError(Exception):
    """A server's environment couldn't be made. The message says why, for the dashboard."""


@dataclass(frozen=True)
class Finding:
    """A locked package with known vulnerabilities."""

    package: str
    version: str
    ids: tuple[str, ...]  # the advisories, such as "GHSA-6q48-6c7w-9gw2"
    summary: str  # the first advisory's, in a line
    fixed: str | None  # the lowest version that fixes them all, if there is one

    def describe(self) -> str:
        return f"{self.package} {self.version} ({', '.join(self.ids)}: {self.summary})"


@dataclass
class Audit:
    clean: bool | None  # None if it couldn't be checked
    summary: str
    affected: list[str] = field(default_factory=list)  # "idna 2.7", for each package with known vulnerabilities
    findings: list[Finding] = field(default_factory=list)


@dataclass
class Fixed:
    """What fix() did."""

    before: Audit
    after: Audit
    updated: list[tuple[str, str, str]] = field(default_factory=list)  # (package, from version, to version)
    error: str | None = None  # why it couldn't update them, if it couldn't


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
        reviewed = self.reviewed / name if self.reviewed else None
        matches = bool(reviewed and _read(reviewed / "pyproject.toml") == text and (reviewed / "uv.lock").exists())
        if lock.exists() and _read(project / "pyproject.toml") == text and matches:
            assert reviewed is not None
            latest = hashlib.sha256((reviewed / "uv.lock").read_bytes()).hexdigest()
            if _read(project / _REVIEWED) != latest and (reviewed / "uv.lock").read_bytes() != lock.read_bytes():
                # The reviewed lock has changed since this server took it, say for a security fix in a dependency,
                # with the server's version the same: take it, and the environment's installed again from it below.
                # One that hasn't changed doesn't undo a fix made here since.
                log.info("The reviewed lock for the MCP server '%s' has changed: using it", name)
                shutil.copyfile(reviewed / "uv.lock", lock)
                (project / "origin").write_text("reviewed\n")
                (project / _FIXED).unlink(missing_ok=True)
            (project / _REVIEWED).write_text(latest)
        if not lock.exists() or _read(project / "pyproject.toml") != text:
            project.mkdir(parents=True, exist_ok=True)
            lock.unlink(missing_ok=True)
            (project / "pyproject.toml").write_text(text)
            (project / _FIXED).unlink(missing_ok=True)
            if matches:
                assert reviewed is not None
                shutil.copyfile(reviewed / "uv.lock", lock)
                (project / _REVIEWED).write_text(hashlib.sha256(lock.read_bytes()).hexdigest())
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
            code, output, said = await self._run(
                ["audit", "--frozen", "--preview-features", "audit-command", "--output-format", "json"],
                project,
                split=True,
            )
        except OSError as exc:
            return Audit(None, f"couldn't run uv: {exc}")
        try:
            report = json.loads(output)
            summary = report["summary"]
            found, packages = int(summary["vulnerabilities"]), int(summary["audited_packages"])
        except (ValueError, KeyError, TypeError):
            words = f"{said}\n{output}"
            if "unrecognized" in words or "unexpected argument" in words:
                return Audit(None, "this uv can't audit: update it with `uv self update`")
            error = next((ln.strip() for ln in words.splitlines() if ln.strip().startswith("error")), "no answer")
            return Audit(None, f"couldn't check: {error}")
        if code not in (0, 1):
            return Audit(None, f"couldn't check: uv said {code}")
        findings = _findings(report.get("vulnerabilities") or [])
        if not found:
            return Audit(True, f"Found no known vulnerabilities in {packages} packages")
        affected = [f"{f.package} {f.version}" for f in findings]
        plural = "y" if found == 1 else "ies"
        return Audit(False, f"Found {found} known vulnerabilit{plural} in {packages} packages", affected, findings)

    async def fix(self, name: str) -> Fixed:
        """Update the packages in a server's lock that have known vulnerabilities, each to the lowest version that
        fixes all of them, and nothing else. The environment's installed again from it when the server next starts,
        with the same checks as ever. Packages with no fixed version are left as they are."""
        async with self._locks.setdefault(name, asyncio.Lock()):
            project = self.root / name
            before = await self.audit(name)
            fixable = [f for f in before.findings if f.fixed]
            if before.clean is not False or not fixable:
                return Fixed(before, before)
            args = ["lock"]
            for f in fixable:
                args += ["--upgrade-package", f"{f.package}=={f.fixed}"]
            updated = [(f.package, f.version, f.fixed or "") for f in fixable]
            what = ", ".join(f"{p} {new}" for p, _, new in updated)
            log.info("Updating %s for the MCP server '%s', for known vulnerabilities", what, name)
            try:
                async with asyncio.timeout(INSTALL_TIMEOUT):
                    await self._uv(args, project, f"Couldn't update {what}")
            except (ServerEnvError, TimeoutError) as exc:
                return Fixed(before, before, error=str(exc) or "uv took too long")
            with (project / _FIXED).open("a") as kept:
                kept.writelines(f"{p} {old} -> {new}\n" for p, old, new in updated)
            return Fixed(before, await self.audit(name), updated)

    def fixed(self, name: str) -> list[str]:
        """What fix() has updated in a server's lock, since it last took a reviewed lock or was locked afresh."""
        return [line for line in (_read(self.root / name / _FIXED) or "").splitlines() if line]

    async def _uv(self, args: list[str], cwd: Path, failure: str, extra: dict[str, str] | None = None) -> None:
        code, output = await self._run(args, cwd, extra)
        if code:
            # uv's own words: its error line and the reason under it, or a malware finding.
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            said = [line for line in lines if re.match(r"(error|caused by)\b", line, re.I) or "MAL-" in line]
            detail = " ".join(said or lines[-1:]).removeprefix("error: ")
            raise ServerEnvError(f"{failure}: {detail}" if detail else f"{failure}.")

    async def _run(self, args: list[str], cwd: Path, extra: dict[str, str] | None = None, split: bool = False) -> Any:
        """Run uv. Returns its exit code and what it printed, or, if ``split``, its exit code, what it printed
        and, apart, what it said on stderr (its errors and warnings)."""
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
            stderr=asyncio.subprocess.PIPE if split else asyncio.subprocess.STDOUT,
        )
        try:
            output, errors = await proc.communicate()
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        if split:
            return proc.returncode or 0, output.decode(errors="replace"), (errors or b"").decode(errors="replace")
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


def _findings(vulnerabilities: list[dict]) -> list[Finding]:
    """Each vulnerable package once, from uv audit's JSON, with the lowest version that fixes every advisory against
    it: for each advisory, the lowest fixed version above the one locked, and the highest of those."""
    by_package: dict[tuple[str, str], list[dict]] = {}
    for v in vulnerabilities:
        dep = v.get("dependency") or {}
        by_package.setdefault((str(dep.get("name")), str(dep.get("version"))), []).append(v)
    findings = []
    for (package, version), advisories in by_package.items():
        ids = list(dict.fromkeys(str(a.get("display_id") or a.get("id")) for a in advisories))
        # The same advisory under another name (an alias) is counted once.
        seen: set[str] = set()
        needed: list[Version] = []
        fixable = True
        for a in advisories:
            names = {str(a.get("id")), *map(str, a.get("aliases") or [])}
            if names & seen:
                continue
            seen |= names
            locked = _version(version) or Version("0")
            fixes = sorted(v for v in map(_version, a.get("fix_versions") or []) if v and v > locked)
            if not fixes:
                fixable = False
                continue
            needed.append(fixes[0])
        fixed = str(max(needed)) if fixable and needed else None
        summary = " ".join(str(advisories[0].get("summary") or "").split())[:200]
        findings.append(Finding(package, version, tuple(ids), summary, fixed))
    return findings


def _version(text: object) -> Version | None:
    try:
        return Version(str(text))
    except InvalidVersion:
        return None
