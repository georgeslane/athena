"""Tools found when they're needed: left out of every prompt, found with find_tools, and run with use_tool just as
they would be by name: asking first, checked for personal details, and counted as themselves."""

import json

import pytest
from conftest import FakeLLMServer, completion
from test_agent import make_agent
from test_dashboard import athena, client, wait_applied  # noqa: F401  (athena is a fixture)

from athena.llm import ToolCall
from athena.mcp_manager import RESERVED_NAMES
from athena.privacy import Verdict
from athena.settings import Settings, SettingsError
from athena.stats import UsageStats
from athena.tool_search import FIND_TOOLS, USE_TOOL, find, meta_tools, summary, unwrap
from athena.tools import Tool, ToolError, ToolRegistry


def tool(name, description, asks=False, calls=None):
    async def handler(args):
        if calls is not None:
            calls.append((name, args))
        return f"{name} did it"

    schema = {"type": "object", "properties": {"ticker": {"type": "string"}}}
    return Tool(name, description, schema, handler, needs_confirmation=asks)


PORTFOLIO = "Show the user's Trading 212 portfolio: its value, cash and each holding. Read-only."
ORDER = "Place an order to buy or sell shares on Trading 212. They approve it first."
WEATHER = "Today's weather where the user is."


def registry(on_demand=("trading212_*",), calls=None):
    tools = ToolRegistry()
    for t in (
        tool("weather", WEATHER, calls=calls),
        tool("trading212_portfolio", PORTFOLIO, calls=calls),
        tool("trading212_place_order", ORDER, asks=True, calls=calls),
    ):
        tools.add(t)
    tools.on_demand = list(on_demand)
    return tools


# -- the registry ---------------------------------------------------------------------------------------


def test_tools_found_when_needed_are_left_out_of_every_prompt_and_find_tools_takes_their_place():
    tools = registry()
    sent = [s["function"]["name"] for s in tools.schemas()]
    assert sent == ["weather", FIND_TOOLS, USE_TOOL]
    find_tools = tools.get(FIND_TOOLS)
    assert "trading212_portfolio: Show the user's Trading 212 portfolio: its value, cash and each holding" in (
        find_tools.description
    )
    assert "trading212_place_order: Place an order to buy or sell shares on Trading 212" in find_tools.description
    use = tools.get(USE_TOOL).parameters["properties"]["name"]
    assert use["enum"] == ["trading212_portfolio", "trading212_place_order"]
    assert tools.get("trading212_portfolio").description == PORTFOLIO  # it can still be run by name
    assert [t.name for t in tools.all()] == ["weather", "trading212_portfolio", "trading212_place_order"]


def test_an_mcp_server_cant_take_their_names():
    assert {FIND_TOOLS, USE_TOOL} <= RESERVED_NAMES  # one offering either is given its server's name first


def test_with_none_found_when_needed_everything_is_sent_as_before():
    for on_demand in ((), ("nothing_*",)):
        tools = registry(on_demand)
        assert [s["function"]["name"] for s in tools.schemas()] == [
            "weather",
            "trading212_portfolio",
            "trading212_place_order",
        ]
        assert tools.get(FIND_TOOLS) is None


def test_what_each_does_is_its_first_sentence_cut_short():
    assert summary(tool("a", "Does one thing. And another.")) == "Does one thing"
    long = summary(tool("a", "word " * 40))
    assert len(long) <= 80 and long.endswith("…")


# -- finding ------------------------------------------------------------------------------------------


def test_find_ranks_by_name_then_by_words():
    tools = registry().deferred()
    assert [t.name for t in find(tools, "trading212_place_order")] == ["trading212_place_order"] + [
        "trading212_portfolio"
    ]
    assert [t.name for t in find(tools, "buy shares")][0] == "trading212_place_order"
    assert [t.name for t in find(tools, "portfolio value")][0] == "trading212_portfolio"
    assert find(tools, "the weather") == []  # only those found when needed


async def test_find_tools_describes_what_it_finds_in_full():
    find_tools, _ = meta_tools(registry().deferred())
    [found] = json.loads(await find_tools.handler({"query": "place order"}))[:1]
    assert found == {
        "name": "trading212_place_order",
        "description": ORDER,
        "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}}},
        "asks_first": True,
    }
    nothing = await find_tools.handler({"query": "spaceships"})
    assert (
        nothing == "No tool matches “spaceships”. Those you can find are: trading212_portfolio, trading212_place_order."
    )
    with pytest.raises(ToolError, match="Say which tool"):
        await find_tools.handler({"query": " "})


