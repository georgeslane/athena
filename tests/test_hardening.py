"""Athena's unit's hardening (resources/systemd/athena.service), under systemd: what Athena can still do, and
what it can't.

It installs the real unit with `athena service install`, for an Athena that's tests/fixtures/hardening_probe.py,
which tries each thing and says what happened. It needs systemd and sudo, so it runs only in CI, on Linux.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from athena import service

REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "tests" / "fixtures" / "hardening_probe.py"
MAX_EXPOSURE = 5.0  # systemd-analyze security's score for the unit, from 0 (locked down) to 10

DEMO = REPO / "tests" / "fixtures" / "demo_mcp_server.py"
CONFIG = f"""\
data_dir = "../athena data"

[llm]
base_url = "http://127.0.0.1:9/v1"
model = "test-model"
warm_up_minutes = 0

[display]
enabled = false

[dashboard]
port = 0

[mcp_servers.demo]
command = "{sys.executable}"
args = ["{DEMO}"]
confirm = []
network = false
read_only_paths = ["{DEMO.parent}"]

[markets]
enabled = true
"""

MUST = [
    "run Athena",
    "use openat2",
    "write its data",
    "change config.toml",
    "change .env",
    "write uv's cache",
    "make an environment with uv",
    "write /tmp",
    "connect to the network",
    "start a sandboxed server",
    "connect out from a sandbox with the network",
]
MUST_NOT = [
    "change its code",
    "add a file to its environment",
    "write to your home folder",
    "change ~/.bashrc",
    "see the system's /tmp",
    "use sudo",  # which works for this user outside the unit: CI's runner has sudo without a password
    "see your home folder from the sandbox",
    "connect out from a sandbox without it",
]


def _can_use_systemd() -> bool:
    if not (os.environ.get("CI") and sys.platform == "linux" and Path("/run/systemd/system").is_dir()):
        return False
    return subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _can_use_systemd(), reason="needs systemd and sudo: runs in CI")


def sudo(*args):
    return subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=60)


@pytest.fixture(scope="module")
def listener():
    """Something on the network to connect to."""
    server = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def serve():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                server.accept()[0].close()
            except OSError:
                pass

    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()[1]
    stop.set()
    server.close()


@pytest.fixture(scope="module")
def probed(listener, tmp_path_factory):
    # Not in /tmp, which the unit gives Athena a private one of. The data folder has a space in its name,
    # as a drive's might, and is outside Athena's home folder.
    root = Path.home() / f".athena-hardening-{os.getpid()}"
    athena_home, data = root / ".athena", root / "athena data"
    athena_home.mkdir(parents=True)
    (athena_home / "config.toml").write_text(CONFIG)
    (athena_home / ".env").write_text("LLM_API_KEY=a-key\n")
    # Where `uv tool install` puts Athena: the unit keeps it read-only, though the rest of uv's folder isn't.
    code = Path.home() / ".local" / "share" / "uv" / "tools" / f"athena-hardening-{os.getpid()}"
    program = code / "bin" / "athena"
    program.parent.mkdir(parents=True)
    program.write_text(f"#!/bin/sh\nexec {sys.executable} {PROBE}\n")
    program.chmod(0o755)
    home_canary = root / "canary"  # in your home folder, which a sandboxed server mustn't see
    home_canary.write_text("secret\n")
    tmp_canary = tmp_path_factory.mktemp("hardening") / "canary"  # in the system's /tmp
    tmp_canary.write_text("secret\n")
    plan = {
        "data": str(data),
        "port": listener,
        "uv": shutil.which("uv"),
        "home_canary": str(home_canary),
        "tmp_canary": str(tmp_canary),
        "code": str(code),
    }
    (athena_home / "probe.json").write_text(json.dumps(plan))
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ATHENA_HOME", str(athena_home))
            mp.delenv("ATHENA_CONFIG", raising=False)
            mp.setattr(service, "program", lambda: program)
            service.install()
        since = time.time() - 1
        sudo("systemctl", "start", "athena")
        deadline = time.monotonic() + 180
        results = data / "probe-results.json"
        while not results.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        log = sudo("journalctl", "--no-pager", "-o", "cat", "-u", "athena", f"--since=@{int(since)}").stdout
        assert results.exists(), f"the probe didn't finish:\n{log}"
        yield json.loads(results.read_text()), athena_home
    finally:
        sudo("systemctl", "stop", "athena")
        sudo("rm", "-f", "/etc/systemd/system/athena.service", "/etc/systemd/system/athena-stopped.service")
        sudo("systemctl", "daemon-reload")
        sudo("systemctl", "reset-failed")
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(code, ignore_errors=True)


def test_athena_can_do_its_job_under_its_unit_and_nothing_more(probed):
    results, athena_home = probed
    print(json.dumps(results, indent=2))  # what happened, for when it fails
    assert results["(mounted over /proc)"] == "nothing", "the sandbox can't mount /proc"
    assert {name: results[name] for name in MUST} == dict.fromkeys(MUST, "yes")
    assert [name for name in MUST_NOT if results[name] == "yes"] == []
    assert "SET_BY_PROBE=yes" in (athena_home / ".env").read_text()


def test_the_units_exposure_stays_low(probed):
    shown = sudo("systemd-analyze", "security", "--no-pager", "athena.service").stdout
    print(shown)
    score = float(re.search(r"Overall exposure level for athena\.service: ([\d.]+)", shown).group(1))
    assert score <= MAX_EXPOSURE
