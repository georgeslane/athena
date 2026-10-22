"""The assistant's services changing while it runs: tools reloaded from config.toml, sessions and forgetting."""

import asyncio
import logging
import os

import pytest
from conftest import FakeEmbedder, FakeLLMServer, completion

from athena.app import build_services, reread_config
from athena.config import ConfigError, load_config
from athena.history import Busy
from athena.llm import LLMClient

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

[code]
enabled = false  # it's there only on Linux: off, so these tests count the same tools everywhere
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
    memory_and_tasks = {"remember", "search_memory", "read_document", "forget_memory"}
    tasks_and_skills = {
        "task_result",
        "change_dag",
        "run_dag",
        "agent_runs",
        "read_skill",
        "save_skill",
        "recent_work",
    }
    files = {"read_file", "write_file", "send_file"}
    assert names() == memory_and_tasks | tasks_and_skills | files | {"find_mcp_servers"}
    path = services.config.path
    path.write_text(CONFIG + '\n[news.feeds]\n"BBC" = "https://feeds.bbci.co.uk/news/rss.xml"\n')
    services.rewarm.clear()
    await services.reload()
    assert "read_news" in names() and services.builtins.get("news") is not None
    assert services.rewarm.is_set()  # the prompt has changed, so the model server reads it again

    # Keys added to .env since Athena started are used.
    monkeypatch.delenv("TRADING212_API_KEY", raising=False)
    path.write_text(CONFIG + '\n[trading212]\nenabled = true\napi_key = "${TRADING212_API_KEY}"\n')
    await services.reload()
    assert services.builtins.problems == {"trading212": "TRADING212_API_KEY isn't set in .env"}
    (path.parent / ".env").write_text("TRADING212_API_KEY=key-from-env\n")
    await services.reload()
    assert services.builtins.get("trading212") is not None and "trading212_portfolio" in names()
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


async def test_a_message_with_nowhere_to_go_isnt_logged_either(services, caplog):
    caplog.set_level(logging.INFO)
    services.channel = None
    assert await services.notify("Your locker number is 42") is False
    assert "Nowhere to send a message of 24 characters" in caplog.text and "locker" not in caplog.text


async def test_forgetting_everything(services):
    master = services.history.master
    await services.agent.respond(master.chat_id, "hello")
    await services.memory.remember("George's locker is number 42.")

    session = await services.forget_everything()
    assert session.id == master.id  # the master agent stays
    assert services.memory.store.count() == {} and services.history.exchanges_after(0) == []
    assert services.stats.summary()["queries"] == 1  # the statistics stay
    await services.agent.respond(session.chat_id, "after")  # and it carries on
    assert services.history.load(session.chat_id)[-1]["content"] == "OK."


async def test_an_agent_cant_be_deleted_while_athena_answers_you_in_it_nor_the_master(services):
    jobs = services.history.new_session("Jobs")
    services.chat.replying = type("Reply", (), {"session": jobs.id})()  # as while a message to it is answered
    with pytest.raises(Busy, match="answering you in that agent"):
        await services.delete_session(jobs.id)
    services.chat.replying = None
    with pytest.raises(ValueError, match="can't be deleted"):
        await services.delete_session(services.history.master.id)
    with pytest.raises(KeyError):
        await services.delete_session(99)
    await services.delete_session(jobs.id)
    assert services.history.get_session(jobs.id) is None


async def test_what_athena_tells_you_by_itself_goes_into_the_master_agents_conversation(services):
    services.history.new_session("Jobs")
    services.keep("(Claude's projects changed.)", "All quiet.")
    [message, reply], _ = services.history.messages(services.history.master.id)
    assert (message.text, message.channel, reply.text) == ("(Claude's projects changed.)", "Athena", "All quiet.")


async def test_forgetting_everything_forgets_what_runs_found_but_keeps_the_dags(services):
    dags = services.dags
    source = "with DAG(schedule='30 7 * * *'):\n    inbox = Task(prompt='Check my inbox.')\n"
    dags.save(1, source)
    dags.store.update(1, context="The bank wrote on Monday.")
    run = dags.store.start_run(1, "manual", "", source)
    dags.store.finish_task(dags.store.add_task(run.id, "inbox", 0, "running"), "success", "The bank wrote.", [])
    dags.store.finish_run(run, "success", "The bank wrote.")

    await services.forget_everything()
    assert dags.store.get(1).dag == source and dags.store.get(1).context == ""
    assert dags.store.runs(1) == []


async def test_the_prompt_says_what_mcp_servers_are_where_their_config_does(tmp_path):
    import sys
    from pathlib import Path

    demo = Path(__file__).parent / "fixtures" / "demo_mcp_server.py"
    server = f'[mcp_servers.demo]\ncommand = "{sys.executable}"\nargs = ["{demo}"]\ninclude = ["add"]\nconfirm = []\n'
    (tmp_path / "config.toml").write_text(CONFIG + server + 'about = "Sums for the user, done properly."\n')
    (tmp_path / ".env").write_text("")
    services = build_services(load_config(tmp_path / "config.toml"))
    services.memory.embedder = FakeEmbedder()
    services.mcp.sandbox = None  # the demo server lives in the repo; the sandbox has tests of its own
    await services.start()
    try:
        assert (
            "## Where some of your tools come from\n- add: Sums for the user, done properly." in services.agent.prompt()
        )
    finally:
        await services.close()


async def test_thinking_is_changed_and_kept_in_config_toml(services):
    assert services.config.llm.thinking_now == "on"
    assert services.set_thinking("off") is True
    assert services.config.llm.thinking == "off"
    # Added to [llm], with the rest as it was.
    added = 'model = "test-model"\nthinking = "off"\n'
    assert services.config.path.read_text() == CONFIG.replace('model = "test-model"\n', added)
    assert load_config(services.config.path).llm.thinking == "off"  # after a restart too
    services.set_thinking("short")
    assert load_config(services.config.path).llm.thinking == "short"
    with pytest.raises(ValueError, match="on, short, off"):
        services.set_thinking("lots")
    assert services.config.llm.thinking == "short"


async def test_thinking_without_a_config_toml_lasts_until_a_restart(config):
    services = build_services(config)
    try:
        assert services.set_thinking("off") is False and config.llm.thinking == "off"
    finally:
        await services.close()


async def test_taking_back_the_last_reply_takes_it_out_of_memory_too(services):
    await services.agent.respond("chat", "My locker number is 42")
    await services.agent.respond("chat", "And my bike lock code is 1234")
    for _ in range(100):
        if services.memory.store.count().get("conversation") == 2:
            break
        await asyncio.sleep(0.02)

    taken = await services.take_back_last_exchange("chat")
    assert taken.question == "And my bike lock code is 1234"
    assert [e.text for e in services.memory.store.recent(kind="conversation")] == [
        "User: My locker number is 42\nAssistant: OK."
    ]
    assert services.history.load("chat")[-1]["content"] == "OK." and len(services.history.load("chat")) == 2

    # Straight after a reply, before it's been added to memory: it isn't added afterwards.
    await services.agent.respond("chat", "My bike is blue")
    await services.take_back_last_exchange("chat")
    await asyncio.sleep(0.2)
    assert services.memory.store.count().get("conversation") == 1

    other = services.history.new_session()  # a new conversation has nothing to take back yet
    assert await services.take_back_last_exchange(other.chat_id) is None


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