# -- use_tool -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"name": "trading212_portfolio", "arguments": {"ticker": "AAPL"}},
            ("trading212_portfolio", {"ticker": "AAPL"}),
        ),
        (
            {"name": "trading212_portfolio", "arguments": '{"ticker": "AAPL"}'},
            ("trading212_portfolio", {"ticker": "AAPL"}),
        ),
        ({"name": "trading212_portfolio"}, ("trading212_portfolio", {})),
        ({"arguments": {}}, "Error: say which tool to use"),
        ({"name": USE_TOOL, "arguments": {}}, "Error: say which tool to use"),  # not itself, round and round
        ({"name": FIND_TOOLS, "arguments": {}}, "Error: say which tool to use"),
        ({"name": "x", "arguments": [1, 2]}, "Error: use_tool's arguments must be an object"),
        ({"name": "x", "arguments": "{not json"}, "Error: use_tool's arguments must be an object"),
    ],
)
def test_use_tool_stands_for_the_call_it_names(arguments, expected):
    run = unwrap(ToolCall("call_1", USE_TOOL, arguments, json.dumps(arguments)))
    if isinstance(expected, str):
        assert run.startswith(expected)
    else:
        assert (run.id, run.name, run.arguments) == ("call_1", *expected)


def agent_with(config, memory, server, calls, **kwargs):
    agent = make_agent(config, memory, server)
    for t in registry(calls=calls).all():
        agent.tools.add(t)
    agent.tools.on_demand = ["trading212_*"]
    return agent


async def test_the_model_finds_a_tool_and_uses_it_with_the_tools_sent_staying_the_same(config, memory):
    calls = []
    server = FakeLLMServer(
        [
            completion(None, [(FIND_TOOLS, {"query": "portfolio"})]),
            completion(None, [(USE_TOOL, {"name": "trading212_portfolio", "arguments": {"ticker": "AAPL"}})]),
            completion("You hold Apple."),
        ]
    )
    stats = UsageStats(config.db_path)
    agent = agent_with(config, memory, server, calls)
    agent.stats = stats
    seen = []

    async def on_tool(name, args, output, outcome):
        seen.append((name, args))

    result = await agent.respond("chat", "What do I hold?", on_tool=on_tool)
    assert result.text == "You hold Apple."
    assert calls == [("trading212_portfolio", {"ticker": "AAPL"})]
    assert result.tools_used == [FIND_TOOLS, "trading212_portfolio"]  # as itself, not use_tool
    assert seen == [(FIND_TOOLS, {"query": "portfolio"}), ("trading212_portfolio", {"ticker": "AAPL"})]
    assert stats.tool_calls_since(0) == {FIND_TOOLS: 1, "trading212_portfolio": 1}

    sent = [[t["function"]["name"] for t in r["tools"]] for r in server.requests]
    assert all(names == sent[0] for names in sent) and "trading212_portfolio" not in sent[0]  # the cache holds
    found = server.requests[1]["messages"][-1]
    assert found["role"] == "tool" and found["name"] == FIND_TOOLS and PORTFOLIO in found["content"]
    used = server.requests[2]["messages"][-1]
    assert used == {
        "role": "tool",
        "tool_call_id": "call_0",
        "name": USE_TOOL,
        "content": "trading212_portfolio did it",
    }
    stats.close()


async def test_a_tool_used_through_use_tool_still_asks_first_and_is_checked_for_personal_details(config, memory):
    calls, asked, checked = [], [], []
    order = {"name": "trading212_place_order", "arguments": {"ticker": "AAPL"}}
    server = FakeLLMServer([completion(None, [(USE_TOOL, order)]), completion("I didn't.")])
    agent = agent_with(config, memory, server, calls)

    class Privacy:
        def check(self, name, args, asks):
            checked.append((name, args, asks))
            return Verdict(())

    agent.privacy = Privacy()

    async def confirm(name, args, summary=None):
        asked.append((name, args))
        return False

    stats = UsageStats(config.db_path)
    agent.stats = stats
    result = await agent.respond("chat", "Buy Apple", confirm=confirm)
    assert asked == [("trading212_place_order", {"ticker": "AAPL"})]  # you're asked about the order itself
    assert checked == [("trading212_place_order", {"ticker": "AAPL"}, True)]
    assert calls == [] and result.tools_used == ["trading212_place_order"]
    told = server.requests[1]["messages"][-1]["content"]
    assert told.startswith("The user declined this action")
    assert stats.summary()["tools"] == [{"name": "trading212_place_order", "calls": 1, "failed": 0, "declined": 1}]
    stats.close()


