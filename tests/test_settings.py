"""What the dashboard's Tools tab changes: config.toml (keeping your comments) and .env (keeping your secrets)."""

import difflib
import json
import os
import stat
import tomllib
from pathlib import Path

import pytest
from dotenv import dotenv_values

from pi_assistant.config import NewsConfig, SQLiteConfig, Trading212Config, load_config
from pi_assistant.settings import (
    BY_ID,
    CATALOG,
    PROTECTED_SECRETS,
    EnvFile,
    Running,
    Settings,
    SettingsError,
)

REPO = Path(__file__).resolve().parents[1]
# The names of the keys these tests set. One a line, or gitleaks takes each name for the value of the one before.
SECRETS = [
    "GITHUB_TOKEN",
    "EMAIL_PASSWORD",
    "EDGAR_IDENTITY",
    "TRADING212_API_KEY",
    "TRADING212_API_SECRET",
]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """config.example.toml as someone's config.toml, an .env, and the settings that edit them."""
    for name in [*SECRETS, "WEATHER_API_KEY", "WEATHER_AUTHORIZATION"]:
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "config.toml"
    config.write_text((REPO / "config.example.toml").read_text())
    config.chmod(0o644)
    env = tmp_path / ".env"
    env.write_text("# Secrets\nTELEGRAM_BOT_TOKEN=123:abc\nGITHUB_TOKEN=\n")
    env.chmod(0o600)
    yield Settings(config, env), config, env
    for name in [*SECRETS, "WEATHER_API_KEY", "WEATHER_AUTHORIZATION"]:  # set by the code under test, not monkeypatch
        os.environ.pop(name, None)


def changed_lines(before: str, after: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


def entry(settings: Settings, name: str, running: Running | None = None) -> dict:
    return next(t for t in settings.describe(running or Running()) if t["id"] == name)


# -- the catalog ------------------------------------------------------------------------------------


def test_the_recommended_servers_are_the_ones_in_the_example_config():
    example = tomllib.loads((REPO / "config.example.toml").read_text())
    servers = [i for i in CATALOG if not i.builtin]
    assert [i.id for i in servers] == list(example["mcp_servers"])
    for integ in servers:
        assert integ.defaults == example["mcp_servers"][integ.id], integ.id
        for secret in integ.secrets:  # the config refers to each key where the catalog says
            node = integ.defaults
            for part in secret.path.split("."):
                node = node[part]
            assert node == secret.template and "${" + secret.name + "}" in node, integ.id
        for field in integ.fields:
            node = integ.defaults
            for part in field.key.split("."):
                node = node[part]
            assert isinstance(node, str), (integ.id, field.key)


def test_the_built_in_tools_settings_exist():
    for integ, model in [(BY_ID["news"], NewsConfig), (BY_ID["trading212"], Trading212Config)]:
        for field in integ.fields:
            assert field.key in model.model_fields
    assert BY_ID["databases"].section == "sqlite" and "databases" in SQLiteConfig.model_fields
    assert not {s.name for i in CATALOG for s in i.secrets} & PROTECTED_SECRETS


def test_everything_is_listed_without_secret_values(setup, monkeypatch):
    settings, _, _ = setup
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_supersecret")
    listed = settings.describe(Running())
    assert [t["id"] for t in listed] == [
        "news",
        "trading212",
        "databases",
        "time",
        "fetch",
        "search",
        "files",
        "apple",
        "github",
        "sec",
        "email",
    ]
    github = next(t for t in listed if t["id"] == "github")
    assert github["secrets"] == [
        {
            "name": "GITHUB_TOKEN",
            "label": "GitHub token",
            "help": github["secrets"][0]["help"],
            "required": True,
            "set": True,
        }
    ]
    assert (github["enabled"], github["state"], github["detail"]) == (False, "off", "Off")
    assert "supersecret" not in json.dumps(listed)


def test_shows_what_is_running(setup):
    settings, _, _ = setup
    running = Running(
        servers={
            "time": (True, None, [("get_current_time", "Get the time. In any zone."), ("convert_time", "")]),
            "fetch": (False, "timed out while connecting", []),
        },
        builtins={"news": (["read_news"], None), "trading212": ([], "TRADING212_API_KEY isn't set in .env")},
    )
    time_ = entry(settings, "time", running)
    assert (time_["state"], time_["detail"]) == ("on", "Connected · 2 tools")
    assert time_["tools"] == [
        {"name": "get_current_time", "description": "Get the time.", "on": True, "asks": False},
        {"name": "convert_time", "description": "", "on": True, "asks": False},
    ]
    assert (entry(settings, "fetch", running)["state"], entry(settings, "fetch", running)["detail"]) == (
        "error",
        "timed out while connecting",
    )
    assert entry(settings, "news", running)["detail"] == "1 tool"
    assert entry(settings, "trading212", running)["state"] == "off"  # switched off, so its problem doesn't matter
    # Switched on in config.toml since Athena started, and not applied yet.
    assert entry(settings, "databases", running)["detail"] == "No databases yet"
    assert entry(settings, "search", running)["detail"] == "Off"  # in config.toml, switched off
    settings.update("search", {"enabled": True})
    assert entry(settings, "search", running)["detail"] == "Not started yet: press Reconnect all"
    running.applying = True
    assert entry(settings, "search", running)["detail"] == "Starting…"
    running.builtins["news"] = ([], None)
    assert entry(settings, "news", running)["detail"] == "Starting…"


# -- switching on and off ---------------------------------------------------------------------------


def test_switching_a_server_on_changes_one_line_and_keeps_the_comments(setup):
    settings, config, _ = setup
    before = config.read_text()
    cfg = settings.update("files", {"enabled": True})
    assert changed_lines(before, config.read_text()) == ["-enabled = false", "+enabled = true"]
    assert cfg.mcp_servers["files"].enabled
    backup = config.parent / "config.toml.bak"
    assert backup.read_text() == before and stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.stat().st_mode) == 0o644  # as it was


