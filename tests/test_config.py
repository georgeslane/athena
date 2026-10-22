import asyncio
from pathlib import Path

import pytest
from conftest import DIMS

from athena.config import ConfigError, MCPServerConfig, load_config, unsendable
from athena.memory import Embedder
from athena.resources import CONFIG_EXAMPLE
from athena.search_eval import describe_embeddings_error

REPO = Path(__file__).resolve().parents[1]


def test_example_config_loads_with_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(CONFIG_EXAMPLE.read_text())
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=123:abc\nLLM_API_KEY=secret\nSIRI_TOKEN=for-the-shortcut\nDASHBOARD_TOKEN=open-sesame-123\n"
    )
    for name in ["TELEGRAM_BOT_TOKEN", "LLM_API_KEY", "SIRI_TOKEN", "DASHBOARD_TOKEN"]:
        monkeypatch.setenv(name, "")  # so they're put back afterwards
        monkeypatch.delenv(name)

    cfg = load_config(cfg_path)

    assert cfg.telegram.bot_token == "123:abc"
    assert cfg.llm.api_key == "secret"
    assert cfg.db_path == tmp_path / "data" / "assistant.db"
    servers = cfg.mcp_servers
    assert set(servers) == {"time", "fetch", "search", "apple", "github", "sec", "email"}
    # Only the two that need no setup start switched on.
    assert {name for name, server in servers.items() if server.enabled} == {"time", "fetch"}
    assert servers["time"].command == "uvx"
    # A fetched URL can carry data anywhere, so fetching asks first; reading the clock doesn't.
    assert servers["fetch"].confirm == ["*"]
    assert servers["time"].confirm == []
    # Anything that can change things or reach someone else asks first...
    assert servers["search"].confirm == ["*"]
    assert servers["email"].confirm == ["send_email"]
    assert servers["apple"].confirm == ["events_create", "reminders_create"]
    assert "events_delete" not in servers["apple"].include
    # ...and the email server itself only sends, and only to addresses you allow.
    assert servers["email"].env["MCP_EMAIL_SERVER_ALLOWED_MUTATIONS"] == "send"
    assert servers["email"].env["MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS"]
    assert servers["github"].url.endswith("/readonly")
    assert servers["github"].headers["X-MCP-Lockdown"] == "true"
    # Third-party servers are pinned to a version.
    for server in servers.values():
        if server.command == "uvx":
            assert any("==" in arg for arg in server.args), server.args
    assert "BBC News" in cfg.news.feeds
    # Trading 212 is off until you add a key, and trades always ask first (see test_trading212.py).
    assert not cfg.trading212.enabled and cfg.trading212.environment == "live"
    assert cfg.sqlite.databases == {}  # none until you list some
    assert cfg.llm.warm_up_minutes == 10
    assert cfg.agent.assistant_name == "Athena"
    # The status board's API only listens on the Pi, and the LED is set on the board's side now.
    assert cfg.display.enabled and (cfg.display.host, cfg.display.port) == ("127.0.0.1", 8091)
    assert cfg.display.show_task
    # Siri is off until you set it up, and only listens on this machine.
    assert not cfg.siri.enabled and cfg.siri.host == "127.0.0.1"
    assert cfg.siri.token == "for-the-shortcut"
    assert cfg.dashboard.token == "open-sesame-123"
    # The dashboard is on, only listens on this machine, and its password comes from .env.
    assert cfg.dashboard.enabled and (cfg.dashboard.host, cfg.dashboard.port) == ("127.0.0.1", 8092)
    assert cfg.llm.context_window == 0  # ask the model server
    assert cfg.news.enabled and cfg.sqlite.enabled
    assert cfg.path == cfg_path and cfg.env_path == tmp_path / ".env"


def test_missing_config_has_helpful_error(tmp_path):
    with pytest.raises(ConfigError, match="athena setup"):
        load_config(tmp_path / "nope.toml")


