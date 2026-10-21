"""Running Python the model writes: the built-in `run_python` tool, for sums, data and files.

The code runs in the strict sandbox (sandbox.py's ``wrap_code``): it sees only what Python needs, read-only,
including the packages Athena has (numpy and pandas among them), and a workspace folder, data/workspace,
which is its working directory and keeps its files from one run to the next. It has no network, none of
Athena's settings, secrets, memory or your files, and no environment but a few harmless settings.

Before it starts, a few lines of Python in the sandbox lower its limits on memory, CPU time, the size of
any file it writes and how many it can open, which it can't raise again; and it's stopped after
[code] timeout_seconds. What it prints comes back to the model, up to [code] max_output_chars.

Without bubblewrap there's no sandbox, and so no tool: code is never run any other way.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from pydantic import Field

from pi_assistant.builtin import FAIL, OK, Builtin, Report, Section, Unavailable
from pi_assistant.catalog import Integration
from pi_assistant.sandbox import INSTALL_HINT, Sandbox, SandboxError
from pi_assistant.tools import Tool, ToolError

if TYPE_CHECKING:
    from pi_assistant.config import Config

MAX_CODE_CHARS = 50_000
# Runs in the sandbox before the model's code: lowers the limits, then runs what's on stdin as __main__.
LAUNCHER = """\
import resource, signal, sys
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # writing too much fails with an error, rather than killing it
for limit, value in {limits}:
    resource.setrlimit(limit, (value, value))
source = sys.stdin.read()
sys.stdin = open("/dev/null")
exec(compile(source, "<code>", "exec"), {{"__name__": "__main__", "__builtins__": __builtins__}})
"""
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "MPLBACKEND": "Agg"}


class CodeConfig(Section):
    # The run_python tool: Python the model writes, run in a sandbox with no network (README, "Running code").
    enabled: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)  # it's stopped after this long
    memory_mb: int = Field(default=1024, ge=128)
    max_output_chars: int = Field(default=10_000, ge=500)  # what it prints that's given back to the model
    max_file_mb: int = Field(default=50, ge=1)  # the largest file it can write
    workspace_mb: int = Field(default=500, ge=10)  # it won't run while its folder holds more than this


def workspace_of(cfg: Config) -> Path:
    """Where the code's files are kept: data/workspace."""
    return cfg.resolve(cfg.data_dir) / "workspace"


class CodeRunner(Builtin):
    section = "code"
    Settings = CodeConfig
    entry = Integration(
        "code",
        "Running code",
        "Runs Python that Athena writes, for sums, data and files, in a sandbox with no network that sees "
        "nothing of yours but its own folder.",
        "code",
        builtin=True,
        asks="Never asks: the code can't reach the network or anything of yours outside its own folder.",
    )

    @classmethod
    def wanted(cls, cfg: Config) -> bool:
        """Switched on, where there can be a sandbox: on Linux. A Mac used for development has no code tool."""
        return cfg.code.enabled and Sandbox.detect().supported

    @classmethod
    def start(cls, cfg: Config) -> Self:
        sandbox = Sandbox.detect()
        if not sandbox.bwrap:
            raise Unavailable(f"It needs a sandbox, which needs Linux and bubblewrap. {INSTALL_HINT.split(',')[0]}.")
        return cls(cfg.code, sandbox, workspace_of(cfg))

    @classmethod
    async def check(cls, cfg: Config, report: Report) -> None:
        try:
            runner = cls.start(cfg)
        except Unavailable as exc:
            report(FAIL, str(exc))
            return
        checks = {
            "runs Python": ("print(6 * 7)", "42"),
            "can't connect out": (
                "import socket\ntry:\n    socket.create_connection(('1.1.1.1', 53), 3)\n    print('connected')\n"
                "except OSError:\n    print('no network')",
                "no network",
            ),
            "can't see Athena's files": (f"import os\nprint(os.path.exists({str(cfg.base_dir)!r}))", "False"),
        }
        for what, (code, expected) in checks.items():
            try:
                said = (await runner.run(code)).output.strip()
            except (ToolError, SandboxError) as exc:
                report(FAIL, f"Code {what}? {exc}")
                continue
            report(OK if said == expected else FAIL, f"Code {what}" + ("" if said == expected else f": {said[:200]}"))

    def __init__(self, cfg: CodeConfig, sandbox: Sandbox, workspace: Path):
        self.cfg = cfg
        self.sandbox = sandbox
        self.workspace = workspace
        self._lock = asyncio.Lock()  # one run at a time: the Pi is small

    def tools(self) -> list[Tool]:
        async def run_python(args: dict[str, Any]) -> str:
            result = await self.run(str(args.get("code") or ""))
            return result.describe()

        return [
            Tool(
                name="run_python",
                description=(
                    "Run Python 3 code and get back what it prints: for sums, statistics, dates, and reading or "
                    "making files such as CSVs. numpy and pandas are there. It runs in a sandbox with no network "
                    "and nothing of the user's, in a folder of its own that keeps files between runs. Print what "
                    f"you need. It's stopped after {self.cfg.timeout_seconds:g} seconds."
                ),
                parameters={
                    "type": "object",
                    "properties": {"code": {"type": "string", "description": "The Python to run."}},
                    "required": ["code"],
                },
                handler=run_python,
            )
        ]

    async def run(self, code: str) -> Ran:
        """Run ``code`` in the sandbox, in the workspace, within the limits."""
        if not code.strip():
            raise ToolError("Give the code to run.")
        if len(code) > MAX_CODE_CHARS:
            raise ToolError(f"That's more than {MAX_CODE_CHARS:,} characters of code: write it in parts.")
        await self.sandbox.check()
        async with self._lock:
            self.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
            before = _files(self.workspace)
            full = sum(size for size, _ in before.values()) > self.cfg.workspace_mb * 1024 * 1024
            started = time.monotonic()
            code_bytes = code.encode()
            launcher = self._launcher(writes=not full)
            command = self.sandbox.wrap_code([sys.executable, "-I", "-c", launcher], self.workspace, ENV)
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
            output, cut = b"", False
            try:
                async with asyncio.timeout(self.cfg.timeout_seconds):
                    assert proc.stdin and proc.stdout
                    proc.stdin.write(code_bytes)
                    with contextlib.suppress(ConnectionError):
                        await proc.stdin.drain()
                    proc.stdin.close()
                    limit = self.cfg.max_output_chars * 4  # bytes: enough for that many characters of any kind
                    while chunk := await proc.stdout.read(65536):
                        if len(output) < limit:
                            output += chunk
                        else:
                            cut = True  # kept reading, so it isn't held up writing, but not kept
                    await proc.wait()
                    timed_out = False
            except TimeoutError:
                timed_out = True
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            seconds = time.monotonic() - started
        text = output.decode(errors="replace")
        if len(text) > self.cfg.max_output_chars:
            text, cut = text[: self.cfg.max_output_chars], True
        changed = _changes(before, _files(self.workspace))
        return Ran(text, proc.returncode, timed_out, cut, seconds, changed, full, self.cfg)

    def _launcher(self, writes: bool = True) -> str:
        """The launcher, with the limits filled in. ``writes``: False to let it write nothing at all."""
        mb = 1024 * 1024
        limits = [
            ("resource.RLIMIT_AS", self.cfg.memory_mb * mb),
            ("resource.RLIMIT_CPU", int(self.cfg.timeout_seconds) + 1),
            ("resource.RLIMIT_FSIZE", self.cfg.max_file_mb * mb if writes else 0),
            ("resource.RLIMIT_NOFILE", 256),
            ("resource.RLIMIT_CORE", 0),
        ]
        return LAUNCHER.format(limits="(" + ", ".join(f"({name}, {value})" for name, value in limits) + ",)")


