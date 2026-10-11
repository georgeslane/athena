"""Runs install.sh and update.sh with stand-ins for sudo, apt-get, uv, systemctl and friends,
which record what they were asked to do instead of changing the machine."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or os.geteuid() == 0, reason="needs bash, and install.sh refuses to run as root"
)

RECORD = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\n'
STUBS = {
    "apt-get": RECORD,
    # Ollama's installer, piped to sh, "updates" Ollama: it stops being too old.
    "curl": RECORD + 'case "$*" in *ollama.com/install.sh*) echo "rm -f \'$STUB_FILES/ollama-too-old\'";; esac\n',
    # While $STUB_FILES/ollama-too-old exists, pulls fail.
    "ollama": RECORD
    + 'if [ "$1" = pull ] && [ -e "$STUB_FILES/ollama-too-old" ]; then\n'
    + '  echo "Error: pull model manifest: 412: this model requires a newer version of Ollama"; exit 1\nfi\n'
    + 'if [ "$1" = pull ] && [ "$2" = missing ]; then echo "Error: file does not exist"; exit 1; fi\n',
    "uv": RECORD + 'exit "${UV_EXIT:-0}"\n',
    "uvx": RECORD,
    "raspi-config": RECORD,
    "getent": RECORD,
    # sudo records the command; for `sudo tee FILE` it also keeps what would have been written.
    "sudo": RECORD + 'if [ "$1" = tee ]; then cat > "$STUB_FILES/$(basename "$2")"; fi\n',
    # `systemctl cat` finds the status board's unit only if DISPLAY_INSTALLED is set.
    "systemctl": RECORD + 'if [ "$1" = cat ]; then [ -n "$DISPLAY_INSTALLED" ]; fi\n',
}


@pytest.fixture
def sandbox(tmp_path):
    repo = tmp_path / "pi-assistant"
    for item in ["scripts", "deploy", "config.example.toml", ".env.example"]:
        (shutil.copytree if (REPO / item).is_dir() else shutil.copy)(REPO / item, repo / item)
    stubs, git_stub, files, home = (tmp_path / d for d in ("stubs", "git-stub", "written", "home"))
    for d in (stubs, git_stub, files, home):
        d.mkdir()
    for path, script in [*((stubs / name, script) for name, script in STUBS.items()), (git_stub / "git", RECORD)]:
        path.write_text(script)
        path.chmod(0o755)
    log = tmp_path / "log"
    log.touch()
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "HOME": str(home),
        "USER": "pi",
        "STUB_LOG": str(log),
        "STUB_FILES": str(files),
        "GIT_CEILING_DIRECTORIES": str(tmp_path),
    }

    def run(script, *args, display_installed=False, fake_git=False, answer="", **extra_env):
        path = f"{git_stub}:{env['PATH']}" if fake_git else env["PATH"]  # install.sh needs the real git
        result = subprocess.run(
            ["bash", f"scripts/{script}", *args],
            cwd=repo,
            env={**env, "PATH": path, "DISPLAY_INSTALLED": "1" if display_installed else "", **extra_env},
            input=answer,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result, log.read_text().splitlines()

    return repo, files, run


def test_install(sandbox):
    repo, files, run = sandbox
    result, calls = run("install.sh")
    assert result.returncode == 0, result.stderr

    assert "sudo apt-get install -y -qq git curl ca-certificates sqlite3" in calls
    assert "uv sync --no-dev" in calls
    assert not [c for c in calls if "display" in c]  # the status board is its own project now
    unit = (files / "pi-assistant.service").read_text()
    assert f"ExecStart={repo}/.venv/bin/pi-assistant run" in unit
    assert "User=pi" in unit
    assert not re.search(r"^[^#].*@[A-Z_]+@", unit, re.MULTILINE)  # every placeholder filled in
    assert (repo / "config.toml").read_text() == (repo / "config.example.toml").read_text()
    assert (repo / ".env").stat().st_mode & 0o777 == 0o600


def test_install_says_where_the_status_board_went(sandbox):
    _, _, run = sandbox
    result, calls = run("install.sh", "--display")
    assert result.returncode == 2
    assert "pi-display-microservice" in result.stderr
    assert calls == []


def test_install_rejects_unknown_options(sandbox):
    _, _, run = sandbox
    result, calls = run("install.sh", "--dispaly")
    assert result.returncode == 2
    assert "Unknown option: --dispaly" in result.stderr
    assert calls == []


def test_update(sandbox):
    _, _, run = sandbox
    result, calls = run("update.sh", fake_git=True)
    assert result.returncode == 0, result.stderr
    assert calls == [
        "git pull --ff-only",
        "uv sync --no-dev",
        "sudo systemctl restart pi-assistant",
        "systemctl cat pi-assistant-display.service",
    ]


def test_update_retires_the_old_status_board(sandbox):
    _, _, run = sandbox
    result, calls = run("update.sh", display_installed=True, fake_git=True)
    assert result.returncode == 0, result.stderr
    assert calls[-3:] == [
        "sudo systemctl disable --now pi-assistant-display",
        "sudo rm -f /etc/systemd/system/pi-assistant-display.service",
        "sudo systemctl daemon-reload",
    ]
    assert "pi-display-microservice" in result.stdout


# -- switch-embeddings.sh --------------------------------------------------------------------------------


def test_switching_the_embeddings_model(sandbox):
    _, _, run = sandbox
    result, calls = run("switch-embeddings.sh", "new-model")
    assert result.returncode == 0, result.stderr
    assert calls == [
        "ollama pull new-model",
        "sudo systemctl stop pi-assistant",
        "uv run pi-assistant embeddings use new-model",
        "sudo systemctl restart ollama",  # lets go of the old model
        "sudo systemctl start pi-assistant",
    ]


def test_athena_starts_again_if_switching_fails(sandbox):
    _, _, run = sandbox
    result, calls = run("switch-embeddings.sh", "new-model", UV_EXIT="1")
    assert result.returncode == 1
    assert calls[-2:] == ["uv run pi-assistant embeddings use new-model", "sudo systemctl start pi-assistant"]
    assert "sudo systemctl restart ollama" not in calls


def test_a_model_that_wont_download_changes_nothing(sandbox):
    _, _, run = sandbox
    result, calls = run("switch-embeddings.sh", "missing")
    assert result.returncode == 1 and "nothing was changed" in result.stderr
    assert calls == ["ollama pull missing"]  # Athena wasn't stopped


@pytest.mark.parametrize("answer", ["y\n", "n\n", ""])
def test_an_ollama_too_old_for_the_model_is_updated_if_you_say_so(sandbox, answer):
    _, files, run = sandbox
    (files / "ollama-too-old").touch()
    result, calls = run("switch-embeddings.sh", "new-model", answer=answer)
    if answer == "y\n":
        assert result.returncode == 0, result.stderr
        assert calls[:3] == [
            "ollama pull new-model",
            "curl -fsSL https://ollama.com/install.sh",
            "curl -sf http://127.0.0.1:11434/api/version",
        ]
        assert calls[3:5] == ["ollama pull new-model", "sudo systemctl stop pi-assistant"]
    else:
        assert result.returncode == 1 and "Nothing was changed" in result.stderr
        assert calls == ["ollama pull new-model"]


def test_switching_needs_one_model(sandbox):
    _, _, run = sandbox
    for args in [(), ("a", "b")]:
        result, calls = run("switch-embeddings.sh", *args)
        assert result.returncode == 2 and "Usage" in result.stderr and calls == []
