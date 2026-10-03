"""Offline tests for the Telegram layer using stand-in bot/update objects."""

import asyncio
import json
from types import SimpleNamespace

from telegram.ext import CallbackQueryHandler, MessageHandler

from pi_assistant.agent import AgentResult
from pi_assistant.config import TelegramConfig
from pi_assistant.telegram_bot import TelegramBot

ME = 111
STRANGER = 999


class FakeMessage:
    def __init__(self, bot, text, kwargs):
        self.bot, self.text, self.kwargs, self.edits = bot, text, kwargs, []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class FakeBot:
    def __init__(self):
        self.sent: list[FakeMessage] = []

    async def send_message(self, chat_id, text, **kwargs):
        msg = FakeMessage(self, text, kwargs)
        self.sent.append(msg)
        return msg

    async def send_chat_action(self, chat_id, action):
        pass


def make_bot(config, respond, allowed=(ME,)):
    config.telegram = TelegramConfig(bot_token="123456:TEST", allowed_user_ids=list(allowed))
    services = SimpleNamespace(config=config, agent=SimpleNamespace(respond=respond))
    return TelegramBot(services)


def text_update(user_id, text, chat_id=1):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id, username="someone"),
        message=SimpleNamespace(text=text),
    )


def button_update(user_id, data):
    answers = []

    async def answer(text=None):
        answers.append(text)

    query = SimpleNamespace(data=data, answer=answer)
    return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id)), answers


def test_handlers_depend_on_allowlist(config):
    app = make_bot(config, None).build()
    handlers = app.handlers[0]
    assert any(isinstance(h, CallbackQueryHandler) for h in handlers)
    assert sum(isinstance(h, MessageHandler) for h in handlers) == 2  # owner text + strangers

    setup_app = make_bot(config, None, allowed=()).build()
    assert [type(h) for h in setup_app.handlers[0]] == [MessageHandler]


async def test_message_round_trip_with_confirmation(config):
    async def respond(chat_id, text, confirm):
        approved = await confirm("delete_note", {"name": "shopping"})
        return AgentResult(text=f"**{'Deleted' if approved else 'Kept'}** it")

    bot = make_bot(config, respond)
    ctx = SimpleNamespace(bot=FakeBot())

    task = asyncio.create_task(bot.on_text(text_update(ME, "delete my shopping note"), ctx))
    while not ctx.bot.sent:  # wait for the Allow/Deny prompt
        await asyncio.sleep(0.01)
    prompt = ctx.bot.sent[0]
    assert "delete_note" in prompt.text and "shopping" in prompt.text
    assert prompt.kwargs["link_preview_options"].is_disabled
    allow = prompt.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

    # A stranger can't press the button for us.
    upd, answers = button_update(STRANGER, allow)
    await bot.on_button(upd, ctx)
    assert answers == ["Not allowed."] and not task.done()

    upd, answers = button_update(ME, allow)
    await bot.on_button(upd, ctx)
    await asyncio.wait_for(task, 2)

    assert prompt.edits == ["Allowed: <b>delete_note</b>"]
    assert ctx.bot.sent[-1].text == "<b>Deleted</b> it"
    assert ctx.bot.sent[-1].kwargs["parse_mode"] == "HTML"

    # Pressing again after it's resolved is harmless.
    upd, answers = button_update(ME, allow)
    await bot.on_button(upd, ctx)
    assert answers == ["This request has expired."]


async def test_confirmation_times_out_as_denied(config):
    async def respond(chat_id, text, confirm):
        return AgentResult(text="Kept it" if not await confirm("x", {}) else "Did it")

    bot = make_bot(config, respond)
    bot.cfg.confirm_timeout_seconds = 0.05
    ctx = SimpleNamespace(bot=FakeBot())
    await bot.on_text(text_update(ME, "do x"), ctx)
    assert ctx.bot.sent[0].edits == ["No answer, so denied: <b>x</b>"]
    assert ctx.bot.sent[-1].text == "Kept it"


async def test_confirmation_shows_long_arguments_in_full(config):
    body = "Hi Sam,\n\n" + "Here's the plan for the weekend. " * 200 + "\nP.S. the door code is 4321."
    args = {"to": "sam@example.com", "body": body}

    async def respond(chat_id, text, confirm):
        return AgentResult(text="Sent" if await confirm("send_email", args) else "Not sent")

    bot = make_bot(config, respond)
    bot.cfg.confirm_timeout_seconds = 0.05
    ctx = SimpleNamespace(bot=FakeBot())
    await bot.on_text(text_update(ME, "email Sam the plan"), ctx)

    *shown, prompt, reply = ctx.bot.sent
    assert len(shown) > 1 and all(len(m.text) <= 4096 for m in shown)
    # Every character is shown before the buttons, including the end of a long argument.
    shown_text = "".join(m.text for m in shown)
    assert shown_text.replace("\n", "") == json.dumps(args, indent=2, ensure_ascii=False).replace("\n", "")
    assert "door code is 4321" in shown_text
    assert "send_email" in prompt.text and "reply_markup" in prompt.kwargs
    assert all(m.kwargs["link_preview_options"].is_disabled for m in [*shown, prompt])
    assert reply.text == "Not sent"


async def test_agent_errors_become_friendly_replies(config):
    import httpx
    import openai

    async def respond(chat_id, text, confirm):
        raise openai.APIConnectionError(request=httpx.Request("POST", "http://mac/v1/chat/completions"))

    bot = make_bot(config, respond)
    ctx = SimpleNamespace(bot=FakeBot())
    await bot.on_text(text_update(ME, "hi"), ctx)
    assert "can't reach the model server" in ctx.bot.sent[-1].text


async def test_setup_mode_tells_stranger_their_id(config):
    bot = make_bot(config, None, allowed=())
    ctx = SimpleNamespace(bot=FakeBot())
    await bot.on_stranger(text_update(STRANGER, "hello"), ctx)
    assert f"user ID is {STRANGER}" in ctx.bot.sent[0].text

    locked = make_bot(config, None)
    ctx2 = SimpleNamespace(bot=FakeBot())
    await locked.on_stranger(text_update(STRANGER, "hello"), ctx2)
    assert ctx2.bot.sent == []  # silently ignored once an allowlist exists
