"""The assistant's services changing while it runs: tools reloaded from config.toml, sessions and forgetting."""

import asyncio
import os

import pytest
from conftest import FakeEmbedder, FakeLLMServer, completion

from pi_assistant.app import build_services, reread_config
from pi_assistant.config import ConfigError, load_config
from pi_assistant.llm import LLMClient

CONFIG = """
[llm]
base_url = "http://127.0.0.1:9/v1"
model = "test-model"

[embeddings]
model = "fake-embed"
dimensions = 256

[display]
enabled = false

[dashboard]
enabled = false
"""


@pytest.fixture
async def services(tmp_path):
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / ".env").write_text("")
    cfg = load_config(tmp_path / "config.toml")
    services = build_services(cfg)
    services.memory.embedder = FakeEmbedder()
    services.agent.llm = FakeLLMServer(lambda body: completion("OK.")).client(cfg.llm)
    await services.start()
    try:
        yield services
    finally:
        await services.close()


async def test_reloading_applies_the_tools_settings_from_config_toml(services, monkeypatch):
    names = lambda: {t.name for t in services.tools.all()}  # noqa: E731
    assert names() == {"remember", "search_memory", "forget_memory"}
    path = services.config.path
    path.write_text(CONFIG + '\n[news.feeds]\n"BBC" = "https://feeds.bbci.co.uk/news/rss.xml"\n')
    services.rewarm.clear()
    await services.reload()
    assert "read_news" in names() and services.news is not None
    assert services.rewarm.is_set()  # the prompt has changed, so the model server reads it again

    # Keys added to .env since Athena started are used.
    monkeypatch.delenv("TRADING212_API_KEY", raising=False)
    path.write_text(CONFIG + '\n[trading212]\nenabled = true\napi_key = "${TRADING212_API_KEY}"\n')
    await services.reload()
    assert services.builtins.problems == {"trading212": "TRADING212_API_KEY isn't set in .env"}
    (path.parent / ".env").write_text("TRADING212_API_KEY=key-from-env\n")
    await services.reload()
    assert services.trading212 is not None and "trading212_portfolio" in names()
    os.environ.pop("TRADING212_API_KEY")

    path.write_text("this isn't [ toml")
    with pytest.raises(ConfigError):
        await services.reload()
    assert "trading212_portfolio" in names()  # nothing changed


def test_athenas_own_secrets_only_change_when_it_restarts(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(CONFIG + '\n[telegram]\nbot_token = "${TELEGRAM_BOT_TOKEN}"\n')
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:original")
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=999:changed\nOTHER_KEY=from-env\n")
    monkeypatch.setenv("OTHER_KEY", "old")
    cfg = reread_config(load_config(tmp_path / "config.toml"))
    assert cfg.telegram.bot_token == "123:original"
    assert os.environ["OTHER_KEY"] == "from-env"


async def test_each_reply_is_added_to_memory_in_the_background(services):
    await services.agent.respond("chat", "My locker number is 42")
    for _ in range(100):
        if services.memory.store.count().get("conversation"):
            break
        await asyncio.sleep(0.02)
    [entry] = services.memory.store.recent(kind="conversation")
    assert entry.text == "User: My locker number is 42\nAssistant: OK."
    assert services.rewarm.is_set()


async def test_a_new_session_and_forgetting_everything(services):
    await services.agent.respond("chat", "hello")
    await services.memory.remember("George's locker is number 42.")
    first = services.history.session

    session = services.new_session()
    assert session.id == first.id + 1 and services.history.load("chat") == []

    session = await services.forget_everything()
    assert session.id == first.id + 2
    assert services.memory.store.count() == {} and services.history.exchanges_after(0) == []
    assert services.stats.summary()["queries"] == 1  # the statistics stay
    await services.agent.respond("chat", "after")  # and it carries on
    assert services.history.load("chat")[-1]["content"] == "OK."


# -- the model's context window ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "model", "status", "expected"),
    [
        (64000, {"max_model_len": 262144}, None, 64000),  # your setting wins
        (0, {"max_model_len": 262144}, None, 262144),  # vLLM and newer oMLX
        (0, {}, {"models": [{"id": "test-model", "max_context_window": 32768, "model_context_length": 262144}]}, 32768),
        (0, {"context_length": 8192}, {"models": [{"id": "other", "max_context_window": 4096}]}, 8192),
        (0, {}, None, None),  # the server doesn't say
    ],
)
async def test_the_context_window_comes_from_the_setting_or_the_model_server(config, setting, model, status, expected):
    config.llm.context_window = setting
    llm: LLMClient = FakeLLMServer([], model=model, status=status).client(config.llm)
    assert await llm.context_window() == expected
    await llm.close()