def test_a_recommended_server_that_isnt_there_yet_is_added(setup, monkeypatch):
    settings, config, env = setup
    example = tomllib.loads(config.read_text())
    del example["mcp_servers"]["github"]
    config.write_text(config.read_text().split("# GitHub, read-only")[0])  # as if it was never copied in
    assert entry(settings, "github")["added"] is False

    cfg = settings.update("github", {"secrets": {"GITHUB_TOKEN": "  github_pat_123  "}, "enabled": True})
    assert cfg.mcp_servers["github"].enabled
    assert cfg.mcp_servers["github"].headers["Authorization"] == "Bearer github_pat_123"
    table = tomllib.loads(config.read_text())["mcp_servers"]["github"]
    assert table == {**BY_ID["github"].defaults, "enabled": True}  # as recommended, and switched on
    assert "# Reads public GitHub repositories" in config.read_text()
    assert dotenv_values(env)["GITHUB_TOKEN"] == "github_pat_123" and os.environ["GITHUB_TOKEN"] == "github_pat_123"
    assert "github_pat_123" not in config.read_text()  # the key is only in .env


def test_switching_on_needs_its_keys_and_settings_first(setup):
    settings, config, env = setup
    before, env_before = config.read_text(), env.read_text()
    with pytest.raises(SettingsError, match="Fill in “GitHub token” first"):
        settings.update("github", {"enabled": True})
    with pytest.raises(SettingsError, match="Fill in “App password” first"):
        settings.update("email", {"enabled": True})
    with pytest.raises(SettingsError, match="Fill in “Athena's address” first"):  # still the example address
        settings.update("email", {"enabled": True, "secrets": {"EMAIL_PASSWORD": "app-password"}})
    assert (config.read_text(), env.read_text()) == (before, env_before)  # nothing was written
    assert "EMAIL_PASSWORD" not in os.environ

    settings.update(
        "email",
        {
            "enabled": True,
            "secrets": {"EMAIL_PASSWORD": "app password with spaces"},
            "fields": {
                "env.MCP_EMAIL_SERVER_EMAIL_ADDRESS": "athena@fastmail.com",
                "env.MCP_EMAIL_SERVER_USER_NAME": "athena@fastmail.com",
                "env.MCP_EMAIL_SERVER_IMAP_HOST": "imap.fastmail.com",
                "env.MCP_EMAIL_SERVER_SMTP_HOST": "smtp.fastmail.com",
                "env.MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS": "me@example.org",
            },
        },
    )
    cfg = load_config(config)
    email = cfg.mcp_servers["email"]
    assert email.enabled and email.env["MCP_EMAIL_SERVER_PASSWORD"] == "app password with spaces"
    assert email.env["MCP_EMAIL_SERVER_ALLOWED_MUTATIONS"] == "send"  # untouched
    # Comments on the lines it changed are kept.
    assert 'MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS = "me@example.org"   # comma-separated' in config.read_text()


