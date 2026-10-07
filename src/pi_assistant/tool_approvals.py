"""Which version of each MCP server's tools you've approved.

A tool's description and arguments go into every prompt, and a server can change them at
any time: a hosted one whenever its makers like, a local one when you update it. A changed
description can carry instructions to the model ("tool poisoning"), so Athena remembers what
each tool said when it was approved, and holds back any tool that's new or has changed since,
until you approve it on the dashboard.

A server's tools are approved as they are the first time it connects: you've just chosen it,
and can see them all on the dashboard.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pi_assistant.memory import connect_db


def fingerprint(description: str, parameters: dict[str, Any]) -> str:
    """A hash of what the model sees of a tool: its description and its arguments."""
    text = json.dumps({"description": description, "parameters": parameters}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class ToolVersion:
    description: str
    parameters: dict[str, Any]

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.description, self.parameters)


@dataclass
class ToolChange:
    """A tool that's held back: new, or different from the version you approved (``approved``)."""

    tool: str
    now: ToolVersion
    approved: ToolVersion | None = None


class ToolApprovals:
    def __init__(self, db_path: Path):
        self._conn = connect_db(db_path)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_tool_approvals (
                    server TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    description TEXT NOT NULL,
                    parameters TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    PRIMARY KEY (server, tool)
                )
                """
            )

    def knows(self, server: str) -> bool:
        with self._lock:
            return (
                self._conn.execute("SELECT 1 FROM mcp_tool_approvals WHERE server = ? LIMIT 1", (server,)).fetchone()
                is not None
            )

    def approved(self, server: str) -> dict[str, ToolVersion]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tool, description, parameters FROM mcp_tool_approvals WHERE server = ?", (server,)
            ).fetchall()
        return {tool: ToolVersion(description, json.loads(parameters)) for tool, description, parameters in rows}

    def approve(self, server: str, tools: dict[str, ToolVersion]) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO mcp_tool_approvals VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(server, tool) DO UPDATE SET "
                "fingerprint = excluded.fingerprint, description = excluded.description, "
                "parameters = excluded.parameters, approved_at = excluded.approved_at",
                [
                    (server, tool, v.fingerprint, v.description, json.dumps(v.parameters, sort_keys=True), now)
                    for tool, v in tools.items()
                ],
            )

    def forget(self, server: str) -> None:
        """For a server that's been removed: if one with its name is added later, it starts afresh."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM mcp_tool_approvals WHERE server = ?", (server,))

    def close(self) -> None:
        self._conn.close()
