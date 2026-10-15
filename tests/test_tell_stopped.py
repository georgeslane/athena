"""scripts/tell-stopped.py: telling you in Telegram that Athena has stopped, which systemd runs when it does.

The last test installs the real units and has systemd stop Athena for real, so it runs only in CI, on Linux,
where it can use sudo. The others run the script with a stand-in for Telegram and for journalctl.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOKEN = "123456789:fake-token-for-the-tests-only-xyz"
SECRET = "an-api-key-from-env"
OTHER_TOKEN = "987654321:" + "B" * 35  # shaped like a real one, built here so the file holds none


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        telegram = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                telegram.sent.append((self.path, body))
                raw = b'{"ok": true, "result": {}}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._http.server_address[1]}"
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    def close(self):
        self._http.shutdown()
        self._http.server_close()


@pytest.fixture
def telegram():
    server = FakeTelegram()
    yield server
    server.close()


def athena_repo(root: Path, config: str) -> Path:
    repo = root / "athena"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "tell-stopped.py", repo / "scripts")
    (repo / "config.toml").write_text(config)
    (repo / ".env").write_text(f'TELEGRAM_BOT_TOKEN={TOKEN}\nLLM_API_KEY="{SECRET}"\n# a comment\n')
    return repo


CONFIG = '[telegram]\nbot_token = "${TELEGRAM_BOT_TOKEN}"\nallowed_user_ids = [42, 43]\n'
JOURNAL = f"""\
Started pi-assistant.service - pi-assistant (Telegram AI assistant).
Traceback (most recent call last):
  File "app.py", line 1, in <module>
