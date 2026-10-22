"""Offline tests for the Telegram layer using stand-in bot/update objects."""

import asyncio
import json
from types import SimpleNamespace

from telegram.ext import CallbackQueryHandler, MessageHandler

from athena.agent import AgentResult
from athena.approvals import Approvals
from athena.config import TelegramConfig
from athena.history import ConversationStore
from athena.telegram_bot import TelegramBot

ME = 111
STRANGER = 999


class FakeMessage:
    def __init__(self, bot, text, kwargs):
        self.bot, self.text, self.kwargs, self.edits = bot, text, kwargs, []
        self.message_id = len(bot.sent) + 1

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class FakeBot:
    def __init__(self):
        self.sent: list[FakeMessage] = []

    async def send_message(self, chat_id, text, **kwargs):
        msg = FakeMessage(self, text, kwargs)
        msg.chat_id = chat_id
        self.sent.append(msg)
        return msg

    async def send_chat_action(self, chat_id, action):
        pass


def make_bot(config, respond, allowed=(ME,)):
    config.telegram = TelegramConfig(bot_token="123456:TEST", allowed_user_ids=list(allowed))
    services = SimpleNamespace(
        config=config,
        agent=SimpleNamespace(respond=respond),
        rewarm=asyncio.Event(),
        history=ConversationStore(config.db_path),
        approvals=Approvals(),
    )
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
    assert sum(isinstance(h, MessageHandler) for h in handlers) == 4  # your text, photos and files, and strangers

    setup_app = make_bot(config, None, allowed=()).build()
    assert [type(h) for h in setup_app.handlers[0]] == [MessageHandler]


async def test_message_round_trip_with_confirmation(config):
    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
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
    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
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

    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
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

    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
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


async def test_keeps_the_model_server_warm(config):
    calls = []

    async def warm_up(chat_id):
        calls.append(chat_id)
        if len(calls) == 2:
            raise ConnectionError("the Mac is asleep")  # it tries again later

    bot = make_bot(config, None)
    bot.s.agent.warm_up = warm_up
    config.llm.warm_up_minutes = 0.001  # every 60ms
    task = asyncio.create_task(bot._keep_warm())
    try:
        while len(calls) < 3:
            await asyncio.sleep(0.01)
        config.llm.warm_up_minutes = 60
        await asyncio.sleep(0.1)
        settled = len(calls)

        # /reload may change the tools, so it warms up again straight away.
        reloaded = []

        async def reload(reconnect=False):
            reloaded.append(reconnect)
            bot.s.rewarm.set()  # as Services.reload does

        async def reply_text(text):
            pass

        bot.s.reload = reload
        bot.s.mcp = SimpleNamespace(status=lambda: [])
        bot.s.tools = SimpleNamespace(all=lambda: [])
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply_text), effective_chat=SimpleNamespace(id=1)
        )
        await bot.cmd_reload(update, SimpleNamespace(bot=FakeBot()))
        while len(calls) == settled:
            await asyncio.sleep(0.01)

        # It warms up the current session: after a new one, that one.
        bot.s.history.new_session()
        bot.s.rewarm.set()
        while calls[-1] != "session:2":
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
    assert reloaded == [True]  # it reconnects every server
    assert set(calls) == {"session:1", "session:2"}  # your conversation, not Telegram's chat


async def test_confirmation_shows_what_the_tool_says_it_will_do(config):
    args = {"side": "buy", "ticker": "AAPL_US_EQ", "quantity": 2}

    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
        approved = await confirm("trading212_place_order", args, "Buy 2 Apple <Inc> at the market price.")
        return AgentResult(text="Bought" if approved else "Not bought")

    bot = make_bot(config, respond)
    bot.cfg.confirm_timeout_seconds = 0.05
    ctx = SimpleNamespace(bot=FakeBot())
    await bot.on_text(text_update(ME, "buy 2 apple"), ctx)

    prompt = ctx.bot.sent[0]
    # The tool's own account of what it will do comes first, then the exact arguments, as always.
    assert prompt.text.startswith(
        "Allow <b>trading212_place_order</b>?\nBuy 2 Apple &lt;Inc&gt; at the market price.\n<pre>{"
    )
    assert "&quot;ticker&quot;: &quot;AAPL_US_EQ&quot;" in prompt.text
    assert "reply_markup" in prompt.kwargs


