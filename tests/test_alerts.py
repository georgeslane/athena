"""Alerts: Athena tells you when something it needs stops working, once, and when it's back."""

import sys

import pytest
from conftest import FakeEmbedder, FakeLLMServer, completion

from athena import alerts
from athena.alerts import HealthMonitor, check_health
from athena.app import build_services
from athena.config import AlertsConfig, load_config


class Checks:
    """How things are, as the test says, and what you were told."""

    def __init__(self):
        self.now: dict[str, tuple[str, str | None]] = {}
        self.said: list[str] = []
        self.can_tell = True

    async def check(self):
        return dict(self.now)

    async def tell(self, text, *, session=None):
        if self.can_tell:
            self.said.append(text)
        return self.can_tell


async def test_you_are_told_once_something_keeps_failing_and_when_its_back():
    c = Checks()
    monitor = HealthMonitor(AlertsConfig(after_failures=2), c.check, c.tell)
    c.now = {"model": ("The model server", None), "mcp:apple": ("The MCP server “apple”", None)}
    await monitor.check_once()
    assert c.said == []

    c.now["model"] = ("The model server", "ConnectError: no route to host")
    await monitor.check_once()
    assert c.said == []  # once might be a blip
    await monitor.check_once()
    assert c.said == ["⚠️ The model server isn't working: ConnectError: no route to host"]
    await monitor.check_once()
    assert len(c.said) == 1  # told once, not every time

    c.now["mcp:apple"] = ("The MCP server “apple”", "it exited")
    c.now["model"] = ("The model server", None)
    await monitor.check_once()
    assert c.said[-1] == "✅ The model server is working again."
    await monitor.check_once()
    assert c.said[-1] == "⚠️ The MCP server “apple” isn't working: it exited"

    # Something no longer checked, such as a server you've removed, isn't said to be working again.
    del c.now["mcp:apple"]
    await monitor.check_once()
    assert len(c.said) == 3 and monitor.told == {}


async def test_what_couldnt_be_sent_is_sent_next_time():
    c = Checks()
    monitor = HealthMonitor(AlertsConfig(after_failures=1), c.check, c.tell)
    c.now = {"disk": ("The drive Athena's data is on", "only 0.4 GB is free")}
    c.can_tell = False  # Telegram isn't there yet
    await monitor.check_once()
    c.can_tell = True
    await monitor.check_once()
    assert c.said == ["⚠️ The drive Athena's data is on isn't working: only 0.4 GB is free"]


async def test_several_things_come_in_one_message():
    c = Checks()
    monitor = HealthMonitor(AlertsConfig(after_failures=1), c.check, c.tell)
    c.now = {"model": ("The model server", "down"), "embeddings": ("The embeddings server", "down too")}
    await monitor.check_once()
    assert c.said == ["⚠️ The model server isn't working: down\n\n⚠️ The embeddings server isn't working: down too"]


CONFIG = f"""
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

[trading212]
enabled = true

[mcp_servers.broken]
command = "{sys.executable}"
args = ["-c", "import sys; sys.exit(3)"]
sandbox = false
timeout_seconds = 10
"""


@pytest.fixture
async def services(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADING212_API_KEY", raising=False)
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / ".env").write_text("")
    cfg = load_config(tmp_path / "config.toml")
    services = build_services(cfg)
    services.memory.embedder = FakeEmbedder()
    model = FakeLLMServer(lambda body: completion("OK."))
    services.llm = services.agent.llm = model.client(cfg.llm)
    await services.start()
    try:
        yield services
    finally:
        await services.close()


async def test_what_athena_checks(services, monkeypatch):
    await services.mcp._connections["broken"].ready.wait()
    health = await check_health(services)
    assert health["model"] == ("The model server", None)
    assert "embeddings" not in health  # the same server as the model, which answered
    what, problem = health["mcp:broken"]
    assert what == "The MCP server “broken”" and problem
    assert health["builtin:trading212"] == ("Trading 212", "TRADING212_API_KEY isn't set in .env")
    assert health["disk"] == ("The drive Athena's data is on", None)
    assert services.alerts is not None  # on by default

    # The Mac's asleep, the embeddings model's missing, and the drive is nearly full.
    async def asleep():
        raise ConnectionError("no route to host")

    async def no_model(*args, **kwargs):
        raise LookupError("model not found")

    monkeypatch.setattr(services.llm, "list_models", asleep)
    monkeypatch.setattr(services.memory.embedder, "embed_one", no_model)
    services.config.alerts.min_free_gb = 1e9
    health = await check_health(services)
    assert health["model"] == ("The model server", "ConnectionError: no route to host")
    assert health["embeddings"] == ("The embeddings server", "LookupError: model not found")
    assert health["disk"][1].startswith("only ") and health["disk"][1].endswith(" GB is free")


async def test_a_check_that_hangs_is_a_problem(services, monkeypatch):
    monkeypatch.setattr(alerts, "CHECK_SECONDS", 0.05)

    async def hangs():
        import asyncio

        await asyncio.Event().wait()

    monkeypatch.setattr(services.llm, "list_models", hangs)
    health = await check_health(services)
    assert health["model"] == ("The model server", "it didn't answer within 0.05 seconds")

    async def other_models():
        return ["another-model"]

    monkeypatch.setattr(services.llm, "list_models", other_models)
    health = await check_health(services)
    assert health["model"] == ("The model server", "it doesn't have the model test-model")


async def test_alerts_go_to_telegram_once_it_has_started(services):
    assert await services._alert("hi") is False  # no channel yet: tried again next time

    class Telegram:
        chat_id = "1"
        said: list[str] = []

        async def tell(self, text, *, session=None):
            self.said.append(text)

    services.channel = Telegram()
    assert await services._alert("hi") is True and Telegram.said == ["hi"]