async def test_a_secret_stops_a_tool_used_through_use_tool_as_it_would_by_name(config, memory):
    from athena.privacy import Finding

    calls = []
    server = FakeLLMServer(
        [
            completion(None, [(USE_TOOL, {"name": "trading212_portfolio", "arguments": {"ticker": "sk-secret"}})]),
            completion("OK."),
        ]  # fmt: skip
    )
    agent = agent_with(config, memory, server, calls)

    class Privacy:
        def check(self, name, args, asks):
            return Verdict((Finding("API key", "sk-secret", secret=True),), refused=True)

    agent.privacy = Privacy()
    await agent.respond("chat", "Look")
    assert calls == [] and "must never leave Athena" in server.requests[1]["messages"][-1]["content"]


async def test_a_use_tool_call_that_names_no_tool_is_told_so(config, memory):
    server = FakeLLMServer([completion(None, [(USE_TOOL, {"name": "nonexistent", "arguments": {}})]),
                            completion(None, [(USE_TOOL, {"arguments": {}})]), completion("Sorry.")])  # fmt: skip
    agent = agent_with(config, memory, server, [])
    result = await agent.respond("chat", "Do it")
    assert server.requests[1]["messages"][-1]["content"] == "Error: there is no tool called 'nonexistent'."
    assert server.requests[2]["messages"][-1]["content"].startswith("Error: say which tool to use")
    assert result.tools_used == ["nonexistent", USE_TOOL]


async def test_a_tool_found_when_needed_can_be_called_by_name_too(config, memory):
    calls = []
    server = FakeLLMServer([completion(None, [("trading212_portfolio", {})]), completion("Done.")])
    agent = agent_with(config, memory, server, calls)
    await agent.respond("chat", "Portfolio?")
    assert calls == [("trading212_portfolio", {})]


# -- choosing them --------------------------------------------------------------------------------------


def test_choosing_tools_keeps_patterns_written_by_hand(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[llm]\nbase_url = "http://x/v1"\nmodel = "m"\n\n[tools]\non_demand = ["trading212_*", "old"]\n')
    (tmp_path / ".env").write_text("")
    settings = Settings(path, tmp_path / ".env")
    known = ["weather", "trading212_portfolio", "trading212_place_order", "database_query"]
    cfg = settings.set_on_demand(["database_query", "trading212_portfolio", "trading212_place_order"], known)
    assert cfg.tools.on_demand == ["trading212_*", "database_query"]  # "old", a name, is chosen afresh
    with pytest.raises(SettingsError, match="no tool called nope"):
        settings.set_on_demand(["nope"], known)
    with pytest.raises(SettingsError, match="trading212_place_order is found when needed because of “trading212_\\*”"):
        settings.set_on_demand(["trading212_portfolio"], known)


async def test_the_dashboard_shows_what_each_tool_costs_and_chooses_those_found_when_needed(athena):  # noqa: F811
    async with client(athena) as http:
        listing = (await http.get("/api/tools")).json()["on_demand"]
        by_name = {t["name"]: t for t in listing["tools"]}
        assert by_name["add"]["from"] == "demo" and by_name["remember"]["from"] == "Athena"
        assert all(t["tokens"] > 0 and t["used"] == 0 and not t["on_demand"] for t in listing["tools"])
        assert listing["days"] == 30 and listing["in_prompt"] == sum(t["tokens"] for t in listing["tools"])
        assert listing["finding"] == 0

        assert (await http.post("/api/tools/on-demand", json={"tools": ["add", "get_env"]})).status_code == 200
        listing = (await wait_applied(http))["on_demand"]
        assert {t["name"] for t in listing["tools"] if t["on_demand"]} == {"add", "get_env"}
        others = sum(t["tokens"] for t in listing["tools"] if not t["on_demand"])
        assert listing["finding"] > 100 and listing["in_prompt"] == others + listing["finding"]
        assert athena.tools.on_demand == ["add", "get_env"]
        assert [s["function"]["name"] for s in athena.tools.schemas()][-2:] == [FIND_TOOLS, USE_TOOL]
        assert 'on_demand = ["add", "get_env"]' in athena.config.path.read_text()

        for bad in ({"tools": "add"}, {"tools": [1]}, {"tools": ["nope"]}):
            assert (await http.post("/api/tools/on-demand", json=bad)).status_code == 400
        assert (await http.post("/api/tools/on-demand", json={"tools": []})).status_code == 200
        await wait_applied(http)
        assert athena.tools.on_demand == [] and athena.tools.get(FIND_TOOLS) is None
