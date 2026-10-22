"""The commands Telegram, the dashboard and the terminal share (commands.py)."""

from conftest import FakeEmbedder, FakeLLMServer, completion

from athena import commands
from athena.app import build_services


def test_a_command_and_what_follows_it():
    assert commands.parse("/recall the boiler") == ("recall", "the boiler")
    assert commands.parse("/Think@athena_bot  short ") == ("think", "short")
    assert commands.parse("/session") == ("session", "")
    for not_one in ("hello", "/", " /session", "/ recall"):
        assert commands.parse(not_one) is None, not_one


async def test_running_them(config):
    services = build_services(config)
    services.memory.embedder = FakeEmbedder()
    services.llm = services.agent.llm = FakeLLMServer(lambda body: completion("OK.")).client(config.llm)
    try:

        async def run(line):
            return await commands.run(services, *commands.parse(line), source="test")

        assert (await run("/help")).startswith("Hi! I'm Testy.")
        assert await run("/remember The boiler was serviced in March.") == "Saved as memory #1."
        assert "#1" in await run("/recall boiler") and await run("/forget 1") == "Forgot memory #1."
        assert await run("/forget") == "Usage: /forget <id> (find ids with /recall)"
        assert (await run("/think off")).startswith("Thinking is off")
        assert await run("/think sometimes") == "Usage: /think on, /think short or /think off"
        assert (await run("/dags")).startswith("No agent has a DAG yet.")
        assert (await run("/tasks")).startswith("No agent has a DAG yet.")  # as it was called
        assert (await run("/tools")).startswith("Tools:\n- remember [")
        assert (await run("/status")).startswith("Model: test-model")
        assert (await run("/compact")).startswith("There's nothing to compact yet")
        assert (await run("/new")).startswith("Started a new agent (#2)") and services.history.session.id == 2
        services.agent.answering_you = 1
        assert await run("/session") == "I'm still answering. Wait until I've finished, or stop me, first."
        assert await run("/stop") is None and await run("/nonsense") is None  # each front end's own, or none
    finally:
        await services.close()
