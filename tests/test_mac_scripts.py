"""The link to the Mac: scripts/mac/install.sh (with stand-ins for launchctl, uv and friends) and
scripts/connect-mac.sh."""

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq5tBo4d5w0tFqkLXYOEOOQhX7r3uB9nI2TQ2hS0j8f athena@pi"
OTHER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKeyOtherKeyOtherKeyOtherKeyOtherKey12 athena@new-pi"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

RECORD = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\n'
STUBS = {
    "uname": '#!/bin/sh\necho "${STUB_UNAME:-Darwin}"\n',
    "launchctl": RECORD,
    "plutil": RECORD,
    "uv": RECORD + 'echo "UV_PROJECT_ENVIRONMENT=$UV_PROJECT_ENVIRONMENT" >> "$STUB_LOG"\n',
    "nc": RECORD + "exit 1\n",  # nothing listening, so Remote Login looks off
    "curl": RECORD + "exit 1\n",
}


@pytest.fixture
def mac(tmp_path):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, script in STUBS.items():
        (stubs / name).write_text(script)
        (stubs / name).chmod(0o755)
    log = tmp_path / "log"
    log.touch()
    env = {"HOME": str(home), "PATH": f"{stubs}:/usr/bin:/bin", "STUB_LOG": str(log)}

    class Mac:
        app = home / "Library" / "Application Support" / "Athena"
        keys = home / ".ssh" / "authorized_keys"
        agents = home / "Library" / "LaunchAgents"

        def __init__(self):
            self.home, self.log = home, log

        def install(self, *args, script=REPO / "scripts" / "mac" / "install.sh", **extra_env):
            return subprocess.run(
                ["bash", str(script), *args],
                env={**env, **extra_env},
                capture_output=True,
                text=True,
            )

        def ssh_command(self, original):
            return subprocess.run(
                [str(self.app / "ssh-command")],
                env={**env, "SSH_ORIGINAL_COMMAND": original},
                capture_output=True,
                text=True,
            )

        def calls(self, tool):
            return [line for line in self.log.read_text().splitlines() if line.startswith(tool + " ")]

    return Mac()


def our_line(mac, key=KEY):
    return f'restrict,command="{mac.app}/ssh-command" {key}'


def test_install_sets_up_both_servers_for_the_pi_key_only(mac):
    result = mac.install("--pi-key", KEY, "--folder", "~/Documents")
    assert result.returncode == 0, result.stderr

    assert (mac.app / "folders").read_text() == f"{mac.home}/Documents\n"
    assert mac.keys.read_text() == our_line(mac) + "\n"
    assert oct(mac.keys.stat().st_mode & 0o777) == "0o600"
    assert oct((mac.home / ".ssh").stat().st_mode & 0o777) == "0o700"

    uid = os.getuid()
    for name in ["files", "apple"]:
        plist = plistlib.loads((mac.agents / f"local.athena.{name}.plist").read_bytes())
        assert plist["Label"] == f"local.athena.{name}"
        assert plist["ProgramArguments"] == [f"{mac.app}/run-{name}"]
        listener = plist["Sockets"]["Listener"]
        assert listener["SockPathName"] == f"{mac.app}/{name}.sock" and listener["SockPathMode"] == 0o600
        assert plist["inetdCompatibility"] == {"Wait": False}  # a server per connection
        assert f"launchctl bootstrap gui/{uid} {mac.agents}/local.athena.{name}.plist" in mac.calls("launchctl")

    # stderr goes to a log, so it can't garble the conversation with the Pi.
    assert (mac.app / "run-files").read_text().splitlines()[1:] == [
        f'exec 2>>"{mac.home}/Library/Logs/Athena/files.log"',
        f'exec "{mac.app}/venv/bin/python" -m pi_assistant.mac_files --folders-file "{mac.app}/folders"',
    ]
    assert mac.calls("uv") == [f"uv sync --project {REPO} --frozen --no-dev --extra mac --no-editable --quiet"]
    assert f"UV_PROJECT_ENVIRONMENT={mac.app}/venv" in mac.log.read_text()
    assert "Remote Login is off" in result.stdout and "iMCP isn't installed" in result.stdout


def test_the_key_can_only_reach_the_two_servers(mac):
    mac.install("--pi-key", KEY, "--folder", f"{mac.home}/Documents").check_returncode()

    assert mac.ssh_command("files").returncode == 1  # the nc stand-in finds nothing listening
    for attempt in ["sh -c id", "files; rm -rf ~", "", "../files"]:
        result = mac.ssh_command(attempt)
        assert result.returncode == 1 and "there's no server called" in result.stderr
    connections = [call for call in mac.calls("nc") if " -U " in call]
    assert connections == [f"nc -U {mac.app}/files.sock"]  # only the one for "files"