async def test_session_starts_a_new_session(config):
    replies = []

    async def reply_text(text):
        replies.append(text)

    bot = make_bot(config, None)
    bot.s.new_session = lambda: SimpleNamespace(id=7)
    update = SimpleNamespace(
        effective_message=SimpleNamespace(reply_text=reply_text), effective_chat=SimpleNamespace(id=1)
    )
    await bot.cmd_session(update, SimpleNamespace(bot=FakeBot()))
    assert replies[0].startswith("Started session 7. I've cleared our conversation from my context.")
    app = bot.build()
    names = {name for h in app.handlers[0] if hasattr(h, "commands") for name in h.commands}
    assert {"session", "reset"} <= names  # /reset still works, for anyone used to it


async def test_you_hear_when_a_servers_tools_change(config):
    from athena.tool_approvals import ToolChange, ToolVersion

    bot = make_bot(config, None)
    fake = FakeBot()
    now = ToolVersion("Add two numbers <b>fast</b>.", {})
    bot._tools_changed(fake, "demo", [ToolChange("add", now, ToolVersion("Add.", {})), ToolChange("drop_<all>", now)])
    while not fake.sent:
        await asyncio.sleep(0.01)
    [msg] = fake.sent
    assert msg.chat_id == ME and msg.kwargs["parse_mode"] == "HTML"
    assert (
        "<b>demo</b> has new or changed tools: <code>add</code> (changed), <code>drop_&lt;all&gt;</code> (new)"
        in msg.text
    )
    assert "approve them on the dashboard" in msg.text


async def test_recall_shows_documents_by_path(config, memory, tmp_path):
    from athena.memory import DOCUMENTS_HOME, IndexedDocument

    bot = make_bot(config, None)
    bot.s.memory = memory
    await memory.remember("George's boiler is serviced every March.")
    vector = (await memory.embedder.embed(["Hartley Heating quoted 95 pounds for the boiler service."], "document"))[0]
    memory.store.set_meta(DOCUMENTS_HOME, "/Users/george")
    memory.store.put_document(
        IndexedDocument("/Users/george/Documents/Boiler quote.md", "~/Documents", 10, 1.0),
        [("Hartley Heating quoted 95 pounds for the boiler service.", vector)],
    )
    ctx = SimpleNamespace(args=["boiler", "service"], bot=FakeBot())
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=ME), effective_message=None)
    await bot.cmd_recall(update, ctx)
    [msg] = ctx.bot.sent
    lines = msg.text.split("\n")
    assert lines[0] == "Memories matching 'boiler service':"
    assert any(line.startswith("📄 ~/Documents/Boiler quote.md (") for line in lines)
    assert any(line.startswith("#1 (") and "serviced every March" in line for line in lines)


def command_update(chat_id=1):
    replies = []

    async def reply_text(text, **kwargs):
        replies.append(SimpleNamespace(text=text, kwargs=kwargs))

    update = SimpleNamespace(
        effective_message=SimpleNamespace(reply_text=reply_text),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=ME),
    )
    return update, replies


def test_the_new_commands_are_in_the_menu(config):
    names = {name for h in make_bot(config, None).build().handlers[0] if hasattr(h, "commands") for name in h.commands}
    assert {"stop", "retry", "think", "compact"} <= names


async def test_compact_says_how_it_went(config):
    bot = make_bot(config, None)
    outcomes = [12, 0, ConnectionError("down")]
    asked = []

    async def compact(chat_id):
        asked.append(chat_id)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    bot.s.agent.compact = compact
    said = []
    for _ in range(3):
        sent = []

        async def reply_text(text, sent=sent, **kwargs):
            message = SimpleNamespace(edits=[])

            async def edit_text(new, **kwargs):
                message.edits.append(new)

            message.edit_text = edit_text
            sent.append((text, message))
            return message

        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply_text), effective_chat=SimpleNamespace(id=1)
        )
        await bot.cmd_compact(update, SimpleNamespace(bot=FakeBot()))
        [(first, message)] = sent
        assert first == "Compacting our conversation…"
        said.append(message.edits[-1])
    assert asked == ["session:1"] * 3  # the conversation, the current session
    assert said[0].startswith("Done: I've summarised the 12 messages before the last few")
    assert said[1].startswith("There's nothing to compact yet")
    assert said[2].startswith("I couldn't compact our conversation:") and said[2].endswith("It's as it was.")


