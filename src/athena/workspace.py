"""The workspace: data/workspace, the folder code runs in (run_code.py), where files you send Athena go,
under inbox/, and where it makes files (files.py).

Code in the sandbox can make anything there, including links to files outside it, such as Athena's .env,
and named pipes that never answer. Athena itself reads and writes there without a sandbox, so it only ever
opens what's there one folder at a time, from the workspace down (openat), refusing links at every step and
anything but an ordinary file at the end: nothing in the workspace can lead it outside, even if what's
there changes while it looks.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from athena.tools import ToolError

if TYPE_CHECKING:
    from athena.config import Config

INBOX = "inbox"  # where files you send Athena go
MAX_NAME = 120  # characters in a file's name


def workspace_of(cfg: Config) -> Path:
    """Where the workspace is: data/workspace."""
    return cfg.resolve(cfg.data_dir) / "workspace"


def clear_workspace(cfg: Config) -> None:
    """Delete everything in the workspace, as forgetting everything does."""
    shutil.rmtree(workspace_of(cfg), ignore_errors=True)  # it doesn't follow links: it removes them


def parts(name: str) -> tuple[str, ...]:
    """A path within the workspace, as its parts. ToolError if it isn't one: absolute, with .., or empty."""
    text = name.strip()
    while text.startswith("./"):
        text = text[2:]
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(p in ("..", ".") for p in path.parts):
        raise ToolError(f"{name!r} isn't a path in the workspace: give it relative to it, like reports/march.pdf.")
    if any("\x00" in p or len(p) > MAX_NAME for p in path.parts):
        raise ToolError(f"{name!r} isn't a name a file can have here.")
    return path.parts


@contextlib.contextmanager
def _folder(workspace: Path, names: tuple[str, ...], create: bool) -> Iterator[int]:
    """A descriptor of the folder ``names`` within the workspace, opened a step at a time without following
    links. ``create``: make any that aren't there."""
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in names:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o700, dir_fd=fd)
            try:
                inner = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                raise ToolError(f"There's no folder {'/'.join(names)} in the workspace.") from None
            except (NotADirectoryError, OSError):  # a file, or a link (ELOOP): not a folder to go into
                raise ToolError(f"{'/'.join(names)} isn't a folder in the workspace.") from None
            os.close(fd)
            fd = inner
        yield fd
    finally:
        os.close(fd)


