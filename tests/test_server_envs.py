"""Locked environments for MCP servers started with uvx: every dependency pinned, with hashes."""

import base64
import hashlib
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from pi_assistant.config import MCPServerConfig
from pi_assistant.mcp_manager import MCPManager
from pi_assistant.server_envs import ServerEnvError, ServerEnvs, UvxServer, parse_uvx, project_text

REPO = Path(__file__).resolve().parents[1]
DEMO = Path(__file__).parent / "fixtures" / "demo_mcp_server.py"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["mcp-server-fetch==2026.8.18"], UvxServer("mcp-server-fetch==2026.8.18", "mcp-server-fetch")),
        (
            ["mcp-server-time@2026.8.18", "--local-timezone=Europe/London"],
            UvxServer("mcp-server-time==2026.8.18", "mcp-server-time", ("--local-timezone=Europe/London",)),
        ),
        (
            ["--from", "edgartools[ai]==5.60.0", "edgartools-mcp"],
            UvxServer("edgartools[ai]==5.60.0", "edgartools-mcp"),
        ),
        (["--from=pkg==1", "cmd", "stdio"], UvxServer("pkg==1", "cmd", ("stdio",))),
        (["mcp-email-server"], UvxServer("mcp-email-server", "mcp-email-server")),  # unpinned: locked on first use
        (["--with", "extra", "pkg==1"], None),  # other uvx options aren't understood, so it isn't locked
        (["--from", "pkg==1", "--help"], None),
        (["pkg>=1"], None),
        ([], None),
    ],
)
def test_reading_what_uvx_would_run(args, expected):
    assert parse_uvx(args) == expected


def test_the_recommended_servers_have_reviewed_locks():
    """mcp-locks/ holds a lock for every uvx server in config.example.toml, and nothing else.
    After changing one: uv run python -m pi_assistant.server_envs"""
    servers = tomllib.loads((REPO / "config.example.toml").read_text())["mcp_servers"]
    uvx = {name: parse_uvx(cfg["args"]) for name, cfg in servers.items() if cfg.get("command") == "uvx"}
    assert uvx and None not in uvx.values()
    assert {p.name for p in (REPO / "mcp-locks").iterdir()} == set(uvx)
    for name, server in uvx.items():
        assert server.pinned
        assert (REPO / "mcp-locks" / name / "pyproject.toml").read_text() == project_text(name, server)
        lock = tomllib.loads((REPO / "mcp-locks" / name / "uv.lock").read_text())
        packages = {p["name"]: p for p in lock["package"]}
        name_only = server.requirement.split("[")[0].split("==")[0]
        assert packages[name_only]["version"] == server.requirement.split("==")[1]
        # Every package that's installed from PyPI has a hash, and a ready-built wheel.
        for package in packages.values():
            if "registry" in package.get("source", {}):
                assert all(w["hash"].startswith("sha256:") for w in package["wheels"]), package["name"]


# -- with uv itself, and a made-up server from a local package index ---------------------------------


def _wheel(index: Path, version: str) -> None:
    """A wheel for athena-fake-server, whose command starts the demo MCP server."""
    files = {
        "athena_fake_server/__init__.py": (
            f"import os, sys\n\ndef main():\n    os.execv({sys.executable!r}, [{sys.executable!r}, {str(DEMO)!r}])\n"
        ),
        f"athena_fake_server-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: athena-fake-server\n"
        f"Version: {version}\n",
        f"athena_fake_server-{version}.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
        "Tag: py3-none-any\n",
        f"athena_fake_server-{version}.dist-info/entry_points.txt": "[console_scripts]\n"
        "athena-fake-server = athena_fake_server:main\n",
    }
    record = []
    for path, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{path},sha256={digest},{len(text.encode())}")
    record.append(f"athena_fake_server-{version}.dist-info/RECORD,,")
    files[f"athena_fake_server-{version}.dist-info/RECORD"] = "\n".join(record) + "\n"
    with zipfile.ZipFile(index / f"athena_fake_server-{version}-py3-none-any.whl", "w") as wheel:
        for path, text in files.items():
            wheel.writestr(path, text)


