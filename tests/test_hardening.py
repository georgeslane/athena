"""deploy/pi-assistant.service's hardening, under systemd: what Athena can still do, and what it can't.

It installs the real unit with scripts/install-units.sh, for an Athena that's tests/fixtures/hardening_probe.py,
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
    "add a file to the repo",
    "change a script",
    "change its environment",
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
    # as a drive's might, and is outside the repo.
    root = Path.home() / f".athena-hardening-{os.getpid()}"
    repo, data = root / "athena", root / "athena data"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "install-units.sh", repo / "scripts")
    shutil.copy(REPO / "scripts" / "update.sh", repo / "scripts")
    shutil.copytree(REPO / "deploy", repo / "deploy")
    (repo / "config.toml").write_text(CONFIG)
    (repo / ".env").write_text("LLM_API_KEY=a-key\n")
    program = repo / ".venv" / "bin" / "pi-assistant"
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
    }
    (repo / "probe.json").write_text(json.dumps(plan))
    installed = subprocess.run(["bash", str(repo / "scripts" / "install-units.sh")], capture_output=True, text=True)
    try:
        assert installed.returncode == 0, installed.stderr
        since = time.time() - 1
        sudo("systemctl", "start", "pi-assistant")
        deadline = time.monotonic() + 180
        results = data / "probe-results.json"
        while not results.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        log = sudo("journalctl", "--no-pager", "-o", "cat", "-u", "pi-assistant", f"--since=@{int(since)}").stdout
        assert results.exists(), f"the probe didn't finish:\n{log}"
        yield json.loads(results.read_text()), repo
    finally:
        sudo("systemctl", "stop", "pi-assistant")
        sudo("rm", "-f", "/etc/systemd/system/pi-assistant.service", "/etc/systemd/system/pi-assistant-stopped.service")
        sudo("systemctl", "daemon-reload")
        sudo("systemctl", "reset-failed")
        shutil.rmtree(root, ignore_errors=True)


def test_athena_can_do_its_job_under_its_unit_and_nothing_more(probed):
    results, repo = probed
    print(json.dumps(results, indent=2))  # what happened, for when it fails
    assert results["(mounted over /proc)"] == "nothing", "the sandbox can't mount /proc"
    assert {name: results[name] for name in MUST} == dict.fromkeys(MUST, "yes")
    assert [name for name in MUST_NOT if results[name] == "yes"] == []
    assert "SET_BY_PROBE=yes" in (repo / ".env").read_text()


def test_the_units_exposure_stays_low(probed):
    shown = sudo("systemd-analyze", "security", "--no-pager", "pi-assistant.service").stdout
    print(shown)
    score = float(re.search(r"Overall exposure level for pi-assistant\.service: ([\d.]+)", shown).group(1))
    assert score <= MAX_EXPOSURE