async def test_think_shows_and_changes_how_much_the_model_thinks(config):
    bot = make_bot(config, None)
    saved = []

    def set_thinking(mode):
        saved.append(mode)
        config.llm.thinking = mode
        return True

    bot.s.set_thinking = set_thinking
    ctx = SimpleNamespace(bot=FakeBot(), args=[])
    update, replies = command_update()
    await bot.cmd_think(update, ctx)
    assert replies[-1].text == "Thinking is on: I think things through before answering, for as long as it takes."
    buttons = replies[-1].kwargs["reply_markup"].inline_keyboard[0]
    assert [b.text for b in buttons] == ["✓ On", "Short", "Off"]

    edits, answers = [], []

    async def answer(text=None):
        answers.append(text)

    async def edit_message_text(text, **kwargs):
        edits.append((text, kwargs))

    press = SimpleNamespace(callback_query=SimpleNamespace(data=buttons[1].callback_data, answer=answer))
    press.callback_query.edit_message_text = edit_message_text
    press.effective_user = SimpleNamespace(id=STRANGER)
    await bot.on_think_button(press, ctx)
    assert answers == ["Not allowed."] and saved == []

    press.effective_user = SimpleNamespace(id=ME)
    await bot.on_think_button(press, ctx)
    text, kwargs = edits[-1]
    assert saved == ["short"]
    assert text == "Thinking is short: I think briefly before answering, for up to 1,000 tokens."
    assert [b.text for b in kwargs["reply_markup"].inline_keyboard[0]] == ["On", "✓ Short", "Off"]

    ctx.args = ["OFF"]
    await bot.cmd_think(update, ctx)
    assert saved[-1] == "off" and replies[-1].text.startswith("Thinking is off: I answer straight away")
    ctx.args = ["lots"]
    await bot.cmd_think(update, ctx)
    assert replies[-1].text == "Usage: /think on, /think short or /think off" and saved[-1] == "off"

    bot.s.set_thinking = lambda mode: setattr(config.llm, "thinking", mode)  # returns None: not saved
    ctx.args = ["on"]
    await bot.cmd_think(update, ctx)
    assert replies[-1].text.endswith("Only until I restart: there's no config.toml to save it in.")


async def test_stop_stops_the_answer_in_progress(config):
    started = asyncio.Event()

    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
        started.set()
        await asyncio.Event().wait()  # thinking for ever

    bot = make_bot(config, respond)
    ctx = SimpleNamespace(bot=FakeBot(), args=[])
    answering = asyncio.create_task(bot.on_text(text_update(ME, "think hard"), ctx))
    await asyncio.wait_for(started.wait(), 2)

    update, replies = command_update()
    await bot.cmd_stop(update, ctx)
    await asyncio.wait_for(answering, 2)
    assert replies[-1].text == "Stopped. Send /retry to have me try again."
    assert ctx.bot.sent == []  # no answer, and no error either

    await bot.cmd_stop(update, ctx)
    assert replies[-1].text == "I'm not working on anything."


async def test_stop_while_waiting_for_your_approval(config):
    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
        await confirm("delete_note", {"name": "shopping"})
        return AgentResult(text="Never sent")

    bot = make_bot(config, respond)
    ctx = SimpleNamespace(bot=FakeBot(), args=[])
    answering = asyncio.create_task(bot.on_text(text_update(ME, "delete my shopping note"), ctx))
    while not ctx.bot.sent:
        await asyncio.sleep(0.01)
    prompt = ctx.bot.sent[0]

    await bot.cmd_stop(command_update()[0], ctx)
    await asyncio.wait_for(answering, 2)
    assert prompt.edits == ["Stopped: <b>delete_note</b>"] and len(ctx.bot.sent) == 1
    upd, answers = button_update(ME, prompt.kwargs["reply_markup"].inline_keyboard[0][0].callback_data)
    await bot.on_button(upd, ctx)
    assert answers == ["This request has expired."]


