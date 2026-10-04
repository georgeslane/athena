"""The Siri endpoint, called over real HTTP the way the Apple Shortcut calls it."""

import asyncio
import contextlib

import httpx
import pytest
from test_telegram import ME, FakeBot, button_update, make_bot

from pi_assistant.agent import AgentResult
from pi_assistant.config import SiriConfig, TelegramConfig
from pi_assistant.doctor import FAIL, OK, WARN, check_siri
from pi_assistant.siri import VOICE_NOTE, SiriServer
from pi_assistant.status import StatusTracker

TOKEN = "test-" * 4
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@contextlib.asynccontextmanager
async def running(ask, still_working=lambda: "Still working.", timeout=5.0):
    server = SiriServer(
        SiriConfig(enabled=True, port=0, token=TOKEN, reply_timeout_seconds=timeout), ask, still_working
    )
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}") as client:
            yield server, client
    finally:
        await server.stop()


def recording_ask(answer="It's sunny."):
    asked = []

    async def ask(prompt):
        asked.append(prompt)
        return answer

    return ask, asked


async def test_answers_a_question_sent_with_the_token():
    ask, asked = recording_ask()
    async with running(ask) as (_, client):
        response = await client.post("/ask", json={"prompt": "  What's the weather?  "}, headers=AUTH)
    assert response.status_code == 200
    assert response.text == "It's sunny."
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert asked == ["What's the weather?"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token-wrong-token"}, {"Authorization": TOKEN}])
async def test_refuses_requests_without_the_token(headers):
    ask, asked = recording_ask()
    async with running(ask) as (_, client):
        response = await client.post("/ask", json={"prompt": "hi"}, headers=headers)
    assert response.status_code == 401 and "token" in response.text
    assert asked == []


async def test_accepts_form_plain_text_and_chunked_bodies():
    ask, asked = recording_ask()

    async def chunks():
        yield b"split across "
        yield b"several chunks"

    async with running(ask) as (_, client):
        await client.post("/ask", data={"prompt": "from a form"}, headers=AUTH)
        await client.post("/ask", content="just text", headers=AUTH)
        await client.post("/ask", content=chunks(), headers=AUTH)
    assert asked == ["from a form", "just text", "split across several chunks"]


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("GET", "/health", None, 200),
        ("POST", "/health", None, 405),
        ("GET", "/ask", None, 405),
        ("GET", "/elsewhere", None, 404),
        ("POST", "/ask", b'{"prompt": "   "}', 400),
        ("POST", "/ask", b'{"prompt": 42}', 400),
        ("POST", "/ask", b"{not json", 400),
        ("POST", "/ask", b"x" * 20_000, 413),
    ],
)
async def test_other_requests_are_turned_away(method, path, body, status):
    ask, asked = recording_ask()
    headers = {**AUTH, "Content-Type": "application/json"}
    async with running(ask) as (_, client):
        response = await client.request(method, path, content=body, headers=headers)
    assert response.status_code == status
    assert asked == []


@pytest.mark.parametrize(
    "request_bytes", [b"nonsense\r\n\r\n", b"GET /health SPDY/3\r\n\r\n", b"GET / HTTP/1.1\r\nno colon\r\n\r\n"]
)
async def test_malformed_requests_get_a_400(request_bytes):
    ask, _ = recording_ask()
    async with running(ask) as (server, _):
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(request_bytes)
        await writer.drain()
        assert (await reader.read()).startswith(b"HTTP/1.1 400 ")
        writer.close()


async def test_slow_answers_carry_on_after_siri_is_told():
    finished = asyncio.Event()

    async def ask(prompt):
        await asyncio.sleep(0.3)
        finished.set()
        return "Done at last."

    async with running(ask, timeout=0.05) as (_, client):
        response = await client.post("/ask", json={"prompt": "something slow"}, headers=AUTH)
        assert response.text == "Still working."
        await asyncio.wait_for(finished.wait(), 2)  # the answer still arrives (in Telegram)


async def test_a_failed_answer_is_a_500_with_something_siri_can_say():
    async def ask(prompt):
        raise RuntimeError("boom")

    async with running(ask) as (_, client):
        response = await client.post("/ask", json={"prompt": "hi"}, headers=AUTH)
    assert response.status_code == 500 and "Try asking in Telegram" in response.text