def test_a_built_in_tool_is_switched_on_and_set_up(setup, monkeypatch):
    settings, config, _ = setup
    with pytest.raises(SettingsError, match="Fill in “API key” first"):
        settings.update("trading212", {"enabled": True})
    cfg = settings.update(
        "trading212", {"enabled": True, "secrets": {"TRADING212_API_KEY": "key-1"}, "fields": {"environment": "demo"}}
    )
    assert (cfg.trading212.enabled, cfg.trading212.environment, cfg.trading212.api_key) == (True, "demo", "key-1")
    with pytest.raises(SettingsError, match="must be one of: live, demo"):
        settings.update("trading212", {"fields": {"environment": "paper"}})
    with pytest.raises(SettingsError, match="'api_key' isn't a setting"):
        settings.update("trading212", {"fields": {"api_key": "x"}})
    with pytest.raises(SettingsError, match="tools are fixed"):
        settings.update("trading212", {"tools": {}})

    cfg = settings.update("news", {"enabled": False})
    assert not cfg.news.enabled and cfg.news.feeds  # the feeds are kept for when it's back on
    assert "[news]\nenabled = false" in config.read_text()


def test_a_key_left_out_of_the_config_is_put_back(setup):
    settings, config, _ = setup
    config.write_text(config.read_text().replace('api_key = "${TRADING212_API_KEY}"\n', ""))
    cfg = settings.update("trading212", {"secrets": {"TRADING212_API_KEY": "key-2"}})
    assert cfg.trading212.api_key == "key-2"
    assert 'api_key = "${TRADING212_API_KEY}"' in config.read_text()


def test_athenas_own_secrets_stay_with_athena(setup):
    settings, config, env = setup
    with pytest.raises(SettingsError, match="can't be changed here"):
        settings.update("github", {"secrets": {"TELEGRAM_BOT_TOKEN": "x"}})
    with pytest.raises(SettingsError, match="isn't one of GitHub's keys"):
        settings.update("github", {"secrets": {"EMAIL_PASSWORD": "x"}})
    with pytest.raises(SettingsError, match="can't use TELEGRAM_BOT_TOKEN"):
        settings.add_server({"name": "sneaky", "command": "uvx", "env": {"TOKEN": "${TELEGRAM_BOT_TOKEN}"}})
    with pytest.raises(SettingsError, match="can't use DASHBOARD_TOKEN"):
        settings.add_server({"name": "sneaky", "url": "https://example.com/mcp?t=${DASHBOARD_TOKEN}"})
    with pytest.raises(SettingsError, match="TELEGRAM_BOT_TOKEN is Athena's own"):
        settings.add_server({"name": "telegram", "command": "uvx", "env": {"BOT_TOKEN": {"secret": "x"}}})
    assert dotenv_values(env)["TELEGRAM_BOT_TOKEN"] == "123:abc"
    assert "sneaky" not in config.read_text()


