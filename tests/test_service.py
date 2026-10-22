"""Athena as a systemd service (service.py): its units, filled in and installed only when they change, updating it,
and moving an Athena that ran from a git checkout into its home folder. Commands that would change the machine
are recorded instead of run."""

import re
import subprocess
from pathlib import Path

import pytest

from athena import service
from athena.resources import CONFIG_EXAMPLE, TELL_STOPPED


class Recorder:
    """subprocess.run, recording each command instead, and keeping what `sudo tee FILE` would write."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, args, input=None, **kwargs):
        self.calls.append(" ".join(str(a) for a in args))
        if args[:2] == ["sudo", "tee"]:
            Path(args[2]).write_text(input)
        if args[:3] == ["sudo", "rm", "-f"]:
            for path in args[3:]:
                Path(path).unlink(missing_ok=True)
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def pi(tmp_path, monkeypatch):
    """A home folder with Athena set up in ~/.athena, a place for units, and the athena command."""
    home = tmp_path / "home"
    athena_home = home / ".athena"
    athena_home.mkdir(parents=True)
    (athena_home / "config.toml").write_text(CONFIG_EXAMPLE.read_text())
    units = tmp_path / "units"
    units.mkdir()
    program = home / ".local" / "share" / "uv" / "tools" / "athena" / "bin" / "athena"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ATHENA_HOME", str(athena_home))
    monkeypatch.delenv("ATHENA_CONFIG", raising=False)
    monkeypatch.setenv("ATHENA_UNIT_DIR", str(units))
    monkeypatch.setattr(service, "program", lambda: program)
    monkeypatch.setattr(service.getpass, "getuser", lambda: "pi")
    return home, athena_home, units, program


def test_the_units_are_filled_in_for_this_pi(pi):
    home, athena_home, units, program = pi
    run = Recorder()
    assert service.install(run) == ["athena.service", "athena-stopped.service"]
    assert run.calls == [
        f"sudo tee {units}/athena.service",
        f"sudo tee {units}/athena-stopped.service",
        "sudo systemctl daemon-reload",
        "sudo systemctl enable athena",
    ]
    unit = (units / "athena.service").read_text()
    stopped = (units / "athena-stopped.service").read_text()
    for text in (unit, stopped):
        assert not re.search(r"^[^#].*@[A-Z_]+@", text, re.MULTILINE)  # every placeholder filled in
        assert "User=pi" in text and f"WorkingDirectory={athena_home}" in text
        assert f"Environment=ATHENA_HOME={athena_home}" in text
    assert f"ExecStart={program} run" in unit and "OnFailure=athena-stopped.service" in unit
    assert f"ExecStart=/usr/bin/python3 -I {TELL_STOPPED}" in stopped
    # It can write its own folder and uv's, but not its own code, which uv keeps in its tools folder.
    assert f'ReadWritePaths="{athena_home}"\n' in unit and "ProtectSystem=strict" in unit
    assert f"ReadOnlyPaths=-{home}/.local/share/uv/tools" in unit
    assert all((home / d).is_dir() for d in (".cache/uv", ".local/share/uv"))
    assert (athena_home / "data").is_dir()


def test_units_are_only_written_when_they_change(pi):
    _, _, units, _ = pi
    service.install(Recorder())
    run = Recorder()
    assert service.install(run) == [] and run.calls == ["sudo systemctl enable athena"]
    (units / "athena-stopped.service").write_text("changed by hand")
    run = Recorder()
    assert service.install(run) == ["athena-stopped.service"] and "sudo systemctl daemon-reload" in run.calls


def test_a_data_folder_elsewhere_is_writable_too(pi, tmp_path):
    _, athena_home, units, _ = pi
    drive = tmp_path / "the drive" / "athena data"
    config = athena_home / "config.toml"
    config.write_text(f'data_dir = "{drive}"\n' + config.read_text().replace('data_dir = "data"', ""))
    service.install(Recorder())
    assert f'ReadWritePaths="{athena_home}" "{drive}"' in (units / "athena.service").read_text()
    assert drive.is_dir()


def test_uninstalling_leaves_athenas_folder(pi):
    _, athena_home, units, _ = pi
    service.install(Recorder())
    run = Recorder()
    service.uninstall(run)
    assert run.calls[0] == "sudo systemctl disable --now athena"
    assert not list(units.iterdir()) and (athena_home / "config.toml").exists()


def test_updating(pi, monkeypatch):
    _, _, _, program = pi
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/bwrap")
    run = Recorder()
    service.update(run)
    assert run.calls == [
        "uv tool upgrade athena",
        f"{program} service install",  # the new version's, so its units come with it
        "sudo systemctl restart athena",
    ]


def test_updating_installs_the_sandbox_if_its_missing(pi, monkeypatch):
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    run = Recorder()
    service.update(run)
    assert run.calls[1] == "sudo apt-get install -y -qq bubblewrap"


# -- moving from a checkout ------------------------------------------------------------------------------


@pytest.fixture
def checkout(pi):
    """An Athena set up the old way: a git checkout in ~/athena, with its settings, data and service."""
    home, _, units, _ = pi
    repo = home / "athena"
    (repo / ".git").mkdir(parents=True)
    (repo / "config.toml").write_text(CONFIG_EXAMPLE.read_text())
    (repo / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
    (repo / "data" / "mcp" / "email").mkdir(parents=True)
    (repo / "data" / "assistant.db").write_text("memories")
    (units / "pi-assistant.service").write_text(f"[Service]\nWorkingDirectory={repo}\n")
    (units / "pi-assistant-stopped.service").write_text("")
    return repo


def test_an_athena_set_up_from_a_checkout_is_found(pi, checkout):
    _, _, units, _ = pi
    assert service.old_checkout() == checkout
    (units / "pi-assistant.service").unlink()
    assert service.old_checkout() == checkout  # ~/athena, with a .git
    (checkout / ".git").rmdir()
    assert service.old_checkout() is None


def test_moving_from_a_checkout(pi, checkout):
    _, athena_home, units, _ = pi
    (athena_home / "config.toml").unlink()  # setup moves it in before making one
    run = Recorder()
    done = service.move_from(checkout, run)
    assert run.calls[0] == "sudo systemctl disable --now pi-assistant.service pi-assistant-stopped.service"
    assert not (units / "pi-assistant.service").exists()
    assert (athena_home / ".env").read_text() == "TELEGRAM_BOT_TOKEN=123:abc\n"
    assert (athena_home / "data" / "assistant.db").read_text() == "memories"
    assert not (athena_home / "data" / "mcp").exists()  # made again: the old ones have the old path in them
    assert (athena_home / "config.toml").exists() and not (checkout / "config.toml").exists()
    assert (checkout / ".git").exists()  # the checkout itself is left for you to delete
    assert len(done) == 4


def test_moving_wont_overwrite_athenas_folder(pi, checkout):
    with pytest.raises(SystemExit, match="already has a config.toml"):
        service.move_from(checkout, Recorder())
    assert (checkout / "config.toml").exists()
