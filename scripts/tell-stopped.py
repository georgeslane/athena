"""Tell you in Telegram that Athena has stopped, which it can't do itself.

systemd runs this (deploy/pi-assistant-stopped.service) as soon as pi-assistant.service fails, even
when it's going to restart it. So first it watches what happens next:
  - systemd gives up, after five failed starts in ten minutes: it tells you, and why.
  - Athena runs steadily again, for STEADY_SECONDS: it was a blip, so it says nothing.
  - Neither, after SETTLE_SECONDS: Athena keeps stopping and restarting, and it tells you that.
While this runs, systemd doesn't start it again for the failures that follow.

It uses only Python's own library, with the system's Python, so it works even when Athena's
environment is what's broken. It finds the bot's token and your chat as Athena does, in config.toml
and .env.

It sends why systemd says Athena stopped, and the end of Athena's log, with every value in .env,
and anything else that looks like a Telegram token, taken out.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")  # another address, for the tests
LOG_LINES, LINE_CHARS = 12, 300
# How long it watches (set in the environment for the tests): running steadily this long again was a blip,
STEADY_SECONDS = float(os.environ.get("STEADY_SECONDS", 60))
SETTLE_SECONDS = float(os.environ.get("SETTLE_SECONDS", 900))  # and still restarting after this long isn't.
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", 2))
TOKEN_LIKE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# systemd's own lines in the log, which say nothing the message doesn't.
SYSTEMD_SAYS = re.compile(r"^(\S+\.service: |(Started|Stopped|Starting|Stopping|Failed to start) )")
WHY = {
    "start-limit-hit": "it kept failing to start, so systemd stopped trying",
    "exit-code": "it stopped with an error",
    "signal": "it was killed",
    "core-dump": "it crashed",
    "oom-kill": "it ran out of memory",
    "timeout": "it took too long to start or stop",
    "watchdog": "it stopped responding",
}


def read_env(path: Path) -> dict[str, str]:
    """NAME=value lines from a .env file, as systemd and Athena read them."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def where_to(repo: Path, env: dict[str, str]) -> tuple[str, str]:
    """The bot's token and your chat. Raises SystemExit, saying why, if either can't be found."""
    text = ""
    try:
        text = (repo / "config.toml").read_text()
        telegram = tomllib.loads(text).get("telegram", {})
    except (OSError, tomllib.TOMLDecodeError):  # a broken config.toml may be why Athena stopped
        telegram = {}
        ids = re.search(r"^\s*allowed_user_ids\s*=\s*\[\s*(\d+)", text, re.MULTILINE)
        telegram["allowed_user_ids"] = [int(ids.group(1))] if ids else []
    token = ENV_REF.sub(lambda m: env.get(m.group(1), ""), str(telegram.get("bot_token") or ""))
    token = token or env.get("TELEGRAM_BOT_TOKEN", "")
    users = telegram.get("allowed_user_ids") or []
    if not token or not users:
        raise SystemExit("Can't tell you Athena has stopped: there's no bot token, or no telegram.allowed_user_ids")
    return token, str(users[0])


def log_lines(unit: str) -> list[str]:
    """The end of Athena's log, without systemd's own lines. Empty if it can't be read."""
    try:
        out = subprocess.run(
            ["journalctl", "--no-pager", "--quiet", "-o", "cat", "-n", str(LOG_LINES * 3), "-u", unit],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    lines = [line.rstrip() for line in out.splitlines() if line.strip() and not SYSTEMD_SAYS.match(line)]
    return [line if len(line) <= LINE_CHARS else line[: LINE_CHARS - 1] + "…" for line in lines[-LOG_LINES:]]


def unit_state(unit: str) -> dict[str, str]:
    """systemd's ActiveState, SubState and Result for the unit. Empty if it can't be asked."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "ActiveState,SubState,Result", unit], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def settle(unit: str) -> tuple[str, str]:
    """Wait to see what becomes of the unit: ("failed", why), ("running", ""), or ("restarting", "")."""
    deadline, running_since = time.monotonic() + SETTLE_SECONDS, None
    while time.monotonic() < deadline:
        state = unit_state(unit)
        if not state:  # systemctl can't be asked: say what systemd said when it started this
            return "failed", os.environ.get("MONITOR_SERVICE_RESULT", "")
        if state.get("ActiveState") == "failed":
            return "failed", state.get("Result", "")
        if (state.get("ActiveState"), state.get("SubState")) == ("active", "running"):
            running_since = running_since or time.monotonic()
            if time.monotonic() - running_since >= STEADY_SECONDS:
                return "running", ""
        else:
            running_since = None
        time.sleep(POLL_SECONDS)
    return "restarting", ""


def hide(text: str, secrets: list[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[hidden]")
    return TOKEN_LIKE.sub("[hidden]", text)


def message(became: str, result: str, lines: list[str]) -> str:
    if became == "restarting":
        text = "⚠️ Athena keeps stopping and restarting."
    else:
        text = f"🛑 Athena has stopped: {WHY.get(result, 'it stopped')}."
    if lines:
        text += "\n\nThe end of its log:\n" + "\n".join(lines)
    if became == "restarting":
        return text
    return text + "\n\nOnce it's fixed, start it again on the Pi with:\nsudo systemctl restart pi-assistant"


def send(token: str, chat: str, text: str) -> None:
    request = urllib.request.Request(
        f"{API}/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": text[:4000], "link_preview_options": {"is_disabled": True}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def main() -> int:
    env = {**read_env(REPO / ".env"), **os.environ}
    token, chat = where_to(REPO, env)
    unit = os.environ.get("MONITOR_UNIT", "pi-assistant.service")
    became, result = settle(unit)
    if became == "running":
        print("Athena failed, then started again by itself: nothing to tell you")
        return 0
    secrets = [v for v in read_env(REPO / ".env").values() if len(v) >= 6] + [token]
    text = hide(message(became, result, log_lines(unit)), secrets)
    try:
        send(token, chat, text)
    except OSError as exc:
        print(f"Couldn't tell you Athena has stopped: {hide(str(exc), secrets)}", file=sys.stderr)
        return 1
    print("Told you Athena has stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