def test_removing_a_key(setup, monkeypatch):
    settings, _, env = setup
    settings.update("trading212", {"secrets": {"TRADING212_API_SECRET": "secret-1"}})
    assert dotenv_values(env)["TRADING212_API_SECRET"] == "secret-1"
    settings.update("trading212", {"secrets": {"TRADING212_API_SECRET": None}})
    assert dotenv_values(env)["TRADING212_API_SECRET"] == "" and "TRADING212_API_SECRET" not in os.environ
    assert entry(settings, "trading212")["secrets"][1]["set"] is False


# -- a server's tools ---------------------------------------------------------------------------------


def choose(**choices):
    return {name: {"on": on, "asks": asks} for name, (on, asks) in choices.items()}


def test_choosing_which_tools_athena_uses_and_which_ask(setup):
    settings, config, _ = setup
    offered = ["fetch_page", "fetch_raw", "post_form"]

    # Some run without asking: only the tools in use are included, so a new one can't slip in.
    cfg = settings.update(
        "fetch",
        {"tools": choose(fetch_page=(True, False), fetch_raw=(True, True), post_form=(False, False))},
        offered,
    )
    server = cfg.mcp_servers["fetch"]
    assert (server.include, server.exclude, server.confirm) == (["fetch_page", "fetch_raw"], [], ["fetch_raw"])

    # Everything asks again, and all are in use: back to the defaults.
    cfg = settings.update(
        "fetch", {"tools": choose(fetch_page=(True, True), fetch_raw=(True, True), post_form=(True, True))}, offered
    )
    server = cfg.mcp_servers["fetch"]
    assert (server.include, server.confirm) == ([], ["*"])
    assert "include" not in tomllib.loads(config.read_text())["mcp_servers"]["fetch"]

    with pytest.raises(SettingsError, match="at least one"):
        settings.update(
            "fetch",
            {"tools": choose(fetch_page=(False, True), fetch_raw=(False, True), post_form=(False, True))},
            offered,
        )
    with pytest.raises(SettingsError, match="each of its tools"):
        settings.update("fetch", {"tools": choose(fetch_page=(True, True))}, offered)
    with pytest.raises(SettingsError, match="aren't known until it's connected"):
        settings.update("fetch", {"tools": choose(fetch_page=(True, True))}, [])


# -- lists of names and values ------------------------------------------------------------------------


def test_news_feeds_change_one_by_one_and_keep_their_comments(setup):
    settings, config, _ = setup
    before = config.read_text()
    feeds = tomllib.loads(before)["news"]["feeds"]
    feeds.pop("Hacker News")
    feeds["Ars Technica"] = "https://feeds.arstechnica.com/arstechnica/index"
    cfg = settings.update("news", {"fields": {"feeds": feeds}})
    assert cfg.news.feeds == feeds
    assert changed_lines(before, config.read_text()) == [
        '-"Hacker News" = "https://hnrss.org/frontpage"',
        '+"Ars Technica" = "https://feeds.arstechnica.com/arstechnica/index"',
    ]
    with pytest.raises(SettingsError, match="needs a name"):
        settings.update("news", {"fields": {"feeds": {" ": "https://example.com/rss"}}})


def test_databases_can_be_listed_where_there_were_none(setup, tmp_path):
    settings, config, _ = setup
    cfg = settings.update("databases", {"fields": {"databases": {"budget": "budget.db"}}})
    assert cfg.sqlite.databases == {"budget": "budget.db"}
    assert "# SQLite databases the assistant can query" in config.read_text()


# -- servers of your own ------------------------------------------------------------------------------