@pytest.fixture
def envs(tmp_path):
    index = tmp_path / "index"
    index.mkdir()
    _wheel(index, "1.0")
    _wheel(index, "2.0")
    uv_env = {"UV_NO_INDEX": "1", "UV_FIND_LINKS": str(index), "UV_CACHE_DIR": str(tmp_path / "cache")}
    return ServerEnvs(tmp_path / "mcp", tmp_path / "reviewed", malware_check=False, uv_env=uv_env)


async def test_a_uvx_server_runs_from_its_locked_environment(envs):
    manager = MCPManager({"fake": MCPServerConfig(command="uvx", args=["athena-fake-server==1.0"])}, envs=envs)
    await manager.start()
    try:
        [status] = manager.status()
        assert status.connected, status.error
        assert status.lock == "first use"
        tools = {t.name: t for t in manager.tools()}
        assert await tools["add"].handler({"a": 2, "b": 40}) == "42"
    finally:
        await manager.stop()
    project = envs.root / "fake"
    lock = tomllib.loads((project / "uv.lock").read_text())
    [fake] = [p for p in lock["package"] if p["name"] == "athena-fake-server"]
    assert fake["version"] == "1.0"  # (a local wheel has no hash in the lock; PyPI's do, as checked above)


async def test_the_lock_only_changes_with_the_version(envs):
    one = UvxServer("athena-fake-server==1.0", "athena-fake-server")
    command = await envs.prepare("fake", one)
    lock = (envs.root / "fake" / "uv.lock").read_text()
    assert command == [str(envs.root / "fake" / ".venv" / "bin" / "athena-fake-server")]

    # Starting again reuses everything: no uv at all, so no network.
    envs.uv = "/nonexistent/uv"
    assert await envs.prepare("fake", one) == command
    assert (envs.root / "fake" / "uv.lock").read_text() == lock

    envs.uv = "uv"
    await envs.prepare("fake", UvxServer("athena-fake-server==2.0", "athena-fake-server"))
    assert 'version = "2.0"' in (envs.root / "fake" / "uv.lock").read_text()


async def test_a_reviewed_lock_is_used_when_it_matches(envs):
    server = UvxServer("athena-fake-server==1.0", "athena-fake-server")
    await envs.prepare("made", server)  # make a lock to stand in for a reviewed one
    reviewed = envs.reviewed / "fake"
    reviewed.mkdir(parents=True)
    (reviewed / "pyproject.toml").write_text(project_text("fake", server))
    (reviewed / "uv.lock").write_text(
        (envs.root / "made" / "uv.lock").read_text().replace("athena-mcp-made", "athena-mcp-fake")
    )

    await envs.prepare("fake", server)
    assert envs.lock_origin("fake") == "reviewed"
    assert (envs.root / "fake" / "uv.lock").read_text() == (reviewed / "uv.lock").read_text()


@pytest.mark.parametrize(
    ("server", "error"),
    [
        (UvxServer("athena-fake-server==9.9", "athena-fake-server"), "Couldn't work out athena-fake-server==9.9's"),
        (UvxServer("athena-fake-server==1.0", "something-else"), "has no command called 'something-else'"),
    ],
)
async def test_what_went_wrong_is_said(envs, server, error):
    with pytest.raises(ServerEnvError, match=error):
        await envs.prepare("fake", server)


async def test_a_server_that_cant_be_installed_shows_why(envs):
    manager = MCPManager({"fake": MCPServerConfig(command="uvx", args=["athena-fake-server==9.9"])}, envs=envs)
    await manager.start()
    try:
        [status] = manager.status()
        assert not status.connected and "Couldn't work out athena-fake-server==9.9's dependencies" in status.error
        assert "athena-fake-server" in status.error and manager.tools() == []
    finally:
        await manager.stop()


async def test_installing_is_checked_for_malware_and_builds_nothing(tmp_path, monkeypatch):
    envs = ServerEnvs(tmp_path / "mcp")
    calls = []

    async def uv(args, cwd, failure, extra=None):
        calls.append((args, extra))
        if args[0] == "lock":
            (cwd / "uv.lock").write_text("locked")
        else:
            (cwd / ".venv" / "bin").mkdir(parents=True)
            (cwd / ".venv" / "bin" / "srv").touch()

    monkeypatch.setattr(envs, "_uv", uv)
    await envs.prepare("srv", UvxServer("srv==1", "srv"))
    sync, extra = calls[1]
    assert sync[:4] == ["sync", "--frozen", "--no-build", "--no-install-project"]
    assert extra == {"UV_MALWARE_CHECK": "1", "UV_PREVIEW_FEATURES": "malware-check"}