async def test_retry_stops_and_asks_again(config):
    asked = []

    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
        asked.append(text)
        if len(asked) == 1:
            await asyncio.Event().wait()  # the first time it never finishes
        return AgentResult(text=f"Answer {len(asked)}", saved=True)

    async def take_back(chat_id):
        raise AssertionError("an answer that wasn't given isn't taken back")

    bot = make_bot(config, respond)
    bot.s.take_back_last_exchange = take_back
    ctx = SimpleNamespace(bot=FakeBot(), args=[])
    answering = asyncio.create_task(bot.on_text(text_update(ME, "what's 2 + 2?"), ctx))
    while not asked:
        await asyncio.sleep(0.01)

    await bot.cmd_retry(command_update()[0], ctx)  # while it's still working on it
    await asyncio.wait_for(answering, 2)
    assert asked == ["what's 2 + 2?", "what's 2 + 2?"]
    assert [m.text for m in ctx.bot.sent] == ["🔁 Trying again: what's 2 + 2?", "Answer 2"]


async def test_retry_takes_back_the_last_answer_and_asks_again(config):
    asked, taken = [], []

    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
        asked.append(text)
        return AgentResult(text=f"Answer {len(asked)}", saved=True)

    async def take_back(chat_id):
        taken.append(chat_id)
        return SimpleNamespace(question="What's the weather tomorrow?", photos=())

    bot = make_bot(config, respond)
    bot.s.take_back_last_exchange = take_back
    ctx = SimpleNamespace(bot=FakeBot(), args=[])
    await bot.on_text(text_update(ME, "What's the weather tomorrow?"), ctx)
    await bot.cmd_retry(command_update()[0], ctx)
    assert taken == ["session:1"] and asked == ["What's the weather tomorrow?"] * 2
    assert ctx.bot.sent[-1].text == "Answer 2"

    # After a restart, it's the last answer the model sees; in a new session, there isn't one.
    async def nothing(chat_id):
        return None

    fresh = make_bot(config, respond)
    fresh.s.take_back_last_exchange = nothing
    update, replies = command_update()
    await fresh.cmd_retry(update, ctx)
    assert replies[-1].text == "There's nothing to try again in this session."


def task_scheduler(config):
    from athena.config import TasksConfig
    from athena.tasks import Scheduler, TaskStore

    history = ConversationStore(config.db_path)
    agent = SimpleNamespace(answering_you=0, last_answered_you=float("-inf"), history=history)
    return Scheduler(TasksConfig(), TaskStore(config.db_path), agent, timezone="Europe/London",
                     channel=lambda: None, tools=lambda: [])  # fmt: skip


async def test_tasks_lists_them_with_buttons_to_run_pause_and_delete(config):
    bot = make_bot(config, None)
    bot.s.tasks = scheduler = task_scheduler(config)
    ctx = SimpleNamespace(bot=FakeBot(), args=[])
    update, replies = command_update()
    await bot.cmd_tasks(update, ctx)
    assert replies[-1].text.startswith("I've no tasks.")

    scheduler.add("Briefing", "Brief me.", "30 7 * * 1-5", [], created_by="you")
    scheduler.add("Servers", "Find servers.", "0 10 * * sun", [], created_by="athena")
    scheduler.pause(2)
    await bot.cmd_tasks(update, ctx)
    lines = replies[-1].text.split("\n")
    assert lines[0] == "Tasks I do by myself:"
    assert lines[1].startswith("▶️ #1 Briefing: every weekday at 07:30 · next ")
    assert lines[2] == "⏸ #2 Servers: every Sunday at 10:00"
    rows = replies[-1].kwargs["reply_markup"].inline_keyboard
    assert [[b.text for b in row] for row in rows] == [
        ["▶ Run #1", "⏸ Pause", "🗑 Delete"],
        ["▶ Run #2", "⏯ Resume", "🗑 Delete"],
    ]

    edits, answers = [], []

    async def answer(text=None):
        answers.append(text)

    async def edit_message_text(text, **kwargs):
        edits.append(text)

    def press(button, user=ME):
        query = SimpleNamespace(data=button.callback_data, answer=answer, edit_message_text=edit_message_text)
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user))

    await bot.on_task_button(press(rows[0][2], user=STRANGER), ctx)
    assert answers == ["Not allowed."] and len(scheduler.store.all()) == 2
    await bot.on_task_button(press(rows[0][1]), ctx)  # pause #1
    assert answers[-1] == "Paused" and scheduler.store.get(1).status == "paused"
    assert edits[-1].split("\n")[1] == "⏸ #1 Briefing: every weekday at 07:30"
    await bot.on_task_button(press(rows[1][1]), ctx)  # resume #2
    assert scheduler.store.get(2).status == "active"
    await bot.on_task_button(press(rows[1][2]), ctx)  # delete #2
    assert answers[-1] == "Deleted" and scheduler.store.get(2) is None
    await bot.on_task_button(press(rows[1][0]), ctx)  # it's gone
    assert answers[-1] == "There's no task #2."

    bot.s.tasks = None
    await bot.cmd_tasks(update, ctx)
    assert replies[-1].text.startswith("Tasks are switched off")