def test_adding_changing_and_removing_a_server_of_your_own(setup, monkeypatch):
    settings, config, env = setup
    before = config.read_text()
    name, cfg = settings.add_server(
        {
            "name": "Weather",
            "command": "uvx",
            "args": ["weather-mcp==1.2.3", " "],
            "env": {"UNITS": "metric", "API_KEY": {"secret": "w-123"}},
        }
    )
    assert name == "weather"
    server = cfg.mcp_servers["weather"]
    assert (server.command, server.args, server.env) == (
        "uvx",
        ["weather-mcp==1.2.3"],
        {"UNITS": "metric", "API_KEY": "w-123"},
    )
    assert server.confirm == ["*"]  # every tool asks until you choose otherwise
    assert 'env = {UNITS = "metric", API_KEY = "${WEATHER_API_KEY}"}' in config.read_text()
    assert dotenv_values(env)["WEATHER_API_KEY"] == "w-123"

    listed = entry(settings, "weather")
    assert listed["custom"] and listed["summary"] == "uvx weather-mcp==1.2.3"
    assert listed["secrets"][0] | {"help": ""} == {
        "name": "WEATHER_API_KEY",
        "label": "API_KEY",
        "help": "",
        "required": True,
        "set": True,
        "where": "env",
    }

    # Replacing the secret keeps its name in .env; a new plain value goes in the config.
    cfg = settings.update(
        "weather", {"fields": {"env": {"UNITS": "imperial", "API_KEY": {"secret": "w-456"}, "REGION": "uk"}}}
    )
    assert cfg.mcp_servers["weather"].env == {"UNITS": "imperial", "API_KEY": "w-456", "REGION": "uk"}
    assert dotenv_values(env)["WEATHER_API_KEY"] == "w-456"

    with pytest.raises(SettingsError, match="already a tool called weather"):
        settings.add_server({"name": "weather", "url": "https://example.com/mcp"})
    with pytest.raises(SettingsError, match="already a tool called github"):
        settings.add_server({"name": "github", "url": "https://example.com/mcp"})
    with pytest.raises(SettingsError, match="either a command"):
        settings.add_server({"name": "both", "command": "uvx", "url": "https://example.com/mcp"})
    with pytest.raises(SettingsError, match="lower-case letters"):
        settings.add_server({"name": "../evil", "command": "uvx"})
    with pytest.raises(SettingsError, match="must start with https://"):
        settings.add_server({"name": "remote", "url": "file:///etc/passwd"})
    with pytest.raises(SettingsError, match="switched off, but not removed"):
        settings.remove_server("github")

    settings.remove_server("weather")
    assert config.read_text() == before


def test_a_server_at_a_url_keeps_its_authorization_in_env(setup):
    settings, config, env = setup
    _, cfg = settings.add_server(
        {
            "name": "weather",
            "url": "https://weather.example.com/mcp",
            "headers": {"Authorization": {"secret": "Bearer t"}},
        }
    )
    assert cfg.mcp_servers["weather"].headers == {"Authorization": "Bearer t"}
    assert dotenv_values(env)["WEATHER_AUTHORIZATION"] == "Bearer t"
    assert "Bearer t" not in config.read_text()


def test_removing_a_server_keeps_the_comment_above_the_next_one(setup):
    settings, config, _ = setup
    config.write_text(
        config.read_text()
        + '\n# My first server.\n[mcp_servers.one]\ncommand = "uvx"\nargs = ["one==1"]\n'
        + '\n# My second server: keep this.\n[mcp_servers.two]\ncommand = "uvx"\nargs = ["two==2"]\n'
    )
    settings.remove_server("one")
    text = config.read_text()
    assert "My first server" not in text and "[mcp_servers.one]" not in text
    assert '# My second server: keep this.\n[mcp_servers.two]\ncommand = "uvx"' in text
    assert load_config(config).mcp_servers["two"].args == ["two==2"]


