"""Locked environments for MCP servers started with uvx: every dependency pinned, with hashes."""

import base64
import hashlib
import json
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from athena.config import MCPServerConfig
from athena.mcp_manager import MCPManager
from athena.resources import CONFIG_EXAMPLE, MCP_LOCKS
from athena.server_envs import (
    Audit,
    Finding,
    ServerEnvError,
    ServerEnvs,
    UvxServer,
    _findings,
    parse_uvx,
    project_text,
)

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
    After changing one: uv run python -m athena.server_envs"""
    servers = tomllib.loads(CONFIG_EXAMPLE.read_text())["mcp_servers"]
    uvx = {name: parse_uvx(cfg["args"]) for name, cfg in servers.items() if cfg.get("command") == "uvx"}
    assert uvx and None not in uvx.values()
    assert {p.name for p in MCP_LOCKS.iterdir()} == set(uvx)
    for name, server in uvx.items():
        assert server.pinned
        assert (MCP_LOCKS / name / "pyproject.toml").read_text() == project_text(name, server)
        lock = tomllib.loads((MCP_LOCKS / name / "uv.lock").read_text())
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


async def test_a_changed_reviewed_lock_reaches_a_server_already_installed(envs):
    """As when a dependency's security fix updates mcp-locks/, with the server's own version the same."""
    server = UvxServer("athena-fake-server==1.0", "athena-fake-server")
    await envs.prepare("fake", server)  # locked on first use, before there was a reviewed lock
    assert envs.lock_origin("fake") == "first use"
    stamp = envs.root / "fake" / ".venv" / ".athena-lock"
    installed = stamp.read_text()

    reviewed = envs.reviewed / "fake"
    reviewed.mkdir(parents=True)
    (reviewed / "pyproject.toml").write_text(project_text("fake", server))
    (reviewed / "uv.lock").write_text((envs.root / "fake" / "uv.lock").read_text() + "# reviewed\n")
    command = await envs.prepare("fake", server)
    assert envs.lock_origin("fake") == "reviewed"
    assert (envs.root / "fake" / "uv.lock").read_text() == (reviewed / "uv.lock").read_text()
    assert stamp.read_text() != installed  # installed again, from the reviewed lock
    assert Path(command[0]).exists()

    (reviewed / "uv.lock").write_text((reviewed / "uv.lock").read_text() + "# a security fix\n")
    await envs.prepare("fake", server)
    assert (envs.root / "fake" / "uv.lock").read_text().endswith("# a security fix\n")

    envs.uv = "/nonexistent/uv"  # and when nothing's changed, nothing's done
    await envs.prepare("fake", server)


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


def _report(*vulnerabilities, packages=33):
    return json.dumps(
        {
            "schema": {"version": "preview"},
            "summary": {"audited_packages": packages, "vulnerabilities": len(vulnerabilities), "adverse_statuses": 0},
            "vulnerabilities": list(vulnerabilities),
            "adverse_statuses": [],
        }
    )


def _advisory(package, version, id, fixes, aliases=(), summary="Something bad"):
    return {
        "dependency": {"name": package, "version": version},
        "id": id,
        "display_id": id,
        "aliases": list(aliases),
        "summary": summary,
        "fix_versions": list(fixes),
    }


CERTIFI = [
    _advisory("certifi", "2026.7.22", "GHSA-6q48", ["2026.10.19"], ["PYSEC-4320"], "certifi bundles emSign roots"),
    _advisory("certifi", "2026.7.22", "PYSEC-4320", ["2026.10.19"], ["GHSA-6q48"], "certifi bundles emSign roots"),
]


@pytest.mark.parametrize(
    ("stdout", "stderr", "code", "clean", "summary", "affected"),
    [
        (_report(), "", 0, True, "Found no known vulnerabilities in 33 packages", []),
        (
            _report(*CERTIFI),
            "warning: preview\n",
            1,
            False,
            "Found 2 known vulnerabilities in 33 packages",
            ["certifi 2026.7.22"],
        ),
        ("", "error: Network connectivity is disabled\n", 2, None, "couldn't check: error: Network", []),
        ("", "error: unrecognized subcommand 'audit'\n", 2, None, "update it with `uv self update`", []),
    ],
)
async def test_auditing_a_servers_dependencies(tmp_path, stdout, stderr, code, clean, summary, affected):
    script = tmp_path / "uv"
    (tmp_path / "out").write_text(stdout)
    (tmp_path / "err").write_text(stderr)
    script.write_text(
        f'#!/bin/sh\necho "$*" > {tmp_path / "args"}\ncat {tmp_path / "out"}\ncat {tmp_path / "err"} >&2\nexit {code}\n'
    )
    script.chmod(0o755)
    envs = ServerEnvs(tmp_path / "mcp", uv=str(script))
    assert (await envs.audit("time")).summary == "not installed yet"
    (tmp_path / "mcp" / "time").mkdir(parents=True)
    (tmp_path / "mcp" / "time" / "uv.lock").write_text("")
    audit = await envs.audit("time")
    assert (audit.clean, audit.affected) == (clean, affected) and summary in audit.summary
    assert (
        tmp_path / "args"
    ).read_text().strip() == "audit --frozen --preview-features audit-command --output-format json"


def test_the_fix_for_each_package_is_the_lowest_version_that_fixes_every_advisory():
    findings = _findings(
        [
            *CERTIFI,  # one advisory under two names: counted once
            _advisory("urllib3", "1.23", "A", ["1.24.2", "2.0.7"]),  # the lowest above 1.23
            _advisory("urllib3", "1.23", "B", ["1.26.5"]),  # and another needs more: the higher wins
            _advisory("urllib3", "1.23", "C", ["1.20"]),  # an old fix line, below what's locked: ignored, so no fix
            _advisory("idna", "2.7", "D", []),  # no fix yet
            _advisory("jinja2", "3.1.4", "E", ["3.1.6", "3.1.5"]),
        ]
    )
    by = {f.package: f for f in findings}
    assert by["certifi"].fixed == "2026.10.19" and by["certifi"].ids == ("GHSA-6q48", "PYSEC-4320")
    assert by["urllib3"].fixed is None  # advisory C has no fix above 1.23
    assert by["idna"].fixed is None
    assert by["jinja2"].fixed == "3.1.5"
    assert by["certifi"].describe() == "certifi 2026.7.22 (GHSA-6q48, PYSEC-4320: certifi bundles emSign roots)"
    both = _findings([_advisory("urllib3", "1.23", "A", ["1.24.2"]), _advisory("urllib3", "1.23", "B", ["1.26.5"])])
    assert both[0].fixed == "1.26.5"


async def test_doctor_says_how_each_server_is_locked(tmp_path):
    from athena.doctor import check_lock
    from athena.mcp_manager import ServerStatus
    from athena.server_envs import Audit, Finding

    idna = Finding("idna", "2.7", ("PYSEC-1",), "Denial of service", "3.7")
    bash = Finding("bashlex", "0.1", ("GHSA-2",), "Bad parse", None)

    class Envs:
        async def audit(self, name):
            return {
                "clean": Audit(True, "Found no known vulnerabilities in 33 packages"),
                "bad": Audit(
                    False, "Found 2 known vulnerabilities in 33 packages", ["idna 2.7", "bashlex 0.1"], [idna, bash]
                ),
                "offline": Audit(None, "couldn't check: error: no network"),
            }[name]

        def fixed(self, name):
            return ["certifi 2026.7.22 -> 2026.10.19"] if name == "clean" else []

    seen = []
    for name, lock in [("clean", "reviewed"), ("bad", "first use"), ("offline", "reviewed"), ("odd", "unlocked")]:
        await check_lock(Envs(), ServerStatus(name, lock=lock), lambda mark, msg: seen.append((mark, msg)))
    await check_lock(Envs(), ServerStatus("ssh-server"), lambda mark, msg: seen.append((mark, msg)))
    marks = [mark for mark, _ in seen]
    assert len(seen) == 5 and marks[0] != marks[1] and marks[2] == marks[3] == marks[4]
    assert (
        "reviewed lock (mcp-locks/), with security updates (certifi 2026.7.22 -> 2026.10.19); "
        "found no known vulnerabilities" in seen[0][1]
    )
    assert seen[1][1] == (
        "bad: Found 2 known vulnerabilities in 33 packages: idna 2.7 (PYSEC-1: Denial of service), fixed in 3.7; "
        "bashlex 0.1 (GHSA-2: Bad parse), no fix yet"
    )
    assert "press Fix on it in the dashboard's Tools tab" in seen[2][1]
    assert "not checked: couldn't check" in seen[3][1] and "aren't pinned" in seen[4][1]


# -- fixing what has known vulnerabilities ---------------------------------------------------------------


def _plain_wheel(index: Path, name: str, version: str, requires: str = "") -> None:
    """A wheel with nothing in it but a command that does nothing, its metadata, and what it depends on."""
    dist = name.replace("-", "_")
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + (
        f"Requires-Dist: {requires}\n" if requires else ""
    )
    files = {
        f"{dist}/__init__.py": "def main():\n    pass\n",
        f"{dist}-{version}.dist-info/entry_points.txt": f"[console_scripts]\n{name} = {dist}:main\n",
        f"{dist}-{version}.dist-info/METADATA": meta,
        f"{dist}-{version}.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = [f"{path},," for path in files] + [f"{dist}-{version}.dist-info/RECORD,,"]
    files[f"{dist}-{version}.dist-info/RECORD"] = "\n".join(record) + "\n"
    with zipfile.ZipFile(index / f"{dist}-{version}-py3-none-any.whl", "w") as wheel:
        for path, text in files.items():
            wheel.writestr(path, text)


@pytest.fixture
def vulnerable(tmp_path):
    """A server whose locked dependency, athena-fake-dep 1.0, has a known vulnerability that 1.1 fixes."""
    index = tmp_path / "dep-index"
    index.mkdir()
    _wheel(index, "1.0")
    for version in ("1.0", "1.1", "2.0"):
        _plain_wheel(index, "athena-fake-dep", version)
    _plain_wheel(index, "athena-dep-server", "1.0", "athena-fake-dep>=1.0")
    uv_env = {"UV_NO_INDEX": "1", "UV_FIND_LINKS": str(index), "UV_CACHE_DIR": str(tmp_path / "cache")}
    envs = ServerEnvs(tmp_path / "mcp", tmp_path / "reviewed", malware_check=False, uv_env=uv_env)
    finding = Finding("athena-fake-dep", "1.0", ("GHSA-test",), "A test vulnerability", "1.1")
    real_audit = envs.audit

    async def audit(name):  # OSV, as if it knew of the vulnerability: only 1.0 has it
        lock = (envs.root / name / "uv.lock").read_text()
        if 'name = "athena-fake-dep"\nversion = "1.0"' in lock:
            return Audit(False, "Found 1 known vulnerability in 2 packages", ["athena-fake-dep 1.0"], [finding])
        return Audit(True, "Found no known vulnerabilities in 2 packages")

    envs.audit = audit
    envs.real_audit = real_audit
    return envs


async def _lock_at_1_0(envs, name="dep"):
    """Lock the server with athena-fake-dep 1.0, as if 1.1 weren't out when it was first used."""
    server = UvxServer("athena-dep-server==1.0", "athena-dep-server")
    project = envs.root / name
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text(project_text(name, server))
    code, output = await envs._run(["lock", "--upgrade-package", "athena-fake-dep==1.0"], project)
    assert code == 0, output
    return server


