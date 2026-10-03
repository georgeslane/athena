from pathlib import Path

import pytest

from pi_assistant.config import ConfigError, MCPServerConfig, load_config

REPO = Path(__file__).resolve().parents[1]


def test_example_config_loads_with_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text((REPO / "config.example.toml").read_text())
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\nLLM_API_KEY=secret\n")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    cfg = load_config(cfg_path)

    assert cfg.telegram.bot_token == "123:abc"
    assert cfg.llm.api_key == "secret"
    assert cfg.db_path == tmp_path / "data" / "assistant.db"
    assert set(cfg.mcp_servers) == {"time", "fetch"}
    assert cfg.mcp_servers["time"].command == "uvx"
    # A fetched URL can carry data anywhere, so fetching asks first; reading the clock doesn't.
    assert cfg.mcp_servers["fetch"].confirm == ["*"]
    assert cfg.mcp_servers["time"].confirm == []
    assert cfg.agent.assistant_name == "Athena"
    assert cfg.display.show_task and cfg.display.led
    assert cfg.status_path == tmp_path / "data" / "status.json"


def test_missing_config_has_helpful_error(tmp_path):
    with pytest.raises(ConfigError, match="config.example.toml"):
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