def test_rerunning_keeps_folders_and_a_new_key_replaces_the_old_one(mac):
    mac.keys.parent.mkdir(mode=0o700)
    mac.keys.write_text("ssh-ed25519 AAAAyourlaptop you@laptop\n")
    mac.install("--pi-key", KEY, "--folder", f"{mac.home}/Documents").check_returncode()

    mac.install().check_returncode()  # no options: same folders, same key
    assert (mac.app / "folders").read_text() == f"{mac.home}/Documents\n"
    assert mac.keys.read_text() == f"ssh-ed25519 AAAAyourlaptop you@laptop\n{our_line(mac)}\n"

    mac.install("--pi-key", OTHER_KEY).check_returncode()
    assert mac.keys.read_text() == f"ssh-ed25519 AAAAyourlaptop you@laptop\n{our_line(mac, OTHER_KEY)}\n"


@pytest.mark.parametrize(
    "key",
    [
        'ssh-ed25519 AAAAC3Nza" command="/bin/sh',  # would change the restrictions
        "ssh-ed25519 AAAAC3Nza\nssh-ed25519 AAAAsecond",  # would add a second, unrestricted key
        "no-pty ssh-ed25519 AAAAC3Nza",
        "not a key",
    ],
)
def test_refuses_anything_but_a_plain_public_key(mac, key):
    result = mac.install("--pi-key", key, "--folder", f"{mac.home}/Documents")
    assert result.returncode != 0 and "doesn't look like a public key" in result.stderr
    assert not mac.keys.exists()


def test_needs_a_key_and_real_folders(mac):
    result = mac.install("--folder", f"{mac.home}/Documents")
    assert result.returncode != 0 and "--pi-key" in result.stderr
    result = mac.install("--pi-key", KEY, "--folder", f"{mac.home}/Nowhere")
    assert result.returncode != 0 and "Not a folder" in result.stderr
    result = mac.install("--pi-key", KEY)
    assert result.returncode != 0 and "--folder" in result.stderr


def test_only_runs_on_a_mac(mac):
    result = mac.install("--pi-key", KEY, "--folder", f"{mac.home}/Documents", STUB_UNAME="Linux")
    assert result.returncode != 0 and "This is for the Mac" in result.stderr


def test_adding_a_server_is_one_line(mac, tmp_path):
    script = tmp_path / "repo" / "scripts" / "mac" / "install.sh"
    script.parent.mkdir(parents=True)
    original = (REPO / "scripts" / "mac" / "install.sh").read_text()
    script.write_text(original.replace('  "apple|', '  "notes|/opt/notes-mcp --read-only"\n  "apple|', 1))
    mac.install("--pi-key", KEY, "--folder", f"{mac.home}/Documents", script=script).check_returncode()

    assert (mac.app / "run-notes").read_text().splitlines()[-1] == "exec /opt/notes-mcp --read-only"
    assert plistlib.loads((mac.agents / "local.athena.notes.plist").read_bytes())["Label"] == "local.athena.notes"
    mac.ssh_command("notes")
    assert mac.calls("nc")[-1] == f"nc -U {mac.app}/notes.sock"
    assert "The servers are: files, notes, apple." in mac.ssh_command("other").stderr


def test_uninstall_removes_everything_it_added(mac):
    mac.keys.parent.mkdir(mode=0o700)
    mac.keys.write_text("ssh-ed25519 AAAAyourlaptop you@laptop\n")
    mac.install("--pi-key", KEY, "--folder", f"{mac.home}/Documents").check_returncode()

    mac.install("--uninstall").check_returncode()
    assert not mac.app.exists() and not list(mac.agents.iterdir())
    assert mac.keys.read_text() == "ssh-ed25519 AAAAyourlaptop you@laptop\n"
    assert f"launchctl bootout gui/{os.getuid()}/local.athena.files" in mac.calls("launchctl")


# -- the Pi's end: scripts/connect-mac.sh --------------------------------------------------


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_connect_mac_makes_a_key_and_an_ssh_entry(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    def connect(*args):
        return subprocess.run(
            ["bash", str(REPO / "scripts" / "connect-mac.sh"), *args],
            env={"HOME": str(home), "PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
        )

    first = connect("my-mac.tail1234.ts.net", "georges")
    assert first.returncode == 0, first.stderr
    key = home / ".ssh" / "athena_mac"
    public = (home / ".ssh" / "athena_mac.pub").read_text().strip()
    assert public.startswith("ssh-ed25519 ") and oct(key.stat().st_mode & 0o777) == "0o600"
    assert f'--pi-key "{public}"' in first.stdout

    connect("my-mac.tail1234.ts.net", "georges").check_returncode()  # safe to re-run
    config = (home / ".ssh" / "config").read_text()
    assert config.count("Host athena-mac") == 1
    for line in ["HostName my-mac.tail1234.ts.net", "User georges", "IdentityFile ~/.ssh/athena_mac", "BatchMode yes"]:
        assert f"  {line}\n" in config
    assert (home / ".ssh" / "athena_mac.pub").read_text().strip() == public  # the same key

    for bad in [("my mac", "georges"), ("my-mac\nHost *", "georges"), ("my-mac", "")]:
        assert connect(*bad).returncode == 2
