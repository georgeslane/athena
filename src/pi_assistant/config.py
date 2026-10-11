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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_CONFIG_PATH = "config.toml"
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Raised when the config file is missing or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
    # At startup, after each reply, after /reload and then every this many minutes, have
    # the model server read the system prompt, tools and your chat, so its cache is ready
    # before your next message. When it's already cached this takes a moment. 0 turns it off.
    warm_up_minutes: float = 10.0
    # The most the model can read at once, in tokens, for the dashboard. 0: ask the model server.
    context_window: int = 0


class TelegramConfig(_Section):
    bot_token: str = ""
    # Only these Telegram user IDs can use the bot. Leave empty to run in setup
    # mode, where the bot replies to anyone with their user ID and nothing else.
    allowed_user_ids: list[int] = Field(default_factory=list)
    confirm_timeout_seconds: float = 300.0


class EmbeddingsConfig(_Section):
    base_url: str = "http://localhost:11434/v1"
    model: str = "embeddinggemma"
    api_key: str = "ollama"
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
    recall_max_distance: float = 0.55
    # and a new fact closer than this to a saved one is treated as the same fact.
    duplicate_distance: float = 0.04
    search_top_k: int = 8
    chunk_chars: int = 1200


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


class NewsConfig(_Section):
    # News feeds (RSS or Atom) the assistant can read, by name. Only these are ever
    # fetched, so reading them doesn't need your approval (see README, "News").
    enabled: bool = True
    feeds: dict[str, str] = Field(default_factory=dict)
    cache_minutes: float = 10.0


class Trading212Config(_Section):
    # Your Trading 212 Invest or Stocks ISA account (see README, "Trading 212"). Reading
    # it runs without asking; placing or cancelling an order always asks you first.
    enabled: bool = False
    api_key: str = ""
    api_secret: str = ""  # keys made before Trading 212 added secrets have none
    environment: Literal["live", "demo"] = "live"  # "demo" is the practice account, with its own key
    timeout_seconds: float = 15.0


class SQLiteConfig(_Section):
    # SQLite databases the assistant can query, by name (see README, "Databases"). Only these
    # files are ever opened, and only for reading.
    enabled: bool = True
    databases: dict[str, str] = Field(default_factory=dict)
    max_rows: int = 100
    timeout_seconds: float = 10.0  # a query running longer than this is stopped


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


class Config(_Section):
    data_dir: str = "data"
    llm: LLMConfig
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    siri: SiriConfig = Field(default_factory=SiriConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    trading212: Trading212Config = Field(default_factory=Trading212Config)
    sqlite: SQLiteConfig = Field(default_factory=SQLiteConfig)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)

    # Directory containing the config file; relative paths are resolved against it.
    base_dir: Path = Field(default_factory=Path.cwd, exclude=True)
    # The config file itself, which the dashboard edits. None if the config didn't come from a file.
    path: Path | None = Field(default=None, exclude=True)

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
