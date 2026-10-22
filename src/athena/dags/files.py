"""Files that come with an agent's guidance, such as your CV for an agent that finds you jobs.

They're kept in the workspace, in agents/<id>/, so the model reads them with read_file as it reads files you send
it in a chat, PDFs among them: the agent's chat and its runs start by saying they're there (Dags.intro). Deleting
the agent deletes them, and forgetting everything does, as it does the rest of the workspace.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from athena import workspace
from athena.tools import ToolError

log = logging.getLogger(__name__)

FOLDER = "agents"
MAX_FILES = 10  # with an agent's guidance


def folder(agent_id: int) -> str:
    return f"{FOLDER}/{agent_id}"


def listed(space: Path | None, agent_id: int) -> list[tuple[str, int]]:
    """An agent's files: (name, size), by name."""
    if space is None:
        return []
    try:
        return [(name, size) for name, size in workspace.listing(space, folder(agent_id)) if size is not None]
    except (OSError, ToolError):  # there's no folder: there are no files
        return []


def keep(space: Path, agent_id: int, name: str, data: bytes) -> str:
    """Keep a file with an agent's guidance: its path in the workspace. ToolError if it can't be."""
    if len(listed(space, agent_id)) >= MAX_FILES:
        raise ToolError(f"An agent's guidance can have up to {MAX_FILES} files: delete one first.")
    return workspace.save_new(space, folder(agent_id), name, data)


def path_of(space: Path, agent_id: int, name: str) -> str:
    """The path in the workspace of one of an agent's files. ToolError if it isn't one."""
    if name not in {listed_name for listed_name, _ in listed(space, agent_id)}:
        raise ToolError(f"This agent has no file called {name}.")
    return f"{folder(agent_id)}/{name}"


def remove(space: Path, agent_id: int, name: str) -> None:
    workspace.remove(space, path_of(space, agent_id, name))


def forget(space: Path | None, agent_id: int) -> None:
    """Delete all of an agent's files, as when it's deleted."""
    if space is not None:
        with contextlib.suppress(OSError, ToolError):
            workspace.remove(space, folder(agent_id))
