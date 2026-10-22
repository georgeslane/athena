"""The files that come with Athena, in resources/: what `athena setup` starts config.toml and .env from, the
system prompt, the reviewed locks of the recommended MCP servers, the systemd units, and the scripts the Pi
and the Mac run."""

from __future__ import annotations

from pathlib import Path

RESOURCES = Path(__file__).parent / "resources"
CONFIG_EXAMPLE = RESOURCES / "config.example.toml"
ENV_EXAMPLE = RESOURCES / "env.example"
SYSTEM_PROMPT = RESOURCES / "system.md"
MCP_LOCKS = RESOURCES / "mcp-locks"
UNITS = RESOURCES / "systemd"
SCRIPTS = RESOURCES / "scripts"
TELL_STOPPED = RESOURCES / "tell_stopped.py"
