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


def run(repo: Path, tmp_path: Path, telegram: FakeTelegram, journal: str = JOURNAL, **env):
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "journalctl").write_text(f"#!/bin/sh\ncat <<'EOF'\n{journal}EOF\n")
    (stubs / "journalctl").chmod(0o755)
    return subprocess.run(
        [sys.executable, "-I", str(repo / "scripts" / "tell-stopped.py")],
        env={"PATH": f"{stubs}:/usr/bin:/bin", "TELEGRAM_API": telegram.url, **env},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_it_tells_you_why_with_the_end_of_the_log_and_no_secrets(tmp_path, telegram):
    repo = athena_repo(tmp_path, CONFIG)
    result = run(repo, tmp_path, telegram, MONITOR_SERVICE_RESULT="start-limit-hit")
    assert result.returncode == 0, result.stderr
    ((path, body),) = telegram.sent
    assert path == f"/bot{TOKEN}/sendMessage" and body["chat_id"] == "42"
    assert body["text"] == (
        "🛑 Athena has stopped: it kept failing to start, so systemd stopped trying.\n\n"
        "The end of its log:\n"
        "Traceback (most recent call last):\n"
        '  File "app.py", line 1, in <module>\n'
        "pi_assistant.memory.MemoryStoreError: the memory index was built with another model (key [hidden])\n"
        "token [hidden] and another [hidden]\n\n"
        "Once it's fixed, start it again on the Pi with:\nsudo systemctl restart pi-assistant"
    )


def test_a_broken_config_still_says_where_to(tmp_path, telegram):
    repo = athena_repo(tmp_path, "[telegram\nallowed_user_ids = [ 42 ]\n")  # not TOML, as might have stopped it
    result = run(repo, tmp_path, telegram, journal="", MONITOR_SERVICE_RESULT="exit-code")
    assert result.returncode == 0, result.stderr
    ((_, body),) = telegram.sent
    assert body["chat_id"] == "42"
    assert body["text"].startswith("🛑 Athena has stopped: it stopped with an error.\n\nOnce it's fixed")


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


@pytest.mark.skipif(not _can_use_systemd(), reason="needs systemd and sudo: runs in CI")
def test_systemd_tells_you_when_athena_stops_for_good(tmp_path, telegram):
    """Install the real units, with an Athena that can't start, and wait for systemd to give up and tell you."""
    repo = athena_repo(tmp_path, CONFIG)
    for script in ("install-units.sh",):
        shutil.copy(REPO / "scripts" / script, repo / "scripts")
    shutil.copytree(REPO / "deploy", repo / "deploy")
    athena = repo / ".venv" / "bin" / "pi-assistant"
    athena.parent.mkdir(parents=True)
    athena.write_text(f"#!/bin/sh\necho 'ConfigError: something is wrong (token {TOKEN})' >&2\nexit 1\n")
    athena.chmod(0o755)
    units = Path("/etc/systemd/system")
    dropins = {
        "pi-assistant.service.d": "[Service]\nRestartSec=1\n",  # so it gives up in seconds, not a minute
        "pi-assistant-stopped.service.d": f"[Service]\nEnvironment=TELEGRAM_API={telegram.url}\n",
    }

    def sudo(*args):
        return subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=60)

    try:
        installed = subprocess.run(["bash", str(repo / "scripts" / "install-units.sh")], capture_output=True, text=True)
        assert installed.returncode == 0, installed.stderr
        for folder, text in dropins.items():
            sudo("mkdir", "-p", str(units / folder))
            subprocess.run(["sudo", "-n", "tee", str(units / folder / "test.conf")], input=text, text=True, check=True)
        sudo("systemctl", "daemon-reload")
        sudo("systemctl", "start", "pi-assistant")
        deadline = time.monotonic() + 90
        while not telegram.sent and time.monotonic() < deadline:
            time.sleep(0.5)
        state = sudo("systemctl", "show", "-p", "ActiveState,Result,NRestarts", "pi-assistant").stdout
        stopped_log = sudo("journalctl", "--no-pager", "-u", "pi-assistant-stopped", "-n", "20").stdout
        assert telegram.sent, f"nothing sent; pi-assistant: {state}\npi-assistant-stopped's log:\n{stopped_log}"
        ((path, body),) = telegram.sent
        assert path == f"/bot{TOKEN}/sendMessage" and body["chat_id"] == "42"
        print(body["text"], state, sep="\n")
        assert body["text"].startswith("🛑 Athena has stopped: it kept failing to start, so systemd stopped trying.")
        assert "ConfigError: something is wrong (token [hidden])" in body["text"]
        assert "ActiveState=failed" in state and "Result=start-limit-hit" in state
    finally:
        sudo("systemctl", "stop", "pi-assistant", "pi-assistant-stopped")
        for name in ("pi-assistant.service", "pi-assistant-stopped.service", *dropins):
            sudo("rm", "-rf", str(units / name))
        sudo("systemctl", "daemon-reload")
        sudo("systemctl", "reset-failed")
