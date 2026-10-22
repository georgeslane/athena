"""`athena eval`: comparing models on tool calling, against a scripted model that gets each case right, or wrong
in the ways a weak model does."""

import json
from datetime import datetime, timedelta

from conftest import FakeLLMServer, completion

from athena.evals import build_cases, run_eval

TOMORROW = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

# What a model that does every case well would say, by what was asked.
RIGHT = {
    "Edinburgh": ("get_weather", {"city": "Edinburgh"}),
    "tomorrow?": ("list_calendar_events", {"date": TOMORROW}),
    "Dentist": ("create_calendar_event", {"title": "Dentist", "start": f"{TOMORROW}T15:00"}),
    "birthday": ("search_memory", {"query": "sister's birthday"}),
    "vegetarian": ("remember", {"fact": "Sam, the user's partner, is vegetarian."}),
    "Tokyo": ("get_current_time", {"timezone": "Asia/Tokyo"}),
    "London": ("get_weather", {"city": "London"}),
}


def model(mistakes=()):
    """A scripted model: right, except for the cases named in ``mistakes``."""

    def reply(body: dict) -> dict:
        last = body["messages"][-1]
        if last["content"] == "Say OK.":
            return completion("OK.")
        if last["role"] == "tool":  # the weather came back with rain: add the umbrella
            if "multi-step" in mistakes:
                return completion("You might want an umbrella.")
            return completion(
                None, [("create_calendar_event", {"title": "Take umbrella", "start": f"{TOMORROW}T08:00"})]
            )
        prompt = last["content"]
        for words, (tool, args) in RIGHT.items():
            if words in prompt:
                if "calendar-read" in mistakes and tool == "list_calendar_events":
                    args = {"date": "tomorrow"}
                if "memory-save" in mistakes and tool == "remember":
                    return completion("Noted!")  # says it'll remember, and doesn't
                return completion(None, [(tool, args)])
        if "no-tool-chat" in mistakes and "poem" in prompt:
            return completion(None, [("search_memory", {"query": "autumn"})])
        return completion("Canberra." if "capital" in prompt else "Leaves fall soft and gold,\nthe year exhales.")

    return reply


async def test_a_model_that_gets_everything_right_passes_every_case(config):
    said = []
    llm = FakeLLMServer(model()).client(config.llm)
    [report] = await run_eval(llm, ["good-model"], log=said.append)
    assert report.error is None and report.passed == len(build_cases(datetime.now())) == 9
    assert said[-1].startswith("  good-model: 9/9 passed")


async def test_a_weaker_model_fails_with_the_reasons(config):
    llm = FakeLLMServer(model(mistakes=("calendar-read", "memory-save", "no-tool-chat", "multi-step"))).client(
        config.llm
    )
    [report] = await run_eval(llm, ["weak-model"], log=lambda line: None)
    failed = {r.case: r.reason for r in report.results if not r.passed}
    assert failed == {
        "calendar-read": "bad date: 'tomorrow'",
        "memory-save": "expected remember, got a text answer",
        "no-tool-chat": "unnecessary tool call: search_memory",
        "multi-step": "expected create_calendar_event, got a text answer",
    }
    assert report.passed == 5


async def test_each_case_is_run_as_often_as_asked_and_a_model_that_cant_be_used_is_reported(config):
    server = FakeLLMServer(model())
    llm = server.client(config.llm)
    [good] = await run_eval(llm, ["good-model"], repeat=2, log=lambda line: None)
    assert len(good.results) == 18 and good.passed == 18
    server.error = "model 'missing-model' not found"
    [missing] = await run_eval(llm, ["missing-model"], log=lambda line: None)
    assert missing.error and "not found" in missing.error and missing.results == []
    # Each request offered the same six tools, and nothing ran: tool results are canned.
    offered = {tuple(t["function"]["name"] for t in r["tools"]) for r in server.requests if r.get("tools")}
    assert len(offered) == 1 and len(next(iter(offered))) == 6
    assert json.dumps(server.requests).count("Heavy rain") >= 2  # the canned weather, for the two-step case