def _dep_version(envs, name="dep"):
    lock = tomllib.loads((envs.root / name / "uv.lock").read_text())
    return next(p["version"] for p in lock["package"] if p["name"] == "athena-fake-dep")


async def test_fixing_updates_just_the_vulnerable_package_to_the_version_that_fixes_it(vulnerable):
    envs = vulnerable
    await _lock_at_1_0(envs)
    assert _dep_version(envs) == "1.0"
    result = await envs.fix("dep")
    assert result.updated == [("athena-fake-dep", "1.0", "1.1")] and result.error is None
    assert _dep_version(envs) == "1.1"  # the version that fixes it, not the newest (2.0)
    assert result.before.clean is False and result.after.clean is True
    assert envs.fixed("dep") == ["athena-fake-dep 1.0 -> 1.1"]
    assert (await envs.fix("dep")).updated == []  # nothing left to do


async def test_a_fix_made_here_isnt_undone_by_a_reviewed_lock_until_that_changes(vulnerable):
    envs = vulnerable
    server = await _lock_at_1_0(envs, name="made")
    reviewed = envs.reviewed / "dep"
    reviewed.mkdir(parents=True)
    (reviewed / "pyproject.toml").write_text(project_text("dep", server))
    (reviewed / "uv.lock").write_text(
        (envs.root / "made" / "uv.lock").read_text().replace("athena-mcp-made", "athena-mcp-dep")
    )
    await envs.prepare("dep", server)
    assert envs.lock_origin("dep") == "reviewed" and _dep_version(envs) == "1.0"

    await envs.fix("dep")
    await envs.prepare("dep", server)  # starting again: the reviewed lock hasn't changed, so the fix stays
    assert _dep_version(envs) == "1.1" and envs.fixed("dep") == ["athena-fake-dep 1.0 -> 1.1"]

    (reviewed / "uv.lock").write_text((reviewed / "uv.lock").read_text() + "# updated in the repo\n")
    await envs.prepare("dep", server)  # it has now: it's taken, as it's been reviewed
    assert (envs.root / "dep" / "uv.lock").read_text().endswith("# updated in the repo\n")
    assert envs.fixed("dep") == []


async def test_a_fix_uv_cant_make_says_why(vulnerable):
    envs = vulnerable
    await _lock_at_1_0(envs)
    impossible = Finding("athena-fake-dep", "1.0", ("GHSA-test",), "A test vulnerability", "9.9")

    async def audit(name):
        return Audit(False, "Found 1 known vulnerability in 2 packages", ["athena-fake-dep 1.0"], [impossible])

    envs.audit = audit
    result = await envs.fix("dep")
    assert result.updated == [] and result.error.startswith("Couldn't update athena-fake-dep 9.9")
    assert _dep_version(envs) == "1.0" and envs.fixed("dep") == []
