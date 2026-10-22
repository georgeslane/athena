"""Running Athena as a systemd service on Linux: installing its units, updating it, and moving an Athena that
was set up in a git checkout into its home folder.

  athena service install    writes /etc/systemd/system/athena.service and athena-stopped.service from
                            resources/systemd/, filled in for this user, this home folder and this install,
                            and has systemd start Athena when the Pi does. Only units that changed are written.
  athena service uninstall  stops Athena and removes its units. Its home folder is left as it is.
  athena update             updates Athena with uv, then its units, and restarts it.

Changes outside your home folder go through sudo, which asks for your password, so you see each one.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

from athena.config import athena_home, find_config
from athena.resources import TELL_STOPPED, UNITS

UNIT_NAMES = ("athena.service", "athena-stopped.service")
OLD_UNITS = ("pi-assistant.service", "pi-assistant-stopped.service")  # from when Athena ran from a checkout


def unit_dir() -> Path:
    return Path(os.environ.get("ATHENA_UNIT_DIR") or "/etc/systemd/system")


def program() -> Path:
    """The `athena` command that's running now, which the service runs too."""
    here = Path(sys.argv[0])
    if here.name == "athena" and here.exists():
        return here.resolve()
    found = shutil.which("athena")
    if not found:
        raise SystemExit("Can't find the athena command: install Athena with uv first (see docs/install.md).")
    return Path(found).resolve()


def render(unit: str, *, home: Path, data_dir: Path, athena: Path, user: str | None = None) -> str:
    """A unit from resources/systemd/, filled in."""
    writable = [home] + ([data_dir] if home not in (data_dir, *data_dir.parents) else [])
    values = {
        "@USER@": user or getpass.getuser(),
        "@HOME@": str(Path.home()),
        "@ATHENA_HOME@": str(home),
        "@ATHENA@": str(athena),
        "@TELL_STOPPED@": str(TELL_STOPPED),
        "@WRITABLE@": " ".join(f'"{path}"' for path in writable),
    }
    text = (UNITS / unit).read_text()
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text


def install(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run, config: str | Path | None = None
) -> list[str]:
    """Install the units that have changed, and enable Athena. Returns the units written."""
    path = find_config(config)
    home, data_dir = path.parent, data_dir_of(path)
    data_dir.mkdir(parents=True, exist_ok=True)
    for cache in (Path.home() / ".cache" / "uv", Path.home() / ".local" / "share" / "uv"):
        cache.mkdir(parents=True, exist_ok=True)  # so systemd can let Athena write there
    written = []
    for unit in UNIT_NAMES:
        wanted = render(unit, home=home, data_dir=data_dir.resolve(), athena=program())
        target = unit_dir() / unit
        if not target.exists() or target.read_text() != wanted:
            run(["sudo", "tee", str(target)], input=wanted, text=True, stdout=subprocess.DEVNULL, check=True)
            written.append(unit)
    if written:
        run(["sudo", "systemctl", "daemon-reload"], check=True)
    run(["sudo", "systemctl", "enable", "athena"], check=True, capture_output=True)
    return written


def data_dir_of(path: Path) -> Path:
    """Where Athena keeps its data, from config.toml's data_dir: read by itself, so the service can be installed
    while the rest of the config isn't right yet."""
    try:
        data_dir = str(tomllib.loads(path.read_text()).get("data_dir") or "data")
    except FileNotFoundError:
        raise SystemExit(f"There's no config file at {path}: run `athena setup` to make one.") from None
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{path} isn't valid TOML: {exc}") from None
    folder = Path(data_dir).expanduser()
    return (folder if folder.is_absolute() else path.parent / folder).resolve()


def uninstall(run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
    run(["sudo", "systemctl", "disable", "--now", "athena"], check=False)
    run(["sudo", "rm", "-f", *(str(unit_dir() / unit) for unit in UNIT_NAMES)], check=True)
    run(["sudo", "systemctl", "daemon-reload"], check=True)


def ensure_bubblewrap(run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
    """Install bubblewrap, which local MCP servers and code run in, if it isn't there."""
    if shutil.which("bwrap"):
        return
    run(["sudo", "apt-get", "install", "-y", "-qq", "bubblewrap"], check=True)


def update(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run, config: str | Path | None = None
) -> None:
    """Update Athena from where it was installed, then its units, and restart it."""
    run(["uv", "tool", "upgrade", "athena"], check=True)
    ensure_bubblewrap(run)
    # The new version's own `athena service install`, so new or changed units come with it.
    run([str(program()), *(["--config", str(config)] if config else []), "service", "install"], check=True)
    run(["sudo", "systemctl", "restart", "athena"], check=True)


# -- moving from a checkout ------------------------------------------------------------------------------


def old_checkout() -> Path | None:
    """The folder an Athena set up from a git checkout ran in, if there's one: from its old unit, or ~/athena."""
    unit = unit_dir() / OLD_UNITS[0]
    if unit.exists():
        for line in unit.read_text().splitlines():
            if line.startswith("WorkingDirectory="):
                folder = Path(line.partition("=")[2].strip())
                if (folder / "config.toml").exists():
                    return folder
    folder = Path.home() / "athena"
    return folder if (folder / "config.toml").exists() and (folder / ".git").exists() else None


def move_from(checkout: Path, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> list[str]:
    """Move config.toml, .env and the data folder from a checkout into Athena's home folder, and retire the old
    units. The checkout itself is left: delete it once Athena's running. Returns what was done, to show."""
    home = athena_home()
    if (home / "config.toml").exists():
        raise SystemExit(f"{home} already has a config.toml: move what you need from {checkout} yourself.")
    done = []
    if (unit_dir() / OLD_UNITS[0]).exists():
        run(["sudo", "systemctl", "disable", "--now", *OLD_UNITS], check=False)
        run(["sudo", "rm", "-f", *(str(unit_dir() / unit) for unit in OLD_UNITS)], check=True)
        run(["sudo", "systemctl", "daemon-reload"], check=True)
        done.append("stopped and removed the pi-assistant service")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("config.toml", ".env"):
        if (checkout / name).exists():
            shutil.move(checkout / name, home / name)
            done.append(f"moved {name}")
    data = checkout / "data"
    if data.is_dir() and not (home / "data").exists():
        shutil.move(data, home / "data")
        # The MCP servers' environments have their old path inside them: they're made again on first use.
        shutil.rmtree(home / "data" / "mcp", ignore_errors=True)
        done.append("moved data/ (the MCP servers' environments will be made again)")
    return done