async def test_tasks_reach_you_through_telegram(config):
    bot = make_bot(config, None)
    bot._bot = FakeBot()
    await bot.tell("📋 **Briefing**\n\nAll quiet.")
    assert bot._bot.sent[-1].text == "📋 <b>Briefing</b>\n\nAll quiet." and bot._bot.sent[-1].chat_id == ME

    asking = asyncio.create_task(
        bot.ask("fetch", {"url": "https://x.example"}, None, why="For your task “News”", timeout=5)
    )
    while len(bot._bot.sent) < 2:
        await asyncio.sleep(0.01)
    prompt = bot._bot.sent[-1]
    assert prompt.text.startswith("🗓️ For your task “News”:\nAllow <b>fetch</b>?")
    upd, _ = button_update(ME, prompt.kwargs["reply_markup"].inline_keyboard[0][0].callback_data)
    await bot.on_button(upd, SimpleNamespace(bot=bot._bot))
    assert await asyncio.wait_for(asking, 2) is True


# -- photos ---------------------------------------------------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + b"a photo" * 10


class PhotoBot(FakeBot):
    """A bot that serves files by id, as Telegram does."""

    def __init__(self, files):
        super().__init__()
        self.files, self.fetched = files, []

    async def get_file(self, file_id):
        self.fetched.append(file_id)

        async def download_as_bytearray():
            return bytearray(self.files[file_id])

        return SimpleNamespace(download_as_bytearray=download_as_bytearray)


def photo_update(user_id, sizes, caption=None, group=None, chat_id=1, document=None):
    photo = [SimpleNamespace(file_id=f, width=w, height=h) for f, w, h in sizes]
    message = SimpleNamespace(photo=photo, caption=caption, media_group_id=group, document=document, text=None)
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id), effective_user=SimpleNamespace(id=user_id), message=message
    )


def asking(answer="It's a cat."):
    asked = []

    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
        asked.append((text, photos))
        return AgentResult(text=answer, saved=True)

    return asked, respond


def test_photos_are_handled_only_from_you(config):
    app = make_bot(config, None).build()
    handlers = [h for h in app.handlers[0] if isinstance(h, MessageHandler)]
    assert len(handlers) == 4  # your text, photos and files, and strangers
    assert [h.callback.__name__ for h in handlers] == ["on_text", "on_photo", "on_file", "on_stranger"]


async def test_a_photo_and_its_caption_go_to_the_model(config):
    asked, respond = asking()
    bot = make_bot(config, respond)
    files = {"small": b"\xff\xd8\xff small", "big": JPEG, "huge": b"\xff\xd8\xff huge"}
    ctx = SimpleNamespace(bot=PhotoBot(files))
    sizes = [("small", 320, 240), ("big", 1280, 960), ("huge", 4000, 3000)]
    await bot.on_photo(photo_update(ME, sizes, caption=" What's this? "), ctx)
    ((text, photos),) = asked
    assert text == "What's this?" and [(p.data, p.mime) for p in photos] == [(JPEG, "image/jpeg")]
    assert ctx.bot.fetched == ["big"]  # the largest copy that isn't huge
    assert ctx.bot.sent[-1].text == "It's a cat."
    assert bot._last[1].photos == photos  # for /retry