async def test_stopping_cancels_unfinished_answers():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def ask(prompt):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async with running(ask, timeout=0.05) as (_, client):
        await client.post("/ask", json={"prompt": "hi"}, headers=AUTH)
        await started.wait()
    assert cancelled.is_set()


def test_needs_a_long_enough_token():
    with pytest.raises(ValueError, match="at least 16"):
        SiriServer(SiriConfig(enabled=True, token="short"), recording_ask()[0], str)


# -- with the Telegram bot ----------------------------------------------------------------


def siri_bot(config, respond, **siri):
    bot = make_bot(config, respond)
    bot.s.status = StatusTracker()
    config.siri = SiriConfig(enabled=True, port=0, token=TOKEN, **siri)
    return bot


async def test_question_and_answer_are_posted_to_the_chat(config):
    seen = {}

    async def respond(chat_id, text, confirm, note=None):
        seen.update(chat_id=chat_id, text=text, note=note)
        return AgentResult(text="**Sunny** and 21°C. See [the forecast](https://example.com/weather).")

    bot = siri_bot(config, respond)
    telegram = FakeBot()
    speech = await bot.ask_from_siri(telegram, "What's the weather <today>?")

    assert speech == "Sunny and 21°C. See the forecast."
    question, answer = telegram.sent
    assert question.text == "🎙️ <b>You, via Siri</b>\nWhat's the weather &lt;today&gt;?"
    assert "<b>Sunny</b>" in answer.text
    assert answer.kwargs["reply_parameters"].message_id == question.message_id
    # Same conversation as typing in Telegram, with a note that the reply will be spoken.
    assert seen == {"chat_id": str(ME), "text": "What's the weather <today>?", "note": VOICE_NOTE}


async def test_answer_still_reaches_siri_when_telegram_is_down(config):
    from telegram.error import NetworkError

    async def respond(chat_id, text, confirm, note=None):
        return AgentResult(text="Sunny.")

    class DownBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            raise NetworkError("no route to Telegram")

    assert await siri_bot(config, respond).ask_from_siri(DownBot(), "Weather?") == "Sunny."


async def test_approvals_happen_in_telegram_while_siri_is_told(config):
    async def respond(chat_id, text, confirm, note=None):
        task = bot.s.status.begin(text)
        with task.approval("fetch"):
            approved = await confirm("fetch", {"url": "https://example.com"})
        task.finish()
        return AgentResult(text="Fetched it." if approved else "Didn't fetch it.")

    bot = siri_bot(config, respond, reply_timeout_seconds=0.2)
    telegram = FakeBot()
    await bot._start_siri(telegram)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{bot._siri.port}") as client:
            response = await client.post("/ask", json={"prompt": "Read example.com"}, headers=AUTH)
        assert response.text == "I need your OK in Telegram first, and I'll answer there."

        question, prompt = telegram.sent
        allow = prompt.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        update, _ = button_update(ME, allow)
        await bot.on_button(update, None)
        while len(telegram.sent) < 3:
            await asyncio.sleep(0.01)
        assert telegram.sent[2].text == "Fetched it."
        assert telegram.sent[2].kwargs["reply_parameters"].message_id == question.message_id
    finally:
        await bot._siri.stop()


async def test_slow_answers_tell_siri_to_look_in_telegram(config):
    bot = siri_bot(config, None)
    assert bot._still_working() == "That's taking a while, so I'll send the answer to Telegram."


@pytest.mark.parametrize(("token", "allowed"), [("short", [ME]), (TOKEN, [])])
async def test_bot_runs_without_siri_if_it_isnt_set_up(config, token, allowed):
    bot = siri_bot(config, None)
    config.siri.token = token
    bot.cfg = config.telegram = TelegramConfig(bot_token="123456:TEST", allowed_user_ids=allowed)
    bot.allowed = set(allowed)
    await bot._start_siri(FakeBot())
    assert bot._siri is None


async def test_doctor_checks_siri(config):
    marks = []

    def report(mark, msg):
        marks.append((mark, msg))

    config.telegram.allowed_user_ids = [ME]
    async with running(recording_ask()[0]) as (server, _):
        config.siri = SiriConfig(enabled=True, port=server.port, token=TOKEN)
        await check_siri(config, report)
    assert [m for m, _ in marks] == [OK]

    marks.clear()
    config.siri.token, config.telegram.allowed_user_ids = "", []
    await check_siri(config, report)  # and it's no longer running
    assert [m for m, _ in marks] == [FAIL, FAIL, WARN]