async def test_uv_doesnt_see_athenas_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("UV_INDEX_URL", "https://example.test/simple")
    script = tmp_path / "uv"
    script.write_text(f"#!/bin/sh\nenv > {tmp_path / 'env'}\n")
    script.chmod(0o755)
    envs = ServerEnvs(tmp_path / "mcp", uv=str(script))
    with pytest.raises(ServerEnvError):  # the stand-in makes no lock, so it fails after recording
        await envs.prepare("srv", UvxServer("srv==1", "srv"))
    seen = (tmp_path / "env").read_text()
    assert "secret" not in seen and "UV_INDEX_URL=https://example.test/simple" in seen


# -- checking for known vulnerabilities ----------------------------------------------------------------

VULNERABLE = """Found 5 known vulnerabilities and no adverse project statuses in 33 packages

Vulnerabilities:

idna 2.7 has 4 known vulnerabilities:

- GHSA-65pc-fj4g-8rjx: Specially crafted inputs to idna.encode() can bypass CVE-2024-3651 fix

urllib3 1.23 has 1 known vulnerability:
"""


@pytest.mark.parametrize(
    ("output", "code", "clean", "summary", "affected"),
    [
        ("Found no known vulnerabilities and no adverse project statuses in 33 packages\n", 0, True, "Found no", []),
        (VULNERABLE, 1, False, "Found 5 known vulnerabilities", ["idna 2.7", "urllib3 1.23"]),
        ("error: Network connectivity is disabled\n", 2, None, "couldn't check: error: Network", []),
        ("error: unrecognized subcommand 'audit'\n", 2, None, "update it with `uv self update`", []),
    ],
)
async def test_auditing_a_servers_dependencies(tmp_path, output, code, clean, summary, affected):
    script = tmp_path / "uv"
    (tmp_path / "out").write_text(output)
    script.write_text(f'#!/bin/sh\necho "$*" > {tmp_path / "args"}\ncat {tmp_path / "out"}\nexit {code}\n')
    script.chmod(0o755)
    envs = ServerEnvs(tmp_path / "mcp", uv=str(script))
    assert (await envs.audit("time")).summary == "not installed yet"
    (tmp_path / "mcp" / "time").mkdir(parents=True)
    (tmp_path / "mcp" / "time" / "uv.lock").write_text("")
    audit = await envs.audit("time")
    assert (audit.clean, audit.affected) == (clean, affected) and summary in audit.summary
    assert (tmp_path / "args").read_text().strip() == "audit --frozen --preview-features audit-command"


async def test_doctor_says_how_each_server_is_locked(tmp_path):
    from pi_assistant.doctor import check_lock
    from pi_assistant.mcp_manager import ServerStatus
    from pi_assistant.server_envs import Audit

    class Envs:
        async def audit(self, name):
            return {
                "clean": Audit(True, "Found no known vulnerabilities in 33 packages"),
                "bad": Audit(False, "Found 4 known vulnerabilities in 33 packages", ["idna 2.7"]),
                "offline": Audit(None, "couldn't check: error: no network"),
            }[name]

    seen = []
    for name, lock in [("clean", "reviewed"), ("bad", "first use"), ("offline", "reviewed"), ("odd", "unlocked")]:
        await check_lock(Envs(), ServerStatus(name, lock=lock), lambda mark, msg: seen.append((mark, msg)))
    await check_lock(Envs(), ServerStatus("ssh-server"), lambda mark, msg: seen.append((mark, msg)))
    marks = [mark for mark, _ in seen]
    assert len(seen) == 4 and marks[0] != marks[1] and marks[2] == marks[3]
    assert "reviewed lock (mcp-locks/); found no known vulnerabilities" in seen[0][1]
    assert "Found 4 known vulnerabilities in 33 packages (idna 2.7)" in seen[1][1]
    assert "not checked: couldn't check" in seen[2][1] and "aren't pinned" in seen[3][1]