@dataclass
class Ran:
    """How a run went: what it printed, and what it changed in the workspace."""

    output: str
    code: int | None  # its exit code; negative if a signal stopped it
    timed_out: bool
    cut: bool  # it printed more than is kept
    seconds: float
    changed: list[str]  # files in the workspace, and how
    full: bool  # the workspace was over its limit, so it could write nothing
    cfg: CodeConfig

    def describe(self) -> str:
        """What the model's told."""
        if self.timed_out:
            head = f"Stopped after {self.cfg.timeout_seconds:g} seconds, its limit."
        elif self.code == 0:
            head = f"Finished in {self.seconds:.1f}s."
        elif self.code is not None and self.code < 0:
            head = f"Stopped by signal {-self.code}: it may have run out of memory or CPU time."
        else:
            head = f"Failed (exit code {self.code})."
        parts = [head, self.output.rstrip() or "(it printed nothing)"]
        if self.full:
            parts.append(
                f"[its folder holds more than {self.cfg.workspace_mb} MB, so it couldn't write anything: delete files "
                "that aren't needed, with os.remove]"
            )
        if self.cut:
            parts.append(f"[cut short: only the first {self.cfg.max_output_chars:,} characters are shown]")
        if self.changed:
            parts.append("Files in its folder that changed: " + ", ".join(self.changed))
        return "\n".join(parts)


def _files(folder: Path) -> dict[str, tuple[int, int]]:
    """The files in ``folder``, by path within it: (size, when changed). Links are listed, never followed."""
    found = {}
    for root, dirs, names in os.walk(folder):  # os.walk doesn't follow links into folders
        for name in names:
            path = Path(root) / name
            with contextlib.suppress(OSError):
                info = path.lstat()
                found[str(path.relative_to(folder))] = (info.st_size, info.st_mtime_ns)
        dirs.sort()
    return found


def _changes(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
    changed = [f"{name} ({_show_size(after[name][0])}{', new' if name not in before else ''})"
               for name in sorted(after) if before.get(name) != after[name]]  # fmt: skip
    changed += [f"{name} (deleted)" for name in sorted(set(before) - set(after))]
    return changed


def _show_size(size: int) -> str:
    for unit, scale in (("MB", 1024 * 1024), ("KB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} bytes"


def clear_workspace(cfg: Config) -> None:
    """Delete the workspace's files, as forgetting everything does."""
    shutil.rmtree(workspace_of(cfg), ignore_errors=True)
