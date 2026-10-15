"""Configuration.

Settings live in a TOML file (``config.toml``, copied from ``config.example.toml``).
Secrets live in environment variables, normally loaded from a ``.env`` file next to
the config. Any string in the TOML can reference an environment variable as
``${NAME}``.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import Field, ValidationError, create_model, model_validator

from pi_assistant.builtin import Section
from pi_assistant.integrations import BUILTINS

DEFAULT_CONFIG_PATH = "config.toml"
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""


_Section = Section  # each section of config.toml
THINKING = ("on", "short", "off")  # how much a model that can think does, before answering


class LLMConfig(_Section):
    base_url: str = "http://localhost:8000/v1"
    model: str
    api_key: str = ""
    temperature: float = 0.7
    max_tokens: int = 2048
    timeout_seconds: float = 300.0
    # Passed through untouched in the request body, for server-specific options
    # such as {"chat_template_kwargs": {"enable_thinking": false}}.
    extra_body: dict[str, Any] = Field(default_factory=dict)
    # Whether a model that can think does so before answering: "on", "off", or "short", which
    # stops it after short_thinking_tokens (oMLX's thinking_budget). /think in Telegram and the
    # dashboard change it. Unset, it's as the model server, or extra_body, has it.
    thinking: Literal["on", "short", "off"] | None = None
    short_thinking_tokens: int = Field(default=1000, ge=1)
    # At startup, after each reply, after /reload and then every this many minutes, have
    # the model server read the system prompt, tools and your chat, so its cache is ready
    # before your next message. When it's already cached this takes a moment. 0 turns it off.
    warm_up_minutes: float = 10.0
    # The most the model can read at once, in tokens, for the dashboard. 0: ask the model server.
    context_window: int = 0

    @property
    def thinking_now(self) -> Literal["on", "short", "off"]:
        """Thinking as requests have it: as set, else as extra_body has it, else on (models think if they can)."""
        if self.thinking:
            return self.thinking
        kwargs = self.extra_body.get("chat_template_kwargs") or {}
        if False in (kwargs.get("enable_thinking"), self.extra_body.get("enable_thinking")):
            return "off"
        return "short" if self.extra_body.get("thinking_budget") else "on"


class TelegramConfig(_Section):
    bot_token: str = ""
    # Only these Telegram user IDs can use the bot. Leave empty to run in setup
    # mode, where the bot replies to anyone with their user ID and nothing else.
    allowed_user_ids: list[int] = Field(default_factory=list)
    confirm_timeout_seconds: float = 300.0


class EmbeddingsConfig(_Section):
    # Empty: the same server as the model, with its key ([llm] base_url and api_key), such as oMLX.
    base_url: str = ""
    model: str = "embeddinggemma-2-bf16"
    api_key: str = ""
    dimensions: int = 768
    # EmbeddingGemma expects task prefixes; other models may want "" for both.
    query_prefix: str = "task: search result | query: "
    document_prefix: str = "title: {title} | text: "
    batch_size: int = 16


class MemoryConfig(_Section):
    # Automatically look up memories related to each message and show them to the model.
    auto_recall: bool = True
    recall_top_k: int = 4
    # Cosine distances (0 = identical, 2 = opposite). Both depend on the embeddings model:
    # `pi-assistant embeddings test` measures them. Recalled memories further away than this are dropped,
    recall_max_distance: float = 0.31
    # and a new fact closer than this to a saved one is treated as the same fact.
    duplicate_distance: float = 0.03
    search_top_k: int = 8
    chunk_chars: int = 1200


class DocumentsConfig(_Section):
    # The Mac your documents are on, and its documents server (scripts/mac/install.sh --documents),
    # started over SSH. scripts/connect-mac.sh ADDRESS USER laptop makes the "athena-laptop" entry.
    command: str = "ssh"
    args: list[str] = Field(default_factory=lambda: ["athena-laptop", "documents"])
    # Folders on that Mac to index, chosen on the dashboard (Memory). Indexed only when you ask.
    folders: list[str] = Field(default_factory=list)
    max_chars_per_file: int = 300_000  # longer documents are indexed up to this many characters
    timeout_seconds: float = 300


class AgentConfig(_Section):
    assistant_name: str = "Athena"
    user_name: str = "the user"
    timezone: str = "UTC"
    system_prompt_file: str = "prompts/system.md"
    max_tool_rounds: int = 8
    tool_timeout_seconds: float = 120.0
    max_tool_result_chars: int = 6000
    # When the conversation grows past this many messages, the oldest half is
    # dropped in one go (rather than one message per turn) so the model server's
    # prompt cache stays valid most of the time.
    max_history_messages: int = 40


class DisplayConfig(_Section):
    # What Athena tells the status board, which is its own service (pi-display-microservice) and
    # asks for it over HTTP (see README, "Status board").
    enabled: bool = True
    host: str = "127.0.0.1"  # only programs on this Pi can ask; anywhere else needs a token
    port: int = 8091
    token: str = ""  # if set, the board must send it
    show_task: bool = True  # include the start of your message; False: just what Athena is doing
    # No longer used here: the LED is now set in pi-display-microservice's own config.toml.
    led: bool | None = None


class DashboardConfig(_Section):
    # A web page for Athena's status, usage and tools, served by the bot (see README, "Dashboard").
    enabled: bool = True
    # Tailscale Serve passes requests from your own devices on to this address.
    host: str = "127.0.0.1"
    port: int = 8092
    # Its password. Athena makes one in .env, as DASHBOARD_TOKEN, if there isn't one.
    token: str = Field(default_factory=lambda: os.environ.get("DASHBOARD_TOKEN", ""))


class TasksConfig(_Section):
    # Tasks Athena does by itself, on a schedule, and tells you about (see README, "Tasks").
    enabled: bool = True
    quiet_minutes: float = 5.0  # a task waits until you haven't messaged Athena for this long
    max_minutes: float = 30.0  # the most a run works for, not counting time it waits for you
    max_tool_rounds: int = 25
    thinking: Literal["on", "short", "off"] | None = "on"  # tasks aren't in a hurry: None follows [llm]
    confirm_minutes: float = 60.0  # how long a task waits for you to allow a tool that asks first


class SiriConfig(_Section):
    # Lets an Apple Shortcut ask the assistant, so you can ask by voice (see README, "Siri").
    enabled: bool = False
    # Tailscale Serve passes requests from your own devices on to this address.
    host: str = "127.0.0.1"
    port: int = 8090
    token: str = ""  # the shortcut sends this to show the request is yours
    # Siri gives up after about 25 seconds. Slower answers go to Telegram instead.
    reply_timeout_seconds: float = 20.0


class MCPServerConfig(_Section):
    enabled: bool = True
    # Local server, started as a subprocess and spoken to over stdio.
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    # Remote server, reached over HTTP.
    url: str | None = None
    transport: Literal["http", "sse"] | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    # Glob patterns matched against the server's own tool names.
    include: list[str] = Field(default_factory=list)  # empty = all tools
    exclude: list[str] = Field(default_factory=list)
    # Ask the user before running these. Every tool by default, since any tool might send
    # data off the local network; set [] only for servers that can't (e.g. local, read-only).
    confirm: list[str] = Field(default_factory=lambda: ["*"])
    timeout_seconds: float = 60.0
    # Local servers run in a sandbox (see sandbox.py): without your home folder, and without the
    # network unless `network`. `read_only_paths` are folders or files it may read as well.
    sandbox: bool = True
    network: bool = True
    read_only_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_transport(self) -> MCPServerConfig:
        if bool(self.command) == bool(self.url):
            raise ValueError("set exactly one of 'command' (local stdio server) or 'url' (HTTP server)")
        return self

    @property
    def http_transport(self) -> Literal["http", "sse"]:
        if self.transport:
            return self.transport
        return "sse" if (self.url or "").rstrip("/").endswith("/sse") else "http"


class _Config(_Section):
    data_dir: str = "data"
    llm: LLMConfig
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    documents: DocumentsConfig = Field(default_factory=DocumentsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    siri: SiriConfig = Field(default_factory=SiriConfig)
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)

    # Directory containing the config file; relative paths are resolved against it.
    base_dir: Path = Field(default_factory=Path.cwd, exclude=True)
    # The config file itself, which the dashboard edits. None if the config didn't come from a file.
    path: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _embeddings_from_the_model_server(self) -> _Config:
        """With no base_url of their own, embeddings come from the model's server, with the model's key."""
        if not self.embeddings.base_url:
            self.embeddings.base_url = self.llm.base_url
            self.embeddings.api_key = self.embeddings.api_key or self.llm.api_key
        return self

    def resolve(self, path: str | Path) -> Path:
        p = Path(path).expanduser()
        return p if p.is_absolute() else self.base_dir / p

    @property
    def env_path(self) -> Path:
        """The .env file next to the config, where secrets live."""
        return self.base_dir / ".env"

    @property
    def db_path(self) -> Path:
        return self.resolve(self.data_dir) / "assistant.db"

    @property
    def log_dir(self) -> Path:
        return self.resolve(self.data_dir) / "logs"


# Athena's settings: the sections above, and one for each built-in integration (integrations.py).
Config = create_model(
    "Config",
    __base__=_Config,
    __module__=__name__,
    **{b.section: (b.Settings, Field(default_factory=b.Settings)) for b in BUILTINS},  # type: ignore[call-overload]
)


def expand_env(value: Any) -> Any:
    """Replace ``${NAME}`` references with environment variables (missing ones become "")."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path or os.environ.get("PI_ASSISTANT_CONFIG") or DEFAULT_CONFIG_PATH).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}\nCopy config.example.toml to config.toml and edit it.")
    # Secrets: .env next to the config. Real environment variables take precedence. Values are
    # taken as they are, without filling in ${NAME}s, as systemd's EnvironmentFile does.
    load_dotenv(path.parent / ".env", override=False, interpolate=False)
    return parse_config(path.read_text(), path)


def parse_config(text: str, path: Path) -> Config:
    """The config in ``text``, as if it were the file at ``path``. Raises ConfigError if it's invalid."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    try:
        return Config.model_validate({**expand_env(raw), "base_dir": path.parent, "path": path})
    except ValidationError as exc:
        # Where and what, but not the values: with ${NAME} filled in, they can be secrets.
        problems = [
            f"  {'.'.join(str(part) for part in error['loc']) or '(top level)'}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        ]
        raise ConfigError(f"Problem in {path}:\n" + "\n".join(problems)) from None
