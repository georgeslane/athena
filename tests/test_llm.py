"""What Athena asks the model server for, besides the conversation: how much the model thinks."""

import pytest
from conftest import FakeLLMServer, completion

from pi_assistant.config import LLMConfig
from pi_assistant.llm import request_options


@pytest.mark.parametrize(
    ("thinking", "extra_body", "expected"),
    [
        # Unset: whatever extra_body says, as before.
        (None, {}, {}),
        (None, {"top_k": 20}, {"top_k": 20}),
        ("off", {}, {"chat_template_kwargs": {"enable_thinking": False}}),
        ("on", {}, {"chat_template_kwargs": {"enable_thinking": True}}),
        ("short", {}, {"chat_template_kwargs": {"enable_thinking": True}, "thinking_budget": 1000}),
        # The setting wins over extra_body, which keeps everything else.
        (
            "on",
            {"chat_template_kwargs": {"enable_thinking": False, "x": 1}, "thinking_budget": 50, "top_k": 20},
            {"chat_template_kwargs": {"enable_thinking": True, "x": 1}, "top_k": 20},
        ),
        # oMLX refuses a request whose shorthand disagrees with the template's setting.
        (
            "off",
            {"enable_thinking": True},
            {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}},
        ),
    ],
)
def test_thinking_goes_in_each_request(thinking, extra_body, expected):
    cfg = LLMConfig(model="m", thinking=thinking, extra_body=extra_body)
    before = repr(extra_body)
    assert request_options(cfg) == expected
    assert repr(extra_body) == before  # the config itself isn't changed


@pytest.mark.parametrize(
    ("thinking", "extra_body", "now"),
    [
        (None, {}, "on"),
        (None, {"chat_template_kwargs": {"enable_thinking": False}}, "off"),
        (None, {"enable_thinking": False}, "off"),
        (None, {"thinking_budget": 500}, "short"),
        ("short", {"chat_template_kwargs": {"enable_thinking": False}}, "short"),
    ],
)
def test_which_thinking_it_is(thinking, extra_body, now):
    assert LLMConfig(model="m", thinking=thinking, extra_body=extra_body).thinking_now == now


async def test_the_request_has_it():
    cfg = LLMConfig(model="m", thinking="short", short_thinking_tokens=300)
    server = FakeLLMServer([completion("Hi."), completion("Hi.")])
    llm = server.client(cfg)
    await llm.chat([{"role": "user", "content": "hi"}])
    assert server.requests[0]["chat_template_kwargs"] == {"enable_thinking": True}
    assert server.requests[0]["thinking_budget"] == 300
    cfg.thinking = "off"  # as /think does, while Athena runs
    await llm.chat([{"role": "user", "content": "hi"}])
    assert server.requests[1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "thinking_budget" not in server.requests[1]
    with pytest.raises(ValueError):
        LLMConfig(model="m", thinking="lots")
