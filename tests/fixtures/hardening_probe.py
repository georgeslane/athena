"""Run by systemd as Athena, under deploy/pi-assistant.service, by tests/test_hardening.py: tries what Athena
must be able to do there, and what it mustn't, and writes what happened to probe-results.json in its data folder.

It reads what to try from probe.json, in the folder it's started in (the test's repo).
"""

import asyncio
import ctypes
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

from pi_assistant.sandbox import Sandbox
from pi_assistant.settings import write_private

repo = Path.cwd()
plan = json.loads((repo / "probe.json").read_text())
data = Path(plan["data"])
home = Path.home()
results: dict[str, str] = {}  # what was tried: "yes" if it worked, else why not


def attempt(name, action):
    try:
        action()
        results[name] = "yes"
    except Exception as exc:
        results[name] = f"{type(exc).__name__}: {exc}"


def create(path):
    def go():
        Path(path).write_text("written by the hardening probe\n")
        Path(path).unlink()

    return go


def change(path):
    def go():
        with open(path, "r+"):  # opened to change, but never made, and left as it is
            pass

    return go


def connect(port):
    return lambda: socket.create_connection(("127.0.0.1", port), 3).close()


def sudo():
    code = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=30).returncode
    if code:
        raise PermissionError(f"sudo failed ({code})")


def uv_venv():
    done = subprocess.run(
        [plan["uv"], "venv", "--python", sys.executable, str(data / "a server's environment")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if done.returncode:
        raise RuntimeError(done.stderr.strip())


# What Athena does: its data, config.toml and .env (as the dashboard does), uv's cache, /tmp and the network.
attempt("write its data", create(data / "probe-file"))
attempt("change config.toml", lambda: write_private(repo / "config.toml", (repo / "config.toml").read_text() + "\n"))
attempt("change .env", lambda: write_private(repo / ".env", (repo / ".env").read_text() + "SET_BY_PROBE=yes\n"))
attempt("write uv's cache", create(home / ".cache" / "uv" / "probe-file"))
attempt("make an environment with uv", uv_venv)
attempt("write /tmp", create("/tmp/probe-file"))
attempt("connect to the network", connect(plan["port"]))

# What it mustn't: its code, scripts and environment, files in the repo, your home folder, sudo.
attempt("add a file to the repo", create(repo / "uv.toml"))
attempt("change a script", change(repo / "scripts" / "update.sh"))
attempt("change its environment", create(repo / ".venv" / "bin" / "probe-file"))
attempt("write to your home folder", create(home / f".athena-probe-{os.getpid()}"))
attempt("change ~/.bashrc", change(home / ".bashrc"))
attempt("see the system's /tmp", lambda: os.stat(plan["tmp_canary"]))
attempt("use sudo", sudo)


# A system call the sandbox needs, which systemd blocks for RestrictSUIDSGID=: bubblewrap 0.12 and later open
# paths with openat2, and stop if that fails. (CI's bubblewrap is older, so the sandbox below doesn't show this.)
def openat2():
    libc = ctypes.CDLL(None, use_errno=True)
    how = (ctypes.c_uint64 * 3)(os.O_PATH, 0, 0)  # struct open_how: flags, mode, resolve
    fd = libc.syscall(437, -100, b"/", ctypes.byref(how), ctypes.sizeof(how))  # SYS_openat2, AT_FDCWD
    if fd < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    os.close(fd)


attempt("use openat2", openat2)


# Anything mounted over part of /proc, which stops the sandbox mounting a /proc of its own: for when it can't.
try:
    with open("/proc/self/mountinfo") as f:
        # Except binfmt_misc, the system's own, on a folder that's always empty, which the kernel allows.
        covered = [m for m in (line.split()[4] for line in f) if m.startswith("/proc/") and "binfmt_misc" not in m]
    results["(mounted over /proc)"] = ", ".join(covered) or "nothing"
except OSError as exc:
    results["(mounted over /proc)"] = str(exc)


# The MCP servers' sandbox, within all this.
async def sandboxed():
    sandbox = Sandbox.detect()
    try:
        await sandbox.check()
    except Exception as exc:
        results["start a sandboxed server"] = f"{type(exc).__name__}: {exc}"
        return

    async def run(command, network):
        proc = await asyncio.create_subprocess_exec(
            *sandbox.wrap(command, network=network),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(f"exit {proc.returncode}: {out.decode().strip()[-300:]}")

    probe = f"import socket; socket.create_connection(('127.0.0.1', {plan['port']}), 3)"
    for name, command, network in [
        ("start a sandboxed server", [sys.executable, "-c", "pass"], False),
        ("see your home folder from the sandbox", ["/bin/sh", "-c", 'test -e "$1"', "sh", plan["home_canary"]], False),
        ("connect out from a sandbox with the network", [sys.executable, "-c", probe], True),
        ("connect out from a sandbox without it", [sys.executable, "-c", probe], False),
    ]:
        try:
            await run(command, network)
            results[name] = "yes"
        except Exception as exc:
            results[name] = f"{type(exc).__name__}: {exc}"


asyncio.run(sandboxed())


# Athena itself: its services, with an MCP server in its sandbox, its database and its dashboard, which makes
# itself a password in .env.
async def athena():
    from pi_assistant.app import build_services
    from pi_assistant.config import load_config

    services = build_services(load_config(repo / "config.toml"))
    await services.start()
    try:
        for _ in range(120):
            (demo,) = services.mcp.status()
            if demo.connected or demo.error:
                break
            await asyncio.sleep(0.5)
        if not (demo.connected and demo.sandboxed):
            raise RuntimeError(f"the MCP server: connected {demo.connected}, sandboxed {demo.sandboxed}, {demo.error}")
        services.privacy.set_terms(["Probe Word"])
        url = f"http://127.0.0.1:{services.dashboard.port}/health"
        health = await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=5).read())
        if health != b"ok":
            raise RuntimeError(f"the dashboard said {health!r}")
        if "DASHBOARD_TOKEN=" not in (repo / ".env").read_text():
            raise RuntimeError("the dashboard's password isn't in .env")
    finally:
        await services.close()


try:
    asyncio.run(athena())
    results["run Athena"] = "yes"
except Exception as exc:
    results["run Athena"] = f"{type(exc).__name__}: {exc}"
(data / "probe-results.json").write_text(json.dumps(results, indent=2))
