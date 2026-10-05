"""`pi-assistant doctor`'s measurement of the prompt the model reads before every reply."""

from types import SimpleNamespace

import pytest
from conftest import FakeLLMServer, completion

from pi_assistant import doctor
from pi_assistant.doctor import FAIL, OK, WARN, PromptCost, check_prompt, measure_prompt, report_prompt
from pi_assistant.tools import Tool

SCHEMAS = [{"type": "function", "function": {"name": "x", "description": "y", "parameters": {"type": "object"}}}]


def fake_server():
    def reply(body):
        answer = completion("OK")
        answer["usage"]["prompt_tokens"] = 1100 if body.get("tools") else 100
        return answer

    return FakeLLMServer(reply)


async def test_measures_the_same_prompt_with_and_without_tools(config):
    server = fake_server()
    cost = await measure_prompt(server.client(config.llm), "You are Testy.", SCHEMAS)

    bare, cold, cached = server.requests
    assert "tools" not in bare and cold["tools"] == SCHEMAS
    assert cached == cold  # the same prompt again, to see whether it was cached
    assert bare["messages"][0]["content"] != cold["messages"][0]["content"]  # each starts with nothing cached
    assert cold["messages"][0]["content"].endswith("\nYou are Testy.")
    assert all(request["max_tokens"] == 1 for request in server.requests)
    assert cost.tools == 1 and cost.tool_tokens == 1000


@pytest.mark.parametrize(
    ("cost", "marks", "says"),
    [
        (PromptCost(25, 9000, 60.0, 50.0, 1.0), [OK, OK, OK, WARN], ["9,000 tokens", "180 tokens a second", "1.0s"]),
        (PromptCost(3, 800, 10.0, 4.0, 9.5), [OK, OK, WARN], ["doesn't seem to reuse"]),
        (PromptCost(3, 800, 1.0, 0.2, 0.9), [OK, OK], ["3 tools, adding 800 tokens"]),  # too quick to matter
        (PromptCost(3, None, 1.0, 0.2, 0.9), [OK, OK], ["3 tools\n"]),  # the server didn't count tokens
    ],
)
def test_reports_what_the_prompt_costs(cost, marks, says):
    reported = []
    report_prompt(cost, lambda mark, msg: reported.append((mark, msg)))
    assert [mark for mark, _ in reported] == marks
    text = "\n".join(msg for _, msg in reported) + "\n"
    assert all(phrase in text for phrase in says)


async def test_check_uses_the_assistants_own_prompt_and_tools(config, monkeypatch):
    server = fake_server()
    builtin = Tool(name="remember", description="Save a fact", parameters={"type": "object"}, handler=None)
    from_mcp = Tool(name="search_files", description="Search", parameters={"type": "object"}, handler=None)
    closed = []

    async def close():
        closed.append(True)

    services = SimpleNamespace(
        tools=SimpleNamespace(all=lambda: [builtin]),
        agent=SimpleNamespace(system_prompt="You are Athena."),
        llm=server.client(config.llm),
        close=close,
    )
    monkeypatch.setattr(doctor, "build_services", lambda cfg: services)
    reported = []
    await check_prompt(config, [from_mcp], lambda mark, msg: reported.append((mark, msg)))

    assert [t["function"]["name"] for t in server.requests[1]["tools"]] == ["remember", "search_files"]
    assert reported[0] == (OK, "2 tools, adding 1,000 tokens to every request")
    assert closed


async def test_check_reports_a_failing_model_server(config, monkeypatch):
    async def close():
        pass

    def broken(body):
        raise ConnectionError("no route to the Mac")

    services = SimpleNamespace(
        tools=SimpleNamespace(all=lambda: []),
        agent=SimpleNamespace(system_prompt="."),
        llm=FakeLLMServer(broken).client(config.llm),
        close=close,
    )
    monkeypatch.setattr(doctor, "build_services", lambda cfg: services)
    reported = []
    await check_prompt(config, [], lambda mark, msg: reported.append((mark, msg)))
    assert [mark for mark, _ in reported] == [FAIL]


async def test_checks_the_trading_212_key_and_its_permissions(config):
    import httpx
    from test_trading212 import FakeTrading212

    def run(t212, key="KEY"):
        reported = []
        config.trading212.api_key = key
        http = httpx.AsyncClient(transport=httpx.MockTransport(t212.handler))
        return reported, doctor.check_trading212(config, lambda mark, msg: reported.append((mark, msg)), http)

    reported, check = run(FakeTrading212())
    await check
    assert reported == [
        (OK, "connected to your live account, 12345678 in GBP"),
        (OK, "the key can read everything Athena uses"),
    ]

    t212 = FakeTrading212()
    t212.reply("GET", "/orders", httpx.Response(403))
    t212.reply("GET", "/history/dividends", httpx.Response(403))
    reported, check = run(t212)
    await check
    assert reported[1] == (
        WARN,
        "the key can't read: orders:read, history:dividends. Add them in Trading 212, under Settings > API",
    )
    assert {r.method for r in t212.requests} == {"GET"}  # checking never trades

    t212 = FakeTrading212()
    t212.reply("GET", "/account/summary", httpx.Response(401))
    reported, check = run(t212)
    await check
    assert reported[0][0] == FAIL and "didn't accept the API key" in reported[0][1]

    reported, check = run(FakeTrading212(), key="")
    await check
    assert reported == [(FAIL, "TRADING212_API_KEY is not set (.env)")]


def test_checks_each_database_opens(config, tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "budget.db")
    conn.execute("CREATE TABLE expenses (x)")
    conn.close()
    config.sqlite.databases = {"budget": "budget.db", "gone": "gone.db"}
    reported = []
    doctor.check_databases(config, lambda mark, msg: reported.append((mark, msg)))
    assert reported == [
        (OK, f"budget: 1 table, read-only ({tmp_path / 'budget.db'})"),
        (FAIL, f"gone: There's no database file at {tmp_path / 'gone.db'}."),
    ]
