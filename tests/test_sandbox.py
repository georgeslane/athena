"""The sandbox local MCP servers run in. The real thing needs Linux and bubblewrap: those tests are
skipped elsewhere, but required on CI, where both are set up."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from athena.config import MCPServerConfig
from athena.mcp_manager import MCPManager
from athena.sandbox import SSH_INCLUDES, Sandbox, SandboxError

REPO = Path(__file__).resolve().parents[1]
DEMO = str(REPO / "tests" / "fixtures" / "demo_mcp_server.py")


def _real() -> Sandbox | None:
    sandbox = Sandbox.detect()
    if not sandbox.supported:
        return None
    try:
        asyncio.run(sandbox.check())
    except SandboxError:
        if os.environ.get("CI"):
            raise  # CI installs bubblewrap: if it can't make a sandbox, the tests below must fail, not skip
        return None
    return sandbox


REAL = _real()
needs_bwrap = pytest.mark.skipif(REAL is None, reason="needs Linux with bubblewrap")


# -- the command line, anywhere --------------------------------------------------------------------


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home" / "pi"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "uvx").touch()
    python = home / "pythons" / "cpython-3.12" / "bin"  # wherever uv was told to put its Pythons
    python.mkdir(parents=True)
    venv = home / "athena" / "data" / "mcp" / "time" / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {python}\nimplementation = CPython\n")
    (home / ".ssh").mkdir()
    return home


def binds(args: list[str]) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == "--ro-bind-try"]


def test_the_sandbox_hides_your_home_and_shows_only_what_the_server_needs(home):
    sandbox = Sandbox("/usr/bin/bwrap", home)
    venv = home / "athena" / "data" / "mcp" / "time" / ".venv"
    args = sandbox.wrap([str(venv / "bin" / "mcp-server-time"), "--local-timezone=Europe/London"], network=False)
    assert args[:3] == ["/usr/bin/bwrap", "--ro-bind", "/"]
    assert args[args.index("--tmpfs", 4) :].count(str(home)) >= 1
    assert args.index(str(home)) < args.index("--ro-bind-try")  # emptied first, then the server's bits shown
    # The environment, and the Python it was made from (wherever that is).
    assert binds(args) == [
        str(home / ".local" / "share" / "uv" / "python"),
        str(venv),
        str(home / "pythons" / "cpython-3.12"),
    ]
    assert "--unshare-all" in args and "--share-net" not in args
    assert {"--die-with-parent", "--new-session"} <= set(args)
    assert args[args.index("--") + 1 :] == [str(venv / "bin" / "mcp-server-time"), "--local-timezone=Europe/London"]


def test_what_else_a_server_can_be_given(home, tmp_path):
    sandbox = Sandbox("/usr/bin/bwrap", home)
    notes = home / "notes"
    args = sandbox.wrap([str(home / ".local" / "bin" / "uvx"), "pkg"], network=True, read_only=[notes], cwd=notes)
    assert "--share-net" in args
    # A command in ~/.local/bin gets that folder, not the rest of ~/.local.
    assert binds(args) == [str(home / ".local" / "share" / "uv" / "python"), str(home / ".local" / "bin"), str(notes)]
    assert args[args.index("--chdir") + 1] == str(notes)
    # A command that's a link gets where the link leads too.
    tool = home / ".local" / "share" / "uv" / "tools" / "x" / "bin"
    tool.mkdir(parents=True)
    (tool / "x").touch()
    (home / ".local" / "bin" / "x").symlink_to(tool / "x")
    assert str(tool) in binds(sandbox.wrap([str(home / ".local" / "bin" / "x")], network=False))
    # ssh (for the Mac's servers) gets ~/.ssh; things outside your home are visible anyway, and what's
    # in a folder it's shown is shown with it, so neither is mounted again.
    assert str(home / ".ssh") in binds(sandbox.wrap([str(home / "bin" / "ssh"), "athena-mac", "apple"], network=True))
    read_only = [Path("/opt/data"), notes / "2026", notes]
    assert binds(sandbox.wrap(["/usr/bin/node", "x.js"], network=True, read_only=read_only)) == [
        str(home / ".local" / "share" / "uv" / "python"),
        str(notes),
    ]


def test_a_python_uv_links_to_is_shown_with_uvs_pythons(home):
    # As uv lays them out on the Pi: environments find their Python through a link, cpython-3.14-…, to
    # the newest 3.14, in uv's folder of Pythons. That folder is shown whole, so the link and where it
    # leads are shown with it: bubblewrap won't mount on top of a link that's already showing.
    pythons = home / ".local" / "share" / "uv" / "python"
    newest = pythons / "cpython-3.14.7-linux-aarch64-gnu"
    (newest / "bin").mkdir(parents=True)
    (newest / "bin" / "python3.14").touch()
    link = pythons / "cpython-3.14-linux-aarch64-gnu"
    link.symlink_to(newest)
    venv = home / "athena" / "data" / "mcp" / "search" / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {link / 'bin'}\nimplementation = CPython\n")
    (venv / "bin" / "python").symlink_to(link / "bin" / "python3.14")
    (venv / "bin" / "duckduckgo-mcp-server").touch()
    sandbox = Sandbox("/usr/bin/bwrap", home)
    for program in ("duckduckgo-mcp-server", "python"):
        assert binds(sandbox.wrap([str(venv / "bin" / program)], network=True)) == [str(pythons), str(venv)]


def test_ssh_doesnt_see_the_settings_it_would_refuse(home, tmp_path, monkeypatch):
    # In the sandbox, root's files belong to nobody, and ssh won't read settings it includes from
    # /etc/ssh/ssh_config.d unless root or you own them. systemd puts one there on the Pi.
    includes = tmp_path / "ssh_config.d"
    includes.mkdir()
    monkeypatch.setattr("athena.sandbox.SSH_INCLUDES", includes)
    sandbox = Sandbox("/usr/bin/bwrap", home)
    for ssh in ("ssh", "/usr/bin/ssh"):
        args = sandbox.wrap([ssh, "athena-mac", "apple"], network=True)
        assert args[args.index(str(includes)) - 1] == "--tmpfs"
        assert args.index(str(includes)) < args.index("--")
    assert str(includes) not in sandbox.wrap(["/usr/bin/node", "x.js"], network=True)


async def test_without_bubblewrap_a_server_isnt_started(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    sandbox = Sandbox(None, Path.home())
    manager = MCPManager({"demo": MCPServerConfig(command=sys.executable, args=[DEMO])}, sandbox=sandbox)
    await manager.start()
    try:
        [status] = manager.status()
        assert not status.connected and "bubblewrap isn't installed" in status.error
        assert "sudo apt install bubblewrap" in status.error and manager.tools() == []
    finally:
        await manager.stop()


async def test_servers_run_through_the_sandbox_unless_told_not_to(monkeypatch):
    class Recording(Sandbox):
        """Runs the command as it is, through env, which also marks it as having been wrapped."""

        wrapped: list[tuple[list[str], bool]] = []

        @property
        def supported(self):
            return True

        async def check(self):
            pass

        def wrap(self, command, *, network, read_only=(), cwd=None):
            self.wrapped.append((command, network))
            return ["/usr/bin/env", "SANDBOXED=yes", *command]

    servers = {
        "boxed": MCPServerConfig(command=sys.executable, args=[DEMO], network=False, include=["get_env"]),
        "free": MCPServerConfig(command=sys.executable, args=[DEMO], sandbox=False, include=["get_env"]),
    }
    manager = MCPManager(servers, sandbox=Recording(None, Path.home()))
    await manager.start()
    try:
        boxed, free = manager.status()
        assert boxed.sandboxed and not free.sandboxed
        tools = {t.name: t for t in manager.tools()}
        assert await tools["get_env"].handler({"name": "SANDBOXED"}) == "yes"
        assert await tools["free__get_env"].handler({"name": "SANDBOXED"}) == "(unset)"
        assert Recording.wrapped == [([sys.executable, DEMO], False)]
    finally:
        await manager.stop()


def test_on_a_mac_servers_run_as_they_are():
    if sys.platform == "linux":
        pytest.skip("not a Mac")
    assert not Sandbox.detect().supported


# -- the real thing, on Linux ------------------------------------------------------------------------


@needs_bwrap
async def test_a_sandboxed_process_cant_see_your_files_or_the_network():
    assert REAL is not None
    results = await REAL.self_test()
    assert results and all(ok for ok, _ in results), results
    assert {"Python servers can start in it", "ssh can read its settings in it"} <= {what for _, what in results}


@needs_bwrap
async def test_a_python_uv_links_to_starts_in_the_sandbox(tmp_path):
    """The Pi's layout, for real: an environment that finds its Python through the link uv makes, in its
    folder of Pythons in your home folder, to the newest version. Here that's Athena's own Python."""
    assert REAL is not None
    home = tmp_path / "home" / "pi"
    pythons = home / ".local" / "share" / "uv" / "python"
    pythons.mkdir(parents=True)
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    newest = pythons / f"cpython-{minor}.{sys.version_info.micro}-linux"
    newest.symlink_to(Path(sys.base_prefix).resolve())
    link = pythons / f"cpython-{minor}-linux"
    link.symlink_to(newest)
    assert (link / "bin" / f"python{minor}").exists()
    venv = home / "athena" / "data" / "mcp" / "demo" / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {link / 'bin'}\ninclude-system-site-packages = false\n")
    (venv / "bin" / "python").symlink_to(link / "bin" / f"python{minor}")

    command = [str(venv / "bin" / "python"), "-c", "import sys; print(sys.prefix)"]
    proc = await asyncio.create_subprocess_exec(
        *Sandbox(REAL.bwrap, home).wrap(command, network=False),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    assert proc.returncode == 0, out
    assert out.decode().strip() == str(venv)


@needs_bwrap
async def test_ssh_reads_its_settings_in_the_sandbox():
    """ssh, which links Athena to your Macs, with a settings file root owns in /etc/ssh/ssh_config.d, as
    systemd puts there on the Pi. CI puts one there too."""
    assert REAL is not None
    ssh = shutil.which("ssh")
    assert ssh, "ssh isn't installed"
    if os.environ.get("CI"):
        assert any(p.stat().st_uid == 0 for p in SSH_INCLUDES.glob("*.conf")), "CI should put one there"
    proc = await asyncio.create_subprocess_exec(
        *REAL.wrap([ssh, "-G", "athena-mac"], network=False),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    assert proc.returncode == 0, out
    assert b"hostname athena-mac" in out.lower(), out


@needs_bwrap
async def test_a_server_runs_in_the_sandbox_and_sees_only_what_its_given(tmp_path):
    assert REAL is not None
    secret = Path.home() / f".athena-test-secret-{os.getpid()}"
    secret.write_text("not for servers")
    try:
        server = MCPServerConfig(command=sys.executable, args=[DEMO], network=False, read_only_paths=[str(REPO)])
        manager = MCPManager({"demo": server}, base_dir=REPO, sandbox=REAL)
        await manager.start()
        try:
            [status] = manager.status()
            assert status.connected and status.sandboxed, status.error
            tools = {t.name: t for t in manager.tools()}
            assert await tools["add"].handler({"a": 2, "b": 40}) == "42"
        finally:
            await manager.stop()

        # The same, run by hand: the server's own view of your home folder.
        probe = f"import os; print(os.path.exists({str(secret)!r}), os.listdir({str(Path.home())!r}))"
        proc = await asyncio.create_subprocess_exec(
            *REAL.wrap([sys.executable, "-c", probe], network=False, read_only=[REPO]),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        assert proc.returncode == 0, out
        assert out.decode().startswith("False"), out
    finally:
        secret.unlink()