pi_assistant.memory.MemoryStoreError: the memory index was built with another model (key {SECRET})
token {TOKEN} and another {OTHER_TOKEN}
pi-assistant.service: Main process exited, code=exited, status=1/FAILURE
pi-assistant.service: Failed with result 'exit-code'.
"""


# systemctl, saying each state in turn, then the last one again: one per line, its properties split by "|".
SYSTEMCTL = """#!/bin/sh
[ -s "$STATES" ] || exit 1
head -n 1 "$STATES" | tr '|' '\\n'
if [ "$(wc -l < "$STATES")" -gt 1 ]; then tail -n +2 "$STATES" > "$STATES.next" && mv "$STATES.next" "$STATES"; fi
"""
GAVE_UP = "ActiveState=failed|SubState=failed|Result=start-limit-hit"
FAILED = "ActiveState=failed|SubState=failed|Result=exit-code"
STOPPED = "ActiveState=inactive|SubState=dead|Result=success"
RESTARTING = "ActiveState=activating|SubState=auto-restart|Result=exit-code"
RUNNING = "ActiveState=active|SubState=running|Result=success"


def run(repo: Path, tmp_path: Path, telegram: FakeTelegram, journal: str = JOURNAL, states=(GAVE_UP,), **env):
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "journalctl").write_text(f"#!/bin/sh\ncat <<'EOF'\n{journal}EOF\n")
    (stubs / "systemctl").write_text(SYSTEMCTL)
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    (tmp_path / "states").write_text("".join(f"{state}\n" for state in states))
    return subprocess.run(
        [sys.executable, "-I", str(repo / "scripts" / "tell-stopped.py")],
        env={
            "PATH": f"{stubs}:/usr/bin:/bin",
            "TELEGRAM_API": telegram.url,
            "STATES": str(tmp_path / "states"),
            "POLL_SECONDS": "0.01",
            "STEADY_SECONDS": "0.1",
            "HELD_SECONDS": "0.1",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_it_tells_you_why_with_the_end_of_the_log_and_no_secrets(tmp_path, telegram):
    repo = athena_repo(tmp_path, CONFIG)
    # Started at the first failure, it waits while systemd restarts Athena, until systemd gives up. On newer
    # systemd, Athena shows as failed for a moment before each restart: that doesn't count.
    result = run(repo, tmp_path, telegram, states=(FAILED, RESTARTING, FAILED, RESTARTING, GAVE_UP))
    assert result.returncode == 0, result.stderr
    ((path, body),) = telegram.sent
    assert path == f"/bot{TOKEN}/sendMessage" and body["chat_id"] == "42"
    assert body["text"] == (
        "🛑 Athena has stopped, and systemd has stopped restarting it.\n\n"
        "The end of its log:\n"
        "Traceback (most recent call last):\n"
        '  File "app.py", line 1, in <module>\n'
        "pi_assistant.memory.MemoryStoreError: the memory index was built with another model (key [hidden])\n"
        "token [hidden] and another [hidden]\n\n"
        "Once it's fixed, start it again on the Pi with:\nsudo systemctl restart pi-assistant"
    )


def test_a_blip_it_recovers_from_says_nothing(tmp_path, telegram):
    repo = athena_repo(tmp_path, CONFIG)
    result = run(repo, tmp_path, telegram, states=(RESTARTING, RUNNING))
    assert result.returncode == 0 and "started again by itself" in result.stdout and telegram.sent == []


def test_stopped_without_an_error_says_nothing(tmp_path, telegram):
    repo = athena_repo(tmp_path, CONFIG)
    result = run(repo, tmp_path, telegram, states=(RESTARTING, STOPPED))
    assert result.returncode == 0 and "stopped without an error" in result.stdout and telegram.sent == []


def test_failed_for_good_without_hitting_the_limit(tmp_path, telegram):
    repo = athena_repo(tmp_path, CONFIG)
    result = run(repo, tmp_path, telegram, states=(FAILED,), MONITOR_SERVICE_RESULT="exit-code")
    assert result.returncode == 0, result.stderr
    expected = "🛑 Athena has stopped, and systemd has stopped restarting it. The last time, it stopped with an error."
    assert telegram.sent[0][1]["text"].startswith(expected)


def test_stopping_and_restarting_for_a_long_time_is_worth_saying(tmp_path, telegram):
    repo = athena_repo(tmp_path, CONFIG)
    # Running for a moment at a time, then failing again, until it's watched for long enough.
    states = (RUNNING, RESTARTING) * 3
    result = run(repo, tmp_path, telegram, states=states, STEADY_SECONDS="60", SETTLE_SECONDS="0.3")
    assert result.returncode == 0, result.stderr
    ((_, body),) = telegram.sent
    assert body["text"].startswith("⚠️ Athena keeps stopping and restarting.\n\nThe end of its log:\n")
    assert "sudo systemctl restart" not in body["text"]


def test_a_broken_config_still_says_where_to(tmp_path, telegram):
    repo = athena_repo(tmp_path, "[telegram\nallowed_user_ids = [ 42 ]\n")  # not TOML, as might have stopped it
    # And systemctl can't be asked: it goes by what systemd said when it started this.
    result = run(repo, tmp_path, telegram, journal="", states=(), MONITOR_SERVICE_RESULT="exit-code")
    assert result.returncode == 0, result.stderr
    ((_, body),) = telegram.sent
    assert body["chat_id"] == "42"
    assert body["text"].startswith("🛑 Athena has stopped, and systemd has stopped restarting it. The last time, it")


def test_without_a_chat_it_says_so(tmp_path, telegram):
    repo = athena_repo(tmp_path, "[telegram]\nallowed_user_ids = []\n")
    result = run(repo, tmp_path, telegram)
    assert result.returncode != 0 and "no telegram.allowed_user_ids" in result.stderr and telegram.sent == []


def test_when_telegram_cant_be_reached_it_says_so_without_the_token(tmp_path, telegram):
    repo = athena_repo(tmp_path, CONFIG)
    result = run(repo, tmp_path, telegram, TELEGRAM_API="http://127.0.0.1:9")
    assert result.returncode == 1 and "Couldn't tell you" in result.stderr and TOKEN not in result.stderr


def _can_use_systemd() -> bool:
    if not (os.environ.get("CI") and sys.platform == "linux" and Path("/run/systemd/system").is_dir()):
        return False
    return subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0


def sudo(*args):
    return subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=60)


@pytest.fixture
def systemd_athena(tmp_path, telegram):
    """Install the real units, for an Athena that runs ``athena`` (a shell script), and start it."""
    units = Path("/etc/systemd/system")
    dropins = {
        "pi-assistant.service.d": "[Service]\nRestartSec=1\n",  # so it gives up in seconds, not a minute
        "pi-assistant-stopped.service.d": f"[Service]\nEnvironment=TELEGRAM_API={telegram.url} STEADY_SECONDS=3\n",
    }

    def start(athena: str) -> Path:
        repo = athena_repo(tmp_path, CONFIG)
        shutil.copy(REPO / "scripts" / "install-units.sh", repo / "scripts")
        shutil.copytree(REPO / "deploy", repo / "deploy")
        program = repo / ".venv" / "bin" / "pi-assistant"
        program.parent.mkdir(parents=True)
        program.write_text(athena)
        program.chmod(0o755)
        installed = subprocess.run(["bash", str(repo / "scripts" / "install-units.sh")], capture_output=True, text=True)
        assert installed.returncode == 0, installed.stderr
        for folder, text in dropins.items():
            sudo("mkdir", "-p", str(units / folder))
            subprocess.run(["sudo", "-n", "tee", str(units / folder / "test.conf")], input=text, text=True, check=True)
        sudo("systemctl", "daemon-reload")
        sudo("systemctl", "start", "pi-assistant")
        return repo

    try:
        yield start
    finally:
        sudo("systemctl", "stop", "pi-assistant", "pi-assistant-stopped")
        for name in ("pi-assistant.service", "pi-assistant-stopped.service", *dropins):
            sudo("rm", "-rf", str(units / name))
        sudo("systemctl", "daemon-reload")
        sudo("systemctl", "reset-failed")


def watched(since: float, timeout=90) -> str:
    """Wait for pi-assistant-stopped to have run, since ``since``, and finished; returns its log since then."""
    log = ["journalctl", "--no-pager", "-o", "cat", "-u", "pi-assistant-stopped", f"--since=@{int(since)}"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ran = "pi-assistant-stopped" in sudo(*log).stdout
        if ran and sudo("systemctl", "is-active", "pi-assistant-stopped").stdout.strip() in ("inactive", "failed"):
            break
        time.sleep(0.5)
    return sudo(*log).stdout


@pytest.mark.skipif(not _can_use_systemd(), reason="needs systemd and sudo: runs in CI")
def test_systemd_tells_you_when_athena_stops_for_good(systemd_athena, telegram):
    since = time.time() - 1
    systemd_athena(f"#!/bin/sh\necho 'ConfigError: something is wrong (token {TOKEN})' >&2\nexit 1\n")
    log = watched(since)
    state = sudo("systemctl", "show", "-p", "ActiveState,Result", "pi-assistant").stdout
    assert telegram.sent, f"nothing sent; pi-assistant: {state}\npi-assistant-stopped's log:\n{log}"
    ((path, body),) = telegram.sent
    assert path == f"/bot{TOKEN}/sendMessage" and body["chat_id"] == "42"
    assert body["text"].startswith("🛑 Athena has stopped, and systemd has stopped restarting it.")
    assert "ConfigError: something is wrong (token [hidden])" in body["text"]
    assert "ActiveState=failed" in state  # given up: it's not restarting


@pytest.mark.skipif(not _can_use_systemd(), reason="needs systemd and sudo: runs in CI")
def test_systemd_says_nothing_when_athena_starts_again_by_itself(systemd_athena, telegram, tmp_path):
    marker = tmp_path / "failed-once"
    since = time.time() - 1
    systemd_athena(f"#!/bin/sh\n[ -e {marker} ] && exec sleep 600\ntouch {marker}\necho 'A blip' >&2\nexit 1\n")
    log = watched(since)
    assert "started again by itself" in log, log
    assert telegram.sent == []
    assert sudo("systemctl", "is-active", "pi-assistant").stdout.strip() == "active"
