"""Runs local MCP servers in a sandbox, with bubblewrap (bwrap), so a compromised one can't read
Athena's secrets or memories, or reach the network if it has no need to.

Inside, a server sees the system read-only, an empty home folder of its own that's wiped when it
stops (so your .env, data/ and ~/.ssh aren't there), its own /tmp, and nothing it starts outlives
it. On top of that it gets, read-only, only what it needs to run: its locked environment, uv's
Pythons, the folder its command is in, ~/.ssh if the command is ssh, and any `read_only_paths`
from its config. Network access is all or nothing (`network` in its config): a server with it
can reach anything Athena can, including services on the Pi itself.

bubblewrap is Linux-only. Elsewhere, such as a Mac used for development, servers run without a
sandbox and doctor says so. On Linux, a server that should be sandboxed and can't be isn't started.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

HIDDEN = ("/home", "/root", "/run/user", "/var/tmp")  # emptied in the sandbox, along with your home folder
INSTALL_HINT = "Install bubblewrap (sudo apt install bubblewrap), or set sandbox = false for the server."


class SandboxError(Exception):
    """A server can't be sandboxed here. The message says why, for the dashboard."""


@dataclass
class Sandbox:
    bwrap: str | None  # its path, or None where there's none (and so no sandboxing at all)
    home: Path
    _works: bool | None = None  # whether bwrap can make a sandbox here, once it's been tried
    _problem: str = ""

    @classmethod
    def detect(cls) -> Sandbox:
        bwrap = shutil.which("bwrap") or ("/usr/bin/bwrap" if Path("/usr/bin/bwrap").exists() else None)
        return cls(bwrap if sys.platform == "linux" else None, Path.home())

    @property
    def supported(self) -> bool:
        """False where sandboxing isn't possible at all (not Linux), so servers run as they are."""
        return sys.platform == "linux"

    async def check(self) -> None:
        """Raise SandboxError if bwrap can't make a sandbox here (missing, or user namespaces are off)."""
        if self._works is None:
            if not self.bwrap:
                self._works, self._problem = False, "bubblewrap isn't installed."
            else:
                code, output = await _run([self.bwrap, "--ro-bind", "/", "/", "--unshare-all", "--", "true"])
                self._works = code == 0
                self._problem = f"bubblewrap can't make a sandbox here: {output.strip() or f'exit {code}'}."
        if not self._works:
            raise SandboxError(f"Couldn't start it in a sandbox: {self._problem} {INSTALL_HINT}")

    def wrap(
        self,
        command: list[str],
        *,
        network: bool,
        read_only: list[Path] = (),  # type: ignore[assignment]
        cwd: Path | None = None,
    ) -> list[str]:
        """The command line that runs ``command`` in a sandbox."""
        assert self.bwrap
        args = [self.bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        for hidden in self._hidden():
            if hidden.is_dir():
                # Nothing in anyone's home, nor their session sockets. Its own home is empty and
                # writable, and gone when it stops.
                args += ["--tmpfs", str(hidden)]
        for path in _unique([*self.needed(command), *read_only, *([cwd] if cwd else [])]):
            args += ["--ro-bind-try", str(path), str(path)]
        args += ["--unshare-all", *(["--share-net"] if network else [])]
        args += ["--die-with-parent", "--new-session", "--chdir", str(cwd or self.home), "--", *command]
        return args

    def needed(self, command: list[str]) -> list[Path]:
        """What a command needs from the home folder to run at all."""
        paths = [self.home / ".local" / "share" / "uv" / "python"]  # where uv puts the Pythons it installs
        program = Path(command[0]) if "/" in command[0] else Path(shutil.which(command[0]) or command[0])
        if program.is_absolute():
            venv = program.parent.parent
            if (venv / "pyvenv.cfg").exists():
                # A virtual environment's command needs the whole environment, and the Python it was made from.
                paths += [venv, *_base_python(venv)]
            else:
                paths.append(program.parent)
            if program.resolve() != program:
                paths.append(program.resolve().parent)  # where a link to it leads
        if program.name == "ssh":
            paths.append(self.home / ".ssh")  # ssh itself is trusted: it's for the Mac's servers
        # Only what's in a hidden folder needs showing: the rest is visible anyway.
        return [p for p in _unique(paths) if any(_within(p, hidden) for hidden in self._hidden())]

    def _hidden(self) -> list[Path]:
        return _unique([*map(Path, HIDDEN), self.home])

    async def self_test(self) -> list[tuple[bool, str]]:
        """Checks, for doctor, that a sandboxed process can't see a file in your home folder, or reach the network."""
        await self.check()
        results = []
        canary = self.home / f".athena-sandbox-check-{os.getpid()}"
        canary.write_text("If a sandboxed server can see this, the sandbox isn't working.\n")
        try:
            code, _ = await _run(self.wrap(["/bin/sh", "-c", 'test -e "$1"', "sh", str(canary)], network=False))
        finally:
            canary.unlink(missing_ok=True)
        results.append(
            (code != 0, f"a sandboxed server {'can' if code == 0 else 'can’t'} see files in your home folder")
        )
        python = next((p for p in ("/usr/bin/python3", "/bin/python3") if Path(p).exists()), None)
        if python:
            probe = "import socket; socket.create_connection(('1.1.1.1', 53), 3)"
            code, _ = await _run(self.wrap([python, "-c", probe], network=False))
            results.append((code != 0, f"one without network access {'can' if code == 0 else 'can’t'} connect out"))
        return results


async def _run(command: list[str]) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
    except OSError as exc:
        return 127, str(exc)
    output, _ = await proc.communicate()
    return proc.returncode or 0, output.decode(errors="replace")


def _base_python(venv: Path) -> list[Path]:
    """The installation of the Python a virtual environment was made from, as its pyvenv.cfg names it."""
    for line in (venv / "pyvenv.cfg").read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "home" and value.strip():
            installed = Path(value.strip()).parent  # home is its bin folder: take the whole installation
            return _unique([installed, installed.resolve()])
    return []


def _within(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _unique(paths: list[Path]) -> list[Path]:
    seen: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.append(path)
    return seen