def test_setting_values_changes_only_those_lines(setup):
    settings, config, _ = setup
    before = config.read_text()
    cfg = settings.set_values({"embeddings.model": "other-model", "memory.recall_max_distance": 0.4})
    assert (cfg.embeddings.model, cfg.memory.recall_max_distance) == ("other-model", 0.4)
    assert changed_lines(before, config.read_text()) == [
        '-model = "embeddinggemma-2:740m-bf16"',
        '+model = "other-model"',
        "-recall_max_distance = 0.31             # auto-recall leaves out memories further away than this "
        "(lower = stricter)",
        "+recall_max_distance = 0.4             # auto-recall leaves out memories further away than this "
        "(lower = stricter)",
    ]
    # A setting that isn't there yet goes at the end of its section, and a section that isn't there is added.
    config.write_text(
        before.replace(
            "duplicate_distance = 0.03              # a new fact closer than this to a saved one isn't saved again\n",
            "",
        )
    )
    settings.set_values({"memory.duplicate_distance": 0.05})
    raw = tomllib.loads(config.read_text())
    assert raw["memory"]["duplicate_distance"] == 0.05 and list(raw["memory"])[-1] == "duplicate_distance"
    config.write_text('[llm]\nmodel = "m"\n')
    settings.set_values({"memory.duplicate_distance": 0.05})
    assert tomllib.loads(config.read_text())["memory"] == {"duplicate_distance": 0.05}


def test_setting_a_value_that_breaks_the_config_writes_nothing(setup):
    settings, config, _ = setup
    before = config.read_text()
    with pytest.raises(SettingsError, match="invalid, so nothing was changed"):
        settings.set_values({"memory.recall_max_distance": "far"})
    assert config.read_text() == before


def test_a_change_that_would_break_the_config_writes_nothing(setup):
    settings, config, env = setup
    settings.add_server({"name": "weather", "command": "uvx", "args": ["w==1"]})
    before, env_before = config.read_text(), env.read_text()
    with pytest.raises(SettingsError, match="invalid, so nothing was changed") as raised:
        settings.update("weather", {"fields": {"command": ""}})  # neither a command nor a URL
    assert "exactly one of 'command'" in str(raised.value)
    assert (config.read_text(), env.read_text()) == (before, env_before)
    with pytest.raises(SettingsError, match="There's no tool called 'nope'"):
        settings.update("nope", {"enabled": True})
    with pytest.raises(SettingsError, match="Unknown change: colour"):
        settings.update("weather", {"colour": "red"})
    with pytest.raises(SettingsError, match="secrets must be names and values"):
        settings.update("weather", {"secrets": ["x"]})


def test_nothing_can_be_changed_without_a_config_file(tmp_path):
    settings = Settings(None, tmp_path / ".env")
    assert not settings.editable
    with pytest.raises(SettingsError, match="no config.toml"):
        settings.describe(Running())


# -- .env ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "line"),
    [
        ("plain-value_1.2:3/4+5=", "NAME=plain-value_1.2:3/4+5="),
        ("Jane Doe jane@example.com", "NAME='Jane Doe jane@example.com'"),
        ("has#hash and $dollar ${HOME}", "NAME='has#hash and $dollar ${HOME}'"),
        ('say "hi"', "NAME='say \"hi\"'"),
    ],
)
def test_env_values_are_written_so_systemd_and_dotenv_read_the_same(tmp_path, value, line, monkeypatch):
    monkeypatch.delenv("NAME", raising=False)
    path = tmp_path / ".env"
    path.write_text("# comment\nOTHER=1\nNAME=old\n")
    EnvFile(path).update({"NAME": value})
    assert path.read_text() == f"# comment\nOTHER=1\n{line}\n"
    assert dotenv_values(path, interpolate=False)["NAME"] == value  # as Athena reads it
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    os.environ.pop("NAME", None)


def test_env_values_that_cant_be_written_safely_are_refused(tmp_path):
    env = EnvFile(tmp_path / ".env")
    for value in ["two\nlines", 'it\'s "both" kinds']:
        with pytest.raises(SettingsError):
            env.update({"NAME": value})
    with pytest.raises(SettingsError):
        env.update({"BAD NAME": "x"})
    assert not (tmp_path / ".env").exists()
