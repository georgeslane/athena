"""A small tool-calling test for comparing models on your own hardware.

Each case sends a request with a fixed set of fake tools and checks whether the
model picks the right tool with sensible arguments (or correctly answers without
one). Nothing is executed; tool results are canned.

    pi-assistant eval --model gemma-4-26b-a4b-it-4bit --model qwen3.6-35b-a3b-4bit
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pi_assistant.llm import LLMClient, LLMReply


def _fn(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


TOOLS = [
    _fn(
        "get_weather",
        "Current weather and today's forecast for a city.",
        {"city": {"type": "string"}, "units": {"type": "string", "enum": ["metric", "imperial"]}},
        ["city"],
    ),
    _fn(
        "list_calendar_events",
        "List calendar events on a date.",
        {"date": {"type": "string", "description": "YYYY-MM-DD"}},
        ["date"],
    ),
    _fn(
        "create_calendar_event",
        "Create a calendar event.",
        {
            "title": {"type": "string"},
            "start": {"type": "string", "description": "ISO 8601 local date-time, e.g. 2026-10-05T15:00"},
            "duration_minutes": {"type": "integer"},
            "location": {"type": "string"},
        },
        ["title", "start"],
    ),
    _fn(
        "search_memory",
        "Search long-term memory for facts the user has told you.",
        {"query": {"type": "string"}},
        ["query"],
    ),
    _fn(
        "remember",
        "Save a durable fact about the user to long-term memory.",
        {"fact": {"type": "string"}},
        ["fact"],
    ),
    _fn(
        "get_current_time",
        "Current time in an IANA timezone.",
        {"timezone": {"type": "string", "description": "e.g. Europe/London"}},
        ["timezone"],
    ),
]


Check = Callable[[LLMReply], str | None]  # returns None on success, or a reason for failure


def expect_tool(name: str, **arg_checks: Callable[[Any], bool]) -> Check:
    def check(reply: LLMReply) -> str | None:
        if not reply.tool_calls:
            return f"expected {name}, got a text answer"
        call = reply.tool_calls[0]
        if call.name != name:
            return f"expected {name}, called {call.name}"
        if call.arguments is None:
            return f"invalid JSON arguments: {call.raw_arguments[:80]}"
        for arg, ok in arg_checks.items():
            if not ok(call.arguments.get(arg)):
                return f"bad {arg}: {call.arguments.get(arg)!r}"
        return None

    return check


def expect_no_tool(reply: LLMReply) -> str | None:
    if reply.tool_calls:
        return f"unnecessary tool call: {reply.tool_calls[0].name}"
    return None if reply.content.strip() else "empty answer"


def has(*words: str) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, str) and all(w.lower() in v.lower() for w in words)


@dataclass
class Case:
    name: str
    prompt: str
    check: Check
    # Optional follow-up: canned result for the first tool call, then a check on the next reply.
    tool_result: str | None = None
    then: Check | None = None


def build_cases(now: datetime) -> list[Case]:
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return [
        Case(
            "weather",
            "What's the weather like in Edinburgh right now?",
            expect_tool("get_weather", city=has("edinburgh")),
        ),
        Case(
            "calendar-read",
            "What have I got on tomorrow?",
            expect_tool("list_calendar_events", date=lambda v: v == tomorrow),
        ),
        Case(
            "calendar-write",
            "Put 'Dentist' in my calendar for tomorrow at 3pm.",
            expect_tool(
                "create_calendar_event",
                title=has("dentist"),
                start=lambda v: isinstance(v, str) and tomorrow in v and "15:00" in v,
            ),
        ),
        Case(
            "memory-search", "When is my sister's birthday again?", expect_tool("search_memory", query=has("birthday"))
        ),
        Case(
            "memory-save",
            "FYI my partner Sam is vegetarian, keep that in mind for restaurant suggestions.",
            expect_tool("remember", fact=has("sam", "vegetarian")),
        ),
        Case("timezone", "What time is it in Tokyo?", expect_tool("get_current_time", timezone=has("tokyo"))),
        Case("no-tool-chat", "Write a two-line poem about autumn.", expect_no_tool),
        Case("no-tool-fact", "What's the capital of Australia?", expect_no_tool),
        Case(
            "multi-step",
            "Check the weather in London and, if it's going to rain, "
            "add 'Take umbrella' to my calendar for 8am tomorrow.",
            expect_tool("get_weather", city=has("london")),
            tool_result=json.dumps({"city": "London", "now": "Cloudy, 12C", "forecast": "Heavy rain from 9am"}),
            then=expect_tool(
                "create_calendar_event",
                title=has("umbrella"),
                start=lambda v: isinstance(v, str) and tomorrow in v and "08:00" in v,
            ),
        ),
    ]


@dataclass
class CaseResult:
    case: str
    passed: bool
    reason: str | None
    seconds: float
    tokens_per_second: float | None


@dataclass
class ModelReport:
    model: str
    results: list[CaseResult] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)


async def run_case(llm: LLMClient, model: str, case: Case, now: datetime) -> CaseResult:
    system = (
        "You are a personal assistant with tools. Use a tool when it's needed; otherwise answer directly. "
        f"Current local time: {now:%A %Y-%m-%d %H:%M} (Europe/London)."
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": case.prompt}]
    reply = await llm.chat(messages, TOOLS, model=model, temperature=0.0, max_tokens=1024)
    seconds, tps = reply.elapsed, reply.tokens_per_second
    reason = case.check(reply)
    if reason is None and case.then and case.tool_result is not None:
        call = reply.tool_calls[0]
        messages.append({"role": "assistant", "content": reply.content or "", "tool_calls": [call.as_message_part()]})
        messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": case.tool_result})
        reply = await llm.chat(messages, TOOLS, model=model, temperature=0.0, max_tokens=1024)
        seconds += reply.elapsed
        reason = case.then(reply)
    return CaseResult(case.name, reason is None, reason, seconds, tps)


async def run_eval(
    llm: LLMClient, models: list[str], repeat: int = 1, log: Callable[[str], None] = print
) -> list[ModelReport]:
    now = datetime.now()
    cases = build_cases(now)
    reports: list[ModelReport] = []
    for model in models:
        report = ModelReport(model)
        reports.append(report)
        log(f"\n== {model} ==")
        try:  # warm-up: loading the model shouldn't count towards the timings
            await llm.chat([{"role": "user", "content": "Say OK."}], model=model, max_tokens=8)
        except Exception as exc:
            report.error = f"{type(exc).__name__}: {exc}"
            log(f"  could not use this model: {report.error}")
            continue
        for _ in range(repeat):
            for case in cases:
                try:
                    res = await run_case(llm, model, case, now)
                except Exception as exc:
                    res = CaseResult(case.name, False, f"request failed: {exc}", 0.0, None)
                report.results.append(res)
                mark = "PASS" if res.passed else "FAIL"
                speed = f", {res.tokens_per_second:.0f} tok/s" if res.tokens_per_second else ""
                detail = f" - {res.reason}" if res.reason else ""
                log(f"  {mark}  {case.name:<15} {res.seconds:5.1f}s{speed}{detail}")
    log("\n== Summary ==")
    for r in reports:
        if r.error:
            log(f"  {r.model}: error ({r.error})")
            continue
        times = [x.seconds for x in r.results if x.seconds]
        median = statistics.median(times) if times else 0.0
        log(f"  {r.model}: {r.passed}/{len(r.results)} passed, median {median:.1f}s per case")
    return reports
