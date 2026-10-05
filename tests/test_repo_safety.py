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
    "config.toml.bak",  # the dashboard's copy of the config before its last change
    "config.toml.tmp",  # and the config while it's being written
    ".env.tmp",
    "data/assistant.db",
    "data/status.json",  # what you last asked the assistant, left by the old built-in status board
    "data/logs/mcp-fetch.log",
    "prompts/system.local.md",
    ".personal-blocklist",  # your personal details, for scripts/check-secrets.sh to look for
    "other-data-dir/assistant.db",
    "other-data-dir/assistant.db-wal",
]

SECRET_PATTERNS = {
    "Telegram bot token": r"(?<![\w-])\d{8,12}:[\w-]{35}(?![\w-])",  # tokens can end in "-"
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


# -- The checks before commits and pushes (scripts/install-git-hooks.sh, scripts/check-secrets.sh) --

# Built at runtime, so this file never holds anything that looks like a real token.
FAKE_TOKEN = "7364920185:AAH" + "Zq3-" * 8

# CI always runs these. Elsewhere they wait until gitleaks is installed.
needs_gitleaks = pytest.mark.skipif(
    shutil.which("gitleaks") is None and not os.environ.get("CI"), reason="needs gitleaks (brew install gitleaks)"
)


@pytest.fixture
def dev_clone(tmp_path) -> Path:
    """A clone of a stand-in for GitHub, set up like the computer you develop on."""
    github = tmp_path / "github.git"
    git("init", "--quiet", "--bare", str(github))
    clone = tmp_path / "dev"
    git("clone", "--quiet", str(github), str(clone))
    for name in [".gitignore", ".gitleaks.toml", "scripts/check-secrets.sh", "scripts/install-git-hooks.sh"]:
        (clone / name).parent.mkdir(exist_ok=True)
        shutil.copy(REPO / name, clone / name)
    git("add", "-A", cwd=clone)
    git("commit", "--quiet", "-m", "first", cwd=clone)
    git("push", "--quiet", "origin", "HEAD", cwd=clone)
    install_hooks(clone).check_returncode()
    return clone


def install_hooks(clone: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/install-git-hooks.sh"], cwd=clone, env=GIT_ENV, capture_output=True, text=True
    )


def commit(
    clone: Path, files: dict[str, str], message: str = "change", *flags: str
) -> subprocess.CompletedProcess[str]:
    for name, text in files.items():
        (clone / name).write_text(text)
    git("add", "--force", *files, cwd=clone)
    return git("commit", "--quiet", "-m", message, *flags, cwd=clone, check=False)


def on_github(clone: Path) -> str:
    """Every commit message the stand-in for GitHub has received."""
    return git("log", "--all", "--format=%B", cwd=clone.parent / "github.git").stdout


@needs_gitleaks
def test_commit_with_a_secret_is_stopped(dev_clone):
    result = commit(dev_clone, {"bot.py": f'TOKEN = "{FAKE_TOKEN}"\n'})  # with no mention of Telegram nearby
    assert result.returncode != 0 and "gitleaks found" in result.stderr
    assert FAKE_TOKEN not in result.stdout + result.stderr  # it's redacted
    assert commit(dev_clone, {"bot.py": 'TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]\n'}).returncode == 0


@needs_gitleaks
def test_commit_of_a_git_ignored_file_is_stopped(dev_clone):
    result = commit(dev_clone, {".env": "LLM_API_KEY=\n"})
    assert result.returncode != 0 and "    .env\n" in result.stderr


@needs_gitleaks
def test_commit_with_a_personal_detail_is_stopped(dev_clone):
    (dev_clone / ".personal-blocklist").write_text("# Never to be pushed\n  12 Example Road  \n")
    result = commit(dev_clone, {"notes.md": "Deliveries go to 12 EXAMPLE ROAD.\n"})
    assert result.returncode != 0 and "Line 2 of .personal-blocklist" in result.stderr
    assert "example road" not in result.stderr.lower()  # it says where, not what
    assert commit(dev_clone, {"notes.md": "Never to be pushed: see above.\n"}).returncode == 0  # comments don't count


@needs_gitleaks
def test_push_checks_the_commits_it_sends_and_only_those(dev_clone):
    # Something that skipped the checks is already on GitHub. It doesn't block later pushes.
    assert commit(dev_clone, {"old.py": f'T = "{FAKE_TOKEN}"\n'}, "old", "--no-verify").returncode == 0
    git("push", "--quiet", "--no-verify", "origin", "HEAD", cwd=dev_clone)
    commit(dev_clone, {"ok.md": "fine\n"}, "clean")
    assert git("push", "--quiet", "origin", "HEAD", cwd=dev_clone, check=False).returncode == 0
    assert "clean" in on_github(dev_clone)

    # A commit made with --no-verify is still checked when pushed, commit message included.
    (dev_clone / ".personal-blocklist").write_text("12 Example Road\n")
    commit(dev_clone, {"ok.md": "still fine\n"}, "Moved to 12 Example Road", "--no-verify")
    result = git("push", "origin", "HEAD", cwd=dev_clone, check=False)
    assert result.returncode != 0 and "Line 1 of .personal-blocklist" in result.stderr
    assert "Example Road" not in on_github(dev_clone)

    # So is a new branch, whose commits aren't on GitHub yet.
    (dev_clone / ".personal-blocklist").unlink()
    git("checkout", "--quiet", "-b", "feature", cwd=dev_clone)
    commit(dev_clone, {"new.py": f'T = "{FAKE_TOKEN}"\n'}, "new", "--no-verify")
    result = git("push", "origin", "feature", cwd=dev_clone, check=False)
    assert result.returncode != 0 and "gitleaks found" in result.stderr
    assert "new" not in on_github(dev_clone).split()


@needs_gitleaks
def test_history_check_finds_secrets_in_old_commits(dev_clone):
    commit(dev_clone, {"old.py": f'T = "{FAKE_TOKEN}"\n'}, "add", "--no-verify")
    assert commit(dev_clone, {"old.py": "T = None\n"}, "remove").returncode == 0
    result = subprocess.run(
        ["bash", "scripts/check-secrets.sh", "history"], cwd=dev_clone, env=GIT_ENV, capture_output=True, text=True
    )
    assert result.returncode != 0 and "revoke it" in result.stderr


def test_hooks_stop_everything_until_gitleaks_is_installed(dev_clone):
    path = os.pathsep.join(d for d in os.environ["PATH"].split(os.pathsep) if not (Path(d) / "gitleaks").exists())
    (dev_clone / "ok.md").write_text("fine\n")
    git("add", "ok.md", cwd=dev_clone)
    result = subprocess.run(
        ["git", "commit", "-m", "ok"], cwd=dev_clone, env={**GIT_ENV, "PATH": path}, capture_output=True, text=True
    )
    assert result.returncode != 0 and "brew install gitleaks" in result.stderr


def test_install_git_hooks_is_safe_to_rerun_and_leaves_other_hooks_alone(dev_clone):
    blocklist = dev_clone / ".personal-blocklist"
    blocklist.write_text("12 Example Road\n")
    assert install_hooks(dev_clone).returncode == 0
    assert blocklist.read_text() == "12 Example Road\n"  # never overwritten

    hook = dev_clone / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho someone else's hook\n")
    assert install_hooks(dev_clone).returncode != 0
    assert "someone else" in hook.read_text()


def test_install_git_hooks_refuses_on_the_pi(tmp_path):
    github = tmp_path / "github.git"
    git("init", "--quiet", "--bare", str(github))
    pi = tmp_path / "pi"
    git("clone", "--quiet", str(github), str(pi))
    (pi / "scripts").mkdir()
    for name in ["disable-git-push.sh", "install-git-hooks.sh"]:
        shutil.copy(REPO / "scripts" / name, pi / "scripts")
    subprocess.run(["bash", "scripts/disable-git-push.sh"], cwd=pi, env=GIT_ENV, check=True, capture_output=True)

    result = install_hooks(pi)
    assert result.returncode != 0 and "Pushing is turned off" in result.stderr
    assert "disabled" in (pi / ".git" / "hooks" / "pre-push").read_text()  # still the hook that blocks pushes
