"""What a built-in integration is: tools Athena has without an MCP server, such as news feeds.

Each one is a class, in a module of its own, that says everything Athena needs to know about it:

  section, Settings   its section of config.toml, and the settings in it
  entry               how the dashboard's Tools tab shows it, and what it can change there
  wanted(cfg)         whether it's switched on, with something to use
  start(cfg)          make it, if it's wanted; raise Unavailable to say why it can't start
  tools(), close()    the tools it gives the model, and letting go of what it holds
  check(cfg, report)  doctor's check of it, if it's wanted

To add one, write its module and add the class to BUILTINS in integrations.py. config.toml then
has its section, the dashboard shows it, it's started and reloaded with the rest of the tools,
and doctor checks it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict

from pi_assistant.catalog import Integration
from pi_assistant.tools import Tool

if TYPE_CHECKING:
    from pi_assistant.config import Config

# How doctor marks each finding: it's fine, worth a look, or broken.
OK, WARN, FAIL = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m"
Report = Callable[[str, str], None]


class Section(BaseModel):
    """A section of config.toml. A setting it doesn't know is an error, so typos don't go unnoticed."""

    model_config = ConfigDict(extra="forbid")


class Unavailable(Exception):
    """A built-in integration that's switched on can't start. The message says why, for the dashboard."""


class Builtin(ABC):
    section: ClassVar[str]
    Settings: ClassVar[type[Section]]
    entry: ClassVar[Integration]

    @classmethod
    def settings(cls, cfg: Config) -> Any:
        return getattr(cfg, cls.section)

    @classmethod
    def wanted(cls, cfg: Config) -> bool:
        return bool(cls.settings(cfg).enabled)

    @classmethod
    @abstractmethod
    def start(cls, cfg: Config) -> Self:
        """Make it from the config. Only called if it's wanted."""

    @abstractmethod
    def tools(self) -> list[Tool]: ...

    async def close(self) -> None:  # noqa: B027 - most hold nothing to let go of
        pass

    @classmethod
    @abstractmethod
    async def check(cls, cfg: Config, report: Report) -> None:
        """Check it works, for doctor. Only called if it's wanted."""