def read(workspace: Path, name: str, limit: int) -> bytes:
    """An ordinary file's contents, up to ``limit`` bytes. ToolError if it isn't one, or is bigger."""
    *folders, file = parts(name)
    with _folder(workspace, tuple(folders), create=False) as at:
        try:
            # O_NONBLOCK: a named pipe opens straight away, rather than waiting for a writer, and is refused below.
            fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=at)
        except FileNotFoundError:
            raise ToolError(f"There's no file {name} in the workspace.") from None
        except OSError:
            raise ToolError(f"{name} isn't a file Athena can open: it's a link or something else.") from None
    with os.fdopen(fd, "rb") as file_:
        info = os.fstat(file_.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ToolError(f"{name} isn't an ordinary file.")
        if info.st_size > limit:
            raise ToolError(f"{name} is {info.st_size / 1024 / 1024:.1f} MB, more than the {limit // 1024 // 1024} MB "
                            "that can be read.")  # fmt: skip
        return file_.read(limit + 1)[:limit]


def write(workspace: Path, name: str, data: bytes, *, new: bool = False) -> str:
    """Write ``data`` to a file in the workspace, making its folders, replacing one that's there unless ``new``.
    Returns its path within the workspace. Only ever an ordinary file, never through a link."""
    *folders, file = parts(name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if new else os.O_TRUNC)
    with _folder(workspace, tuple(folders), create=True) as at:
        try:
            fd = os.open(file, flags | os.O_NONBLOCK, 0o600, dir_fd=at)
        except FileExistsError:
            raise
        except OSError:
            raise ToolError(f"{name} can't be written: there's a link or a folder of that name.") from None
    with os.fdopen(fd, "wb") as out:
        if not stat.S_ISREG(os.fstat(out.fileno()).st_mode):
            raise ToolError(f"{name} isn't an ordinary file.")
        out.write(data)
    return "/".join(parts(name))


def kind(workspace: Path, name: str) -> tuple[str, int] | None:
    """What's at a path in the workspace, without following links: ("file", its size), ("folder", 0), or
    ("other", 0) for a link and the like. None if there's nothing, or no such folder on the way."""
    *folders, last = parts(name)
    try:
        with _folder(workspace, tuple(folders), create=False) as at:
            info = os.stat(last, dir_fd=at, follow_symlinks=False)
    except (ToolError, FileNotFoundError):
        return None
    if stat.S_ISREG(info.st_mode):
        return "file", info.st_size
    return ("folder", 0) if stat.S_ISDIR(info.st_mode) else ("other", 0)


def remove(workspace: Path, name: str) -> None:
    """Remove a file or folder, with what's in it, from the workspace: a link itself, never what it leads to."""
    if (found := kind(workspace, name)) is None:
        return
    *folders, last = parts(name)
    with _folder(workspace, tuple(folders), create=False) as at:
        if found[0] == "folder":
            shutil.rmtree(last, dir_fd=at)  # which doesn't follow links within it either
        else:
            os.unlink(last, dir_fd=at)


def size(workspace: Path) -> int:
    """How much the workspace holds."""
    return sum(size for size, _ in files(workspace).values())


def make_folders(workspace: Path, name: str) -> None:
    """Make the folders a file at ``name`` would be in, never through a link."""
    *folders, _ = parts(name)
    with _folder(workspace, tuple(folders), create=True):
        pass


def save_new(workspace: Path, folder: str, name: str, data: bytes) -> str:
    """Save ``data`` as a new file in ``folder``, under ``name`` made safe, numbered if that's taken."""
    stem, dot, suffix = safe_name(name).rpartition(".")
    stem, suffix = (stem, f".{suffix}") if dot and stem else (suffix, "")
    for n in range(1, 1000):
        candidate = f"{folder}/{stem}{f' ({n})' if n > 1 else ''}{suffix}"
        try:
            return write(workspace, candidate, data, new=True)
        except FileExistsError:
            continue
    raise ToolError(f"There are too many files called {name} already.")


def safe_name(name: str) -> str:
    """A file's name as Athena keeps it: only its last part, without anything that could be read as a path."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip().lstrip(".")
    base = "".join(c if c.isprintable() and c not in '<>:"|?*' else "_" for c in base)[:MAX_NAME]
    return base or "file"


def listing(workspace: Path, name: str = "") -> list[tuple[str, int | None]]:
    """What's in a folder in the workspace: (name, size), with None for a folder, links and the like left out."""
    folders = parts(name) if name.strip(" ./") else ()
    found = []
    with _folder(workspace, folders, create=False) as at:
        for entry in sorted(os.listdir(at)):
            info = os.stat(entry, dir_fd=at, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                found.append((entry, None))
            elif stat.S_ISREG(info.st_mode):
                found.append((entry, info.st_size))
    return found


def files(folder: Path) -> dict[str, tuple[int, int]]:
    """Every file in ``folder``, by path within it: (size, when changed). Links are listed, never followed."""
    found = {}
    for root, dirs, names in os.walk(folder):  # os.walk doesn't follow links into folders
        for name in names:
            path = Path(root) / name
            with contextlib.suppress(OSError):
                info = path.lstat()
                found[str(path.relative_to(folder))] = (info.st_size, info.st_mtime_ns)
        dirs.sort()
    return found


def changes(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
    """What changed between two of ``files()``, as the model's told."""
    changed = [f"{name} ({show_size(after[name][0])}{', new' if name not in before else ''})"
               for name in sorted(after) if before.get(name) != after[name]]  # fmt: skip
    changed += [f"{name} (deleted)" for name in sorted(set(before) - set(after))]
    return changed


def show_size(size: int) -> str:
    for unit, scale in (("MB", 1024 * 1024), ("KB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} bytes"