def test_unknown_keys_are_rejected(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[llm]\nmodel = "m"\nmodle_typo = 1\n')
    with pytest.raises(ConfigError, match="modle_typo"):
        load_config(p)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"command": "x", "url": "http://y"}],
)
def test_mcp_server_needs_exactly_one_transport(kwargs):
    with pytest.raises(ValueError):
        MCPServerConfig(**kwargs)


def test_mcp_tools_ask_first_unless_configured_otherwise():
    assert MCPServerConfig(command="x").confirm == ["*"]
    assert MCPServerConfig(command="x", confirm=[]).confirm == []


def test_mcp_http_transport_detection():
    assert MCPServerConfig(url="http://mac:8765/sse").http_transport == "sse"
    assert MCPServerConfig(url="http://mac:8765/mcp").http_transport == "http"
    assert MCPServerConfig(url="http://mac:8765/mcp", transport="sse").http_transport == "sse"


def test_problems_in_the_config_never_show_its_values(tmp_path, monkeypatch):
    # With ${NAME} filled in, a value can be a secret, and errors end up in logs and on the dashboard.
    monkeypatch.setenv("MY_SECRET", "hunter2-very-secret")
    p = tmp_path / "config.toml"
    p.write_text('[llm]\nmodel = "m"\ntemperature = "${MY_SECRET}"\n[mcp_servers.x]\nenv = { A = "${MY_SECRET}" }\n')
    with pytest.raises(ConfigError) as raised:
        load_config(p)
    message = str(raised.value)
    assert "llm.temperature: Input should be a valid number" in message
    assert "mcp_servers.x: Value error, set exactly one of 'command'" in message
    assert "hunter2" not in message and raised.value.__cause__ is None


def test_env_values_are_taken_as_they_are(tmp_path, monkeypatch):
    # As systemd reads them: a password with ${...} in it isn't filled in from elsewhere.
    monkeypatch.setenv("SIRI_TOKEN", "")
    monkeypatch.delenv("SIRI_TOKEN")
    p = tmp_path / "config.toml"
    p.write_text('[llm]\nmodel = "m"\n[siri]\ntoken = "${SIRI_TOKEN}"\n')
    (tmp_path / ".env").write_text("SIRI_TOKEN='abc${HOME}def'\n")
    assert load_config(p).siri.token == "abc${HOME}def"


def test_embeddings_on_the_models_server_use_its_key(tmp_path, monkeypatch, embeddings_server):
    """Whether its address is left empty or written out, as the old setup did, the model's server takes its key."""
    embeddings_server.key = "sk-mac"
    monkeypatch.setenv("LLM_API_KEY", "sk-mac")
    llm = f'[llm]\nbase_url = "{embeddings_server.url}"\nmodel = "m"\napi_key = "${{LLM_API_KEY}}"\n'
    path = tmp_path / "config.toml"

    def embed(embeddings: str) -> str:
        """ "ok", or what `athena doctor` would say is wrong."""
        path.write_text(f'{llm}[embeddings]\nmodel = "fake-a"\ndimensions = {DIMS}\n{embeddings}')
        cfg = load_config(path).embeddings

        async def run():
            embedder = Embedder(cfg)
            try:
                await embedder.embed_one("hello", "query")
                return "ok"
            except Exception as exc:
                return await describe_embeddings_error(exc, cfg)
            finally:
                await embedder.close()

        return asyncio.run(run())

    assert embed("") == "ok"
    assert embed(f'base_url = "{embeddings_server.url}/"\n') == "ok"
    assert embed(f'base_url = "{embeddings_server.url}"\napi_key = "wrong"\n') == (
        f"the embeddings server ({embeddings_server.url}) refused its API key. Leave [embeddings] base_url empty "
        "to use the model's server and key, or set its own api_key"
    )
    other = embeddings_server.url.replace("127.0.0.1", "localhost")
    path.write_text(f'{llm}[embeddings]\nbase_url = "{other}"\n')
    assert load_config(path).embeddings.api_key == ""  # another server isn't sent the model's key


def test_secrets_that_cant_be_sent():
    assert unsendable("0123abcd-_.~!:") is None
    assert unsendable("a" * 48 + "\u2588") == "\u2588"  # a terminal's cursor, copied with it
    assert unsendable("open sesame") == " "
