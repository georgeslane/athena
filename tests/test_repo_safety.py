"""The first ground rule in CLAUDE.md: secrets and personal data never reach GitHub, from here or the Pi."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs git and a git checkout"
)

# Run git without the user's own config, so a global gitignore or hook can't make these pass.
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

# Where a real install keeps secrets and personal data.
PRIVATE_PATHS = [
    ".env",
    ".env.local",
    "config.toml",
    "data/assistant.db",
    "data/status.json",  # what you last asked the assistant, for the status board
    "data/logs/mcp-fetch.log",
    "prompts/system.local.md",
    "other-data-dir/assistant.db",
    "other-data-dir/assistant.db-wal",
]

SECRET_PATTERNS = {
    "Telegram bot token": r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b",
    "API key (sk-...)": r"\bsk-[A-Za-z0-9_-]{20,}",
    "GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{36,}",
    "Google API key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}


def git(*args: str, cwd: Path = REPO, check: bool = True) -> subprocess.CompletedProcess[str]:
    # core.excludesFile would otherwise default to the user's ~/.config/git/ignore.
    return subprocess.run(
        ["git", "-c", f"core.excludesFile={os.devnull}", *args],
        cwd=cwd,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture(params=["working tree", "fresh clone"])
def checkout(request, tmp_path) -> Path:
    """This checkout, and a clone of its last commit, which is what the Pi gets."""
    if request.param == "working tree":
        return REPO
    clone = tmp_path / "clone"
    git("clone", "--quiet", str(REPO), str(clone))
    return clone


def test_private_files_are_git_ignored(checkout):
    for path in PRIVATE_PATHS:
        result = git("check-ignore", "--quiet", path, cwd=checkout, check=False)
        assert result.returncode == 0, f"{path} isn't git-ignored here. Is .gitignore committed?"


def test_example_files_are_committed_and_not_ignored(checkout):
    for name in ["config.example.toml", ".env.example"]:
        assert (checkout / name).is_file(), f"{name} is missing here, but install.sh copies it. Is it committed?"
        assert git("check-ignore", "--quiet", "--no-index", name, cwd=checkout, check=False).returncode == 1


def test_env_example_holds_no_values():
    for line in (REPO / ".env.example").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            assert not value.strip(), f".env.example has a value for {name}; real values belong in .env"


def test_no_secrets_in_files_git_would_commit():
    # Tracked files plus untracked ones that aren't ignored: everything `git add -A` would pick up.
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout.split("\0")
    found = []
    for name in filter(None, names):
        path = REPO / name
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        found += [f"{name}: {label}" for label, pattern in SECRET_PATTERNS.items() if re.search(pattern, text)]
    assert not found, "Possible secrets that git would commit:\n" + "\n".join(found)


def test_disable_git_push_blocks_pushes_but_not_pulls(tmp_path):
    github = tmp_path / "github.git"
    git("init", "--quiet", "--bare", str(github))
    pi = tmp_path / "pi"
    git("clone", "--quiet", str(github), str(pi))
    (pi / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "disable-git-push.sh", pi / "scripts")
    git("commit", "--quiet", "--allow-empty", "-m", "first", cwd=pi)
    git("push", "--quiet", "origin", "HEAD", cwd=pi)

    for _ in range(2):  # install.sh is safe to re-run, so this must be too
        subprocess.run(["bash", "scripts/disable-git-push.sh"], cwd=pi, env=GIT_ENV, check=True, capture_output=True)

    git("commit", "--quiet", "--allow-empty", "-m", "second", cwd=pi)
    assert git("push", "origin", "HEAD", cwd=pi, check=False).returncode != 0
    assert git("push", str(github), "HEAD", cwd=pi, check=False).returncode != 0  # by URL: the hook refuses
    assert git("fetch", "origin", cwd=pi, check=False).returncode == 0  # so `git pull` still works
    assert git("rev-list", "--all", "--count", cwd=github).stdout.strip() == "1"