async def test_an_album_is_answered_once(config, monkeypatch):
    import athena.telegram_bot as telegram_bot

    monkeypatch.setattr(telegram_bot, "ALBUM_SECONDS", 0.2)
    asked, respond = asking("Two cats.")
    bot = make_bot(config, respond)
    files = {f"p{i}": b"\xff\xd8\xff" + bytes([i]) for i in range(7)}
    ctx = SimpleNamespace(bot=PhotoBot(files))
    for i in range(7):
        caption = "Which is fluffier?" if i == 3 else None
        await bot.on_photo(photo_update(ME, [(f"p{i}", 800, 600)], caption=caption, group="album1"), ctx)
    await asyncio.sleep(0.5)
    ((text, photos),) = asked
    assert text == "Which is fluffier?" and len(photos) == 5
    assert [m.text for m in ctx.bot.sent] == ["I'll look at the first 5 photos, not all of them.", "Two cats."]
    assert bot._albums == {}


async def test_images_sent_as_files_are_checked(config):
    asked, respond = asking()
    bot = make_bot(config, respond)
    png = b"\x89PNG\r\n\x1a\n" + b"pixels"
    files = {"png": png, "pdf": b"%PDF-1.7 not an image"}
    ctx = SimpleNamespace(bot=PhotoBot(files))

    def document(file_id, size=1000):
        return SimpleNamespace(file_id=file_id, file_size=size)

    await bot.on_photo(photo_update(ME, [], document=document("png")), ctx)
    assert asked[-1][1][0].mime == "image/png"
    await bot.on_photo(photo_update(ME, [], document=document("pdf")), ctx)
    assert ctx.bot.sent[-1].text == "I can only look at JPEG, PNG, WebP and GIF images."
    await bot.on_photo(photo_update(ME, [], document=document("png", size=9_000_000)), ctx)
    assert ctx.bot.sent[-1].text.startswith("That image is over 8 MB.")
    assert len(asked) == 1 and ctx.bot.fetched == ["png", "pdf"]  # the big one wasn't downloaded


async def test_a_model_that_cant_take_photos_says_so(config):
    import httpx
    import openai

    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
        request = httpx.Request("POST", "http://llm.test/v1/chat/completions")
        raise openai.BadRequestError(
            "Model does not support images", response=httpx.Response(400, request=request), body=None
        )

    bot = make_bot(config, respond)
    ctx = SimpleNamespace(bot=PhotoBot({"p": JPEG}))
    await bot.on_photo(photo_update(ME, [("p", 800, 600)]), ctx)
    assert "can't take photos" in ctx.bot.sent[-1].text and "doctor" in ctx.bot.sent[-1].text


async def test_retry_sends_the_photos_again(config, tmp_path):
    from athena.photos import Photo, PhotoStore

    asked, respond = asking()
    store = PhotoStore(tmp_path / "photos")
    name = store.keep(Photo(JPEG))

    async def take_back(chat_id):
        return SimpleNamespace(question="[Photo] What's this?", photos=(name, "gone.jpg"))

    bot = make_bot(config, respond)
    bot.s.take_back_last_exchange = take_back
    bot.s.history.photos = store
    ctx = SimpleNamespace(bot=FakeBot(), args=[])
    await bot.cmd_retry(command_update()[0], ctx)
    ((text, photos),) = asked
    assert text == "[Photo] What's this?" and [p.data for p in photos] == [JPEG]  # one that's gone is left out
    assert ctx.bot.sent[0].text == "🔁 Trying again: [Photo] What's this?"


async def test_an_approval_asked_here_can_be_answered_on_the_dashboard(config):
    async def respond(chat_id, text, confirm, allowed=(), note=None, channel=None, photos=None):
        return AgentResult(text="Sent" if await confirm("send_email", {"to": "sam"}) else "Not sent")

    bot = make_bot(config, respond)
    ctx = SimpleNamespace(bot=FakeBot())
    task = asyncio.create_task(bot.on_text(text_update(ME, "email Sam"), ctx))
    while not bot.s.approvals.pending():
        await asyncio.sleep(0.01)
    [approval] = bot.s.approvals.pending()
    assert (approval.tool, approval.session, approval.why) == ("send_email", 1, "")  # your message, in session 1
    assert bot.s.approvals.answer(approval.id, True)  # as the dashboard does
    await asyncio.wait_for(task, 2)
    prompt = ctx.bot.sent[0]
    assert prompt.edits == ["Allowed: <b>send_email</b>"] and ctx.bot.sent[-1].text == "Sent"
    upd, answers = button_update(ME, prompt.kwargs["reply_markup"].inline_keyboard[0][0].callback_data)
    await bot.on_button(upd, ctx)
    assert answers == ["This request has expired."]  # pressing it here afterwards does nothing
