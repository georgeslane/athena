import asyncio

from conftest import FakeLLMServer, completion

from pi_assistant.agent import Agent
from pi_assistant.history import ConversationStore
from pi_assistant.tools import Tool, ToolRegistry


def make_agent(config, memory, server: FakeLLMServer, extra_tools=()):
    tools = ToolRegistry()
    for t in memory.tools():
        tools.add(t)
    for t in extra_tools:
        tools.add(t)
    history = ConversationStore(config.db_path, config.agent.max_history_messages)
    prompt = "You are {assistant_name} helping {user_name}. Literal braces stay: {not_a_key} {}"
    return Agent(config.agent, server.client(config.llm), tools, history, memory, system_prompt_template=prompt)


async def test_plain_answer_is_saved_to_history(config, memory):
    server = FakeLLMServer([completion("Hello George!")])
    agent = make_agent(config, memory, server)

    result = await agent.respond("chat1", "hi")

    assert result.text == "Hello George!"
    assert result.model_calls == 1
    sent = server.requests[0]
    assert sent["model"] == "test-model"
    assert sent["messages"][0] == {
        "role": "system",
        "content": "You are Testy helping George. Literal braces stay: {not_a_key} {}",
    }
    assert sent["messages"][-1]["content"].startswith("<context>\nCurrent time:")
    assert sent["messages"][-1]["content"].endswith("\n\nhi")
    assert {t["function"]["name"] for t in sent["tools"]} == {"remember", "search_memory", "forget_memory"}
    assert agent.history.load("chat1") == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello George!"},
    ]


async def test_tool_call_round_trip_and_cache_friendly_prefix(config, memory):
    server = FakeLLMServer(
        [
            completion(None, [("remember", {"fact": "George's sister Anna's birthday is 14 March."})]),
            completion("Got it, I'll remember Anna's birthday."),
            completion("It's on 14 March."),
        ]
    )
    agent = make_agent(config, memory, server)

    first = await agent.respond("chat1", "My sister Anna's birthday is 14 March")
    assert first.tools_used == ["remember"]
    assert memory.store.count() == {"fact": 1}
    tool_msg = server.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_0"
    assert "Saved as memory #1" in tool_msg["content"]

    second = await agent.respond("chat1", "When is Anna's birthday?")
    assert second.text == "It's on 14 March."
    # Auto-recall put the memory in the context block of the new message...
    assert "Anna's birthday is 14 March" in server.requests[2]["messages"][-1]["content"]
    # ...while the system prompt and earlier turns are unchanged (prompt cache can be reused).
    assert server.requests[2]["messages"][0] == server.requests[0]["messages"][0]
    assert server.requests[2]["messages"][1] == {"role": "user", "content": "My sister Anna's birthday is 14 March"}


async def test_confirmation_declined(config, memory):
    await memory.remember("Old fact about the boiler.")
    server = FakeLLMServer([completion(None, [("forget_memory", {"id": 1})]), completion("OK, I've left it alone.")])
    agent = make_agent(config, memory, server)
    asked = []

    async def confirm(tool, args):
        asked.append((tool, args))
        return False

    result = await agent.respond("chat1", "forget the boiler thing", confirm=confirm)

    assert asked == [("forget_memory", {"id": 1})]
    assert "declined" in server.requests[1]["messages"][-1]["content"]
    assert memory.store.count() == {"fact": 1}
    assert result.text == "OK, I've left it alone."


async def test_confirmation_required_without_channel(config, memory):
    await memory.remember("Another fact.")
    server = FakeLLMServer([completion(None, [("forget_memory", {"id": 1})]), completion("Can't do that here.")])
    agent = make_agent(config, memory, server)
    await agent.respond("chat1", "forget it")
    assert "approval" in server.requests[1]["messages"][-1]["content"]
    assert memory.store.count() == {"fact": 1}


async def test_bad_tool_calls_are_reported_back_to_the_model(config, memory):
    server = FakeLLMServer(
        [
            completion(None, [("no_such_tool", {}), ("remember", "{not json")]),
            completion("Sorry about that."),
        ]
    )
    agent = make_agent(config, memory, server)
    await agent.respond("chat1", "do something")
    tool_msgs = [m for m in server.requests[1]["messages"] if m["role"] == "tool"]
    assert "no tool called 'no_such_tool'" in tool_msgs[0]["content"]
    assert "not valid JSON" in tool_msgs[1]["content"]


async def test_tool_errors_timeouts_and_truncation(config, memory):
    async def boom(args):
        raise ValueError("bad input")

    async def slow(args):
        await asyncio.sleep(5)
        return "late"

    async def big(args):
        return "x" * 10_000

    extra = [
        Tool("boom", "fails", {"type": "object", "properties": {}}, boom),
        Tool("slow", "sleeps", {"type": "object", "properties": {}}, slow),
        Tool("big", "huge output", {"type": "object", "properties": {}}, big),
    ]
    config.agent.tool_timeout_seconds = 0.1
    config.agent.max_tool_result_chars = 100
    server = FakeLLMServer([completion(None, [("boom", {}), ("slow", {}), ("big", {})]), completion("done")])
    agent = make_agent(config, memory, server, extra)

    await agent.respond("chat1", "go")

    outputs = [m["content"] for m in server.requests[1]["messages"] if m["role"] == "tool"]
    assert outputs[0] == "Error: ValueError: bad input"
    assert "timed out" in outputs[1]
    assert outputs[2].startswith("x" * 100) and "truncated 9900 characters" in outputs[2]


async def test_tool_round_limit_forces_final_answer(config, memory):
    config.agent.max_tool_rounds = 2
    server = FakeLLMServer(
        [
            completion(None, [("search_memory", {"query": "a"})]),
            completion(None, [("search_memory", {"query": "b"})]),
            completion("Here's what I found."),
        ]
    )
    agent = make_agent(config, memory, server)
    result = await agent.respond("chat1", "dig around")
    assert result.text == "Here's what I found."
    assert "tools" not in server.requests[2]  # final call made with tools switched off


async def test_reasoning_is_stripped_and_empty_answers_not_saved(config, memory):
    server = FakeLLMServer([completion("<think>hmm</think>\nThe answer is 4."), completion("")])
    agent = make_agent(config, memory, server)
    assert (await agent.respond("c", "2+2?")).text == "The answer is 4."
    result = await agent.respond("c", "and?")
    assert "didn't manage" in result.text
    assert len(agent.history.load("c")) == 2  # failed turn not stored


async def test_embeddings_outage_does_not_break_chat(config, memory):
    async def broken(*a, **k):
        raise ConnectionError("ollama down")

    memory.embedder.embed = broken
    server = FakeLLMServer([completion("Still here.")])
    agent = make_agent(config, memory, server)
    assert (await agent.respond("c", "hello")).text == "Still here."
