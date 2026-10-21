"""Telegram front end. Uses long polling, so the Pi needs no open ports."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import openai
from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from pi_assistant.app import Services
from pi_assistant.config import THINKING, ConfigError
from pi_assistant.formatting import TELEGRAM_LIMIT, markdown_to_speech, markdown_to_telegram_html, split_message
from pi_assistant.memory import Found
from pi_assistant.photos import MAX_BYTES, MAX_PER_MESSAGE, Photo, label, mime_of
from pi_assistant.siri import VOICE_NOTE, SiriServer
from pi_assistant.status import State, describe_error
from pi_assistant.tool_approvals import ToolChange

log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
THINKING_SAYS = {
    "on": "I think things through before answering, for as long as it takes.",
    "short": "I think briefly before answering, for up to {tokens} tokens.",
    "off": "I answer straight away, without thinking first.",
}


@dataclass
class _Asked:
    """The last message in a chat, for /retry."""

    text: str
    answered: bool = False
    photos: list[Photo] = field(default_factory=list)


@dataclass
class _Album:
    """Photos sent together, which arrive as a message each, gathered to be answered as one."""

    chat_id: int
    photos: list[Photo] = field(default_factory=list)
    caption: str = ""
    timer: asyncio.Task[None] | None = None


class _NotAPhoto(Exception):
    """An image Athena can't take. The message says why, for the chat."""


ALBUM_SECONDS = 1.5  # after an album's last photo arrives, how long to wait for more
MAX_SIDE = 2048  # of the copies Telegram keeps of a photo, the largest up to this many pixels is used


COMMANDS = [
    ("session", "Start a new session: clears our conversation (memories are kept)"),
    ("compact", "Summarise our conversation so far, keeping the last few messages, to make room"),
    ("stop", "Stop the answer I'm working on"),
    ("retry", "Answer your last message again"),
    ("think", "Think before answering: /think on, short or off"),
    ("tasks", "What I do by myself: run, pause or delete it"),
    ("remember", "Save a fact: /remember <text>"),
    ("recall", "Search memory: /recall <query>"),
    ("forget", "Delete a memory: /forget <id>"),
    ("tools", "List available tools and MCP servers"),
    ("reload", "Reconnect to MCP servers"),
    ("status", "Check the model, memory and tools"),
]


class TelegramBot:
    def __init__(self, services: Services):
        self.s = services
        self.cfg = services.config.telegram
        self.allowed = set(self.cfg.allowed_user_ids)
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._siri: SiriServer | None = None
        self._warming: asyncio.Task[None] | None = None
        self._telling: set[asyncio.Task[None]] = set()
        self._answering: dict[int, set[asyncio.Task[Any]]] = {}  # for each chat, replies /stop can stop
        self._last: dict[int, _Asked] = {}
        self._albums: dict[str, _Album] = {}  # by Telegram's media group id
        self._replying: set[asyncio.Task[None]] = set()  # albums being answered
        self._bot: Bot | None = None

    # -- setup ----------------------------------------------------------------------------

    def build(self) -> Application:
        if not self.cfg.bot_token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Add it to .env (see .env.example).")
        app = (
            ApplicationBuilder()
            .token(self.cfg.bot_token)
            # Needed so a button press can arrive while a message is waiting for confirmation.
            .concurrent_updates(True)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        if self.allowed:
            me = filters.User(user_id=list(self.allowed))
            app.add_handler(CommandHandler("start", self.cmd_start, filters=me))
            app.add_handler(CommandHandler("help", self.cmd_start, filters=me))
            app.add_handler(CommandHandler(["session", "reset"], self.cmd_session, filters=me))
            app.add_handler(CommandHandler("compact", self.cmd_compact, filters=me))
            app.add_handler(CommandHandler("stop", self.cmd_stop, filters=me))
            app.add_handler(CommandHandler("retry", self.cmd_retry, filters=me))
            app.add_handler(CommandHandler("think", self.cmd_think, filters=me))
            app.add_handler(CommandHandler("tasks", self.cmd_tasks, filters=me))
            app.add_handler(CommandHandler("remember", self.cmd_remember, filters=me))
            app.add_handler(CommandHandler("recall", self.cmd_recall, filters=me))
            app.add_handler(CommandHandler("forget", self.cmd_forget, filters=me))
            app.add_handler(CommandHandler("tools", self.cmd_tools, filters=me))
            app.add_handler(CommandHandler("reload", self.cmd_reload, filters=me))
            app.add_handler(CommandHandler("status", self.cmd_status, filters=me))
            app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, self.on_text))
            app.add_handler(MessageHandler(me & (filters.PHOTO | filters.Document.IMAGE), self.on_photo))
            app.add_handler(CallbackQueryHandler(self.on_button, pattern=r"^confirm:"))
            app.add_handler(CallbackQueryHandler(self.on_think_button, pattern=r"^think:"))
            app.add_handler(CallbackQueryHandler(self.on_task_button, pattern=r"^task:"))
        app.add_handler(MessageHandler(filters.ALL, self.on_stranger))
        app.add_error_handler(self.on_error)
        return app

    async def _post_init(self, app: Application) -> None:
        self.s.mcp.listeners.append(lambda server, changes: self._tools_changed(app.bot, server, changes))
        if self.allowed:
            self._bot = app.bot
            self.s.channel = self  # how tasks reach you
        await self.s.start()
        for st in self.s.mcp.status():
            log.info("MCP %s: %s", st.name, f"{st.tools} tools" if st.connected else st.error)
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except TelegramError as exc:
            log.warning("Couldn't set the bot's command menu: %s", exc)
        me = await app.bot.get_me()
        if self.allowed:
            log.info("Telegram bot @%s ready for user(s) %s", me.username, sorted(self.allowed))
        else:
            log.warning("@%s is in setup mode: message it to get your user ID.", me.username)
        if self.s.config.siri.enabled:
            await self._start_siri(app.bot)
        if self.s.config.llm.warm_up_minutes > 0:
            self._warming = asyncio.create_task(self._keep_warm(), name="keep-warm")

    async def _post_shutdown(self, app: Application) -> None:
        if self._warming:
            self._warming.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._warming
        if self._siri:
            await self._siri.stop()
        await self.s.close()

    def _tools_changed(self, bot: Bot, server: str, changes: list[ToolChange]) -> None:
        """Tell you that a server's tools changed, so they're held back until you approve them."""
        if not self.cfg.allowed_user_ids:
            return
        tools = ", ".join(f"<code>{html.escape(c.tool)}</code> ({'changed' if c.approved else 'new'})" for c in changes)
        text = (
            f"⚠️ <b>{html.escape(server)}</b> has new or changed tools: {tools}. Athena won't use them until "
            "you approve them on the dashboard's Tools tab, where you can see what changed."
        )
        task = asyncio.create_task(self._tell(bot, text), name="tools-changed")
        self._telling.add(task)
        task.add_done_callback(self._telling.discard)

    async def _tell(self, bot: Bot, text: str) -> None:
        try:
            await bot.send_message(self.cfg.allowed_user_ids[0], text, parse_mode=ParseMode.HTML)
        except TelegramError as exc:
            log.warning("Couldn't send a message about changed tools: %s", exc)

    async def _keep_warm(self) -> None:
        """Keep the model server's prompt cache ready for your chat (llm.warm_up_minutes)."""
        chat_id = str(self.cfg.allowed_user_ids[0]) if self.cfg.allowed_user_ids else "warm-up"
        while True:
            try:
                reply = await self.s.agent.warm_up(chat_id)
                if reply and reply.elapsed > 5:
                    log.info(
                        "The model server had to read the prompt afresh (%s tokens, %.0fs)",
                        reply.prompt_tokens,
                        reply.elapsed,
                    )
            except Exception as exc:  # the Mac might be asleep: try again next time
                log.debug("Couldn't warm up the model server: %s", exc)
            with contextlib.suppress(TimeoutError):  # after a reply, or a change to the tools or session
                await asyncio.wait_for(self.s.rewarm.wait(), self.s.config.llm.warm_up_minutes * 60)
            self.s.rewarm.clear()

    async def _start_siri(self, bot: Bot) -> None:
        if not self.allowed:
            log.warning("Siri is turned on, but it needs telegram.allowed_user_ids to know which chat to use")
            return
        try:
            self._siri = SiriServer(
                self.s.config.siri, lambda prompt: self.ask_from_siri(bot, prompt), self._still_working
            )
            await self._siri.start()
        except (ValueError, OSError) as exc:
            self._siri = None
            log.error("Siri is turned on, but its endpoint couldn't start: %s", exc)

    def run(self) -> None:
        self.build().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)

    # -- helpers ----------------------------------------------------------------------------

    async def _send(self, bot: Bot, chat_id: int, markdown: str, reply_to: int | None = None) -> None:
        for chunk in split_message(markdown):
            # Only the first part is shown as a reply.
            reply = ReplyParameters(reply_to, allow_sending_without_reply=True) if reply_to else None
            reply_to = None
            try:
                await bot.send_message(
                    chat_id,
                    markdown_to_telegram_html(chunk),
                    parse_mode=ParseMode.HTML,
                    link_preview_options=NO_PREVIEW,
                    reply_parameters=reply,
                )
            except BadRequest:  # our HTML conversion produced something Telegram rejects
                await bot.send_message(chat_id, chunk, link_preview_options=NO_PREVIEW, reply_parameters=reply)

    async def _keep_typing(self, bot: Bot, chat_id: int) -> None:
        try:
            while True:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(4.5)
        except asyncio.CancelledError:
            pass
        except TelegramError:
            pass

    async def _confirm(
        self,
        bot: Bot,
        chat_id: int,
        tool: str,
        args: dict[str, Any],
        summary: str | None = None,
        why: str = "",
        timeout: float | None = None,
    ) -> bool:
        key = secrets.token_hex(6)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        # Show every argument: approving content you can't see isn't approval. Link previews
        # stay off, or Telegram's servers would fetch a URL in the arguments before you decide.
        # The tool's own summary of what it will do, if it has one, goes first.
        pretty = json.dumps(args, indent=2, ensure_ascii=False)
        said = f"\n{html.escape(summary)}" if summary else ""
        reason = f"🗓️ {html.escape(why)}:\n" if why else ""
        prompt = f"{reason}Allow <b>{html.escape(tool)}</b>?{said}\n<pre>{html.escape(pretty)}</pre>"
        if len(prompt) > TELEGRAM_LIMIT:
            chunks = split_message(pretty)
            for chunk in chunks:
                await bot.send_message(chat_id, chunk, link_preview_options=NO_PREVIEW)
            prompt = (
                f"{reason}Allow <b>{html.escape(tool)}</b> with the arguments in the {len(chunks)} messages above?"
                f"{said}"
            )
        buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Allow", callback_data=f"confirm:{key}:yes"),
                    InlineKeyboardButton("Deny", callback_data=f"confirm:{key}:no"),
                ]
            ]
        )
        msg = await bot.send_message(
            chat_id, prompt, parse_mode=ParseMode.HTML, reply_markup=buttons, link_preview_options=NO_PREVIEW
        )
        try:
            approved = await asyncio.wait_for(future, timeout=timeout or self.cfg.confirm_timeout_seconds)
            verdict = "Allowed" if approved else "Denied"
        except TimeoutError:
            approved, verdict = False, "No answer, so denied"
        except asyncio.CancelledError:  # /stop
            await self._settle(msg, f"Stopped: <b>{html.escape(tool)}</b>")
            raise
        finally:
            self._pending.pop(key, None)
        await self._settle(msg, f"{verdict}: <b>{html.escape(tool)}</b>")
        return approved

    async def _settle(self, msg: Any, text: str) -> None:
        """Replace an approval's buttons with what became of it."""
        try:
            await msg.edit_text(text, parse_mode=ParseMode.HTML)
        except TelegramError:
            pass

    # -- reaching you when you haven't just asked something (channel.py) ----------------------

    @property
    def chat_id(self) -> str:
        return str(self.cfg.allowed_user_ids[0])  # a private chat with the bot has the user's ID

    async def tell(self, text: str) -> None:
        assert self._bot is not None
        await self._send(self._bot, self.cfg.allowed_user_ids[0], text)

    async def ask(self, tool: str, args: dict[str, Any], summary: str | None, *, why: str, timeout: float) -> bool:
        assert self._bot is not None
        return await self._confirm(self._bot, self.cfg.allowed_user_ids[0], tool, args, summary, why, timeout)

    # -- handlers -----------------------------------------------------------------------------

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.effective_chat and update.message and update.message.text
        chat_id = update.effective_chat.id
        reply = await self._reply(ctx.bot, chat_id, update.message.text)
        if reply is not None:
            await self._send(ctx.bot, chat_id, reply)

    async def on_photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """A photo, or an image sent as a file, with its caption as what you're asking. Photos sent together
        arrive one by one, so they're gathered for a moment and answered as one message."""
        assert update.effective_chat and update.message
        msg, chat_id = update.message, update.effective_chat.id
        try:
            photo = await self._download(ctx.bot, msg)
        except _NotAPhoto as exc:
            await self._send(ctx.bot, chat_id, str(exc))
            return
        caption = (msg.caption or "").strip()
        if not msg.media_group_id:
            await self._answer_photos(ctx.bot, chat_id, caption, [photo])
            return
        album = self._albums.setdefault(msg.media_group_id, _Album(chat_id))
        album.photos.append(photo)
        album.caption = album.caption or caption  # Telegram puts the caption on one of them
        if album.timer:
            album.timer.cancel()
        album.timer = asyncio.create_task(self._album_done(ctx.bot, msg.media_group_id))
        self._replying.add(album.timer)
        album.timer.add_done_callback(self._replying.discard)

    async def _album_done(self, bot: Bot, group: str) -> None:
        await asyncio.sleep(ALBUM_SECONDS)
        album = self._albums.pop(group)  # a photo arriving after this starts another
        photos, left_out = album.photos[:MAX_PER_MESSAGE], len(album.photos) - MAX_PER_MESSAGE
        if left_out > 0:
            await self._send(bot, album.chat_id, f"I'll look at the first {MAX_PER_MESSAGE} photos, not all of them.")
        await self._answer_photos(bot, album.chat_id, album.caption, photos)

    async def _answer_photos(self, bot: Bot, chat_id: int, caption: str, photos: list[Photo]) -> None:
        reply = await self._reply(bot, chat_id, caption, photos=photos)
        if reply is not None:
            await self._send(bot, chat_id, reply)

    async def _download(self, bot: Bot, msg: Any) -> Photo:
        """A message's photo or image, as Telegram has it. _NotAPhoto if it's one Athena can't take."""
        if msg.photo:
            # Telegram keeps a photo at several sizes, smallest first: the largest that isn't huge will do.
            fitting = [p for p in msg.photo if max(p.width, p.height) <= MAX_SIDE]
            file_id = (fitting or msg.photo[:1])[-1].file_id
        else:
            if (msg.document.file_size or 0) > MAX_BYTES:
                raise _NotAPhoto(
                    f"That image is over {MAX_BYTES // 1_000_000} MB. Send it as a photo instead, "
                    "and Telegram makes it smaller."
                )
            file_id = msg.document.file_id
        data = bytes(await (await bot.get_file(file_id)).download_as_bytearray())
        mime = mime_of(data)
        if mime is None:
            raise _NotAPhoto("I can only look at JPEG, PNG, WebP and GIF images.")
        return Photo(data, mime)

    async def ask_from_siri(self, bot: Bot, prompt: str) -> str:
        """Answer a question asked through Siri. Both are posted to the chat; the answer is returned as speech."""
        chat_id = self.cfg.allowed_user_ids[0]  # a private chat with the bot has the user's ID
        question: int | None = None
        try:
            msg = await bot.send_message(
                chat_id, f"🎙️ <b>You, via Siri</b>\n{html.escape(prompt, quote=False)}", parse_mode=ParseMode.HTML
            )
            question = msg.message_id
        except TelegramError as exc:
            log.warning("Couldn't post the question from Siri to Telegram: %s", exc)
        reply = await self._reply(bot, chat_id, prompt, note=VOICE_NOTE, channel="Siri")
        if reply is None:
            return "You stopped that in Telegram."
        try:
            await self._send(bot, chat_id, reply, reply_to=question)
        except TelegramError as exc:  # Siri still gets the answer
            log.warning("Couldn't post the answer for Siri to Telegram: %s", exc)
        return markdown_to_speech(reply)

    def _still_working(self) -> str:
        """What Siri says when the answer isn't ready in time."""
        if self.s.status.snapshot().state is State.APPROVAL:
            return "I need your OK in Telegram first, and I'll answer there."
        return "That's taking a while, so I'll send the answer to Telegram."

    async def _reply(
        self,
        bot: Bot,
        chat_id: int,
        text: str,
        note: str | None = None,
        channel: str = "Telegram",
        photos: list[Photo] | None = None,
    ) -> str | None:
        """The agent's reply to ``text``, and ``photos`` with it, showing "typing..." and asking for approvals in
        the chat meanwhile.

        None if /stop stopped it.
        """
        typing = asyncio.create_task(self._keep_typing(bot, chat_id))
        asked = self._last[chat_id] = _Asked(text, photos=list(photos or []))

        async def confirm(tool: str, args: dict[str, Any], summary: str | None = None) -> bool:
            return await self._confirm(bot, chat_id, tool, args, summary)

        answering = asyncio.create_task(
            self.s.agent.respond(str(chat_id), text, confirm=confirm, note=note, channel=channel, photos=photos)
        )
        self._answering.setdefault(chat_id, set()).add(answering)
        try:
            await asyncio.wait({answering})
            if answering.cancelled():
                log.info("Stopped answering, as asked")
                return None
            result = answering.result()
            asked.answered = result.saved
            reply = result.text
            log.info(
                "Replied in %.1fs (%d model calls, tools: %s)",
                result.elapsed,
                result.model_calls,
                ", ".join(result.tools_used) or "none",
            )
        except openai.APITimeoutError:
            reply = f"The model took longer than {self.s.config.llm.timeout_seconds:.0f}s to answer, so I gave up."
        except openai.APIConnectionError:
            reply = (
                f"I can't reach the model server at {self.s.config.llm.base_url}. Is the Mac awake and oMLX running?"
            )
        except openai.APIStatusError as exc:
            reply = f"The model server returned an error ({exc.status_code}): {exc.message}"
            if photos:
                reply += "\nMaybe the model can't take photos: `uv run pi-assistant doctor` checks."
        except Exception as exc:
            log.exception("Agent failed")
            reply = f"Something went wrong: {type(exc).__name__}: {exc}"
        finally:
            typing.cancel()
            self._answering[chat_id].discard(answering)
            answering.cancel()  # if this was cancelled itself, say as Athena shuts down
        return reply

    async def on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        assert query is not None
        if not update.effective_user or update.effective_user.id not in self.allowed:
            await query.answer("Not allowed.")
            return
        _, key, choice = (query.data or "::").split(":", 2)
        future = self._pending.get(key)
        if future and not future.done():
            future.set_result(choice == "yes")
            await query.answer()
        else:
            await query.answer("This request has expired.")

    async def on_stranger(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or not update.effective_chat:
            return
        if not self.allowed:
            await ctx.bot.send_message(
                update.effective_chat.id,
                f"Setup mode. Your Telegram user ID is {user.id}.\n"
                "Add it to telegram.allowed_user_ids in config.toml, then restart the service.",
            )
        log.warning("Ignored message from unauthorised user %s (@%s)", user.id, user.username)

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Telegram handler error", exc_info=ctx.error)

    # -- commands -------------------------------------------------------------------------------

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        lines = [f"Hi! I'm {self.s.config.agent.assistant_name}. Just send me a message.", ""]
        lines += [f"/{c} - {d}" for c, d in COMMANDS]
        await update.effective_message.reply_text("\n".join(lines))  # type: ignore[union-attr]

    async def cmd_session(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        session = self.s.new_session()
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Started session {session.id}. I've cleared our conversation from my context. "
            "Memories are kept, and I can still search what we said before."
        )

    async def cmd_compact(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        assert message is not None
        said = await message.reply_text("Compacting our conversation…")
        try:
            summarised = await self.s.agent.compact(str(update.effective_chat.id))  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("Couldn't compact the conversation: %s", exc)
            text = f"I couldn't compact our conversation: {describe_error(exc)}. It's as it was."
        else:
            text = (
                f"Done: I've summarised the {summarised} messages before the last few, and kept them as they were."
                if summarised
                else "There's nothing to compact yet: I see only the last few messages as they are."
            )
        with contextlib.suppress(TelegramError):
            await said.edit_text(text)

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        stopped = await self._stop(update.effective_chat.id)  # type: ignore[union-attr]
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            "Stopped. Send /retry to have me try again." if stopped else "I'm not working on anything."
        )

    async def _stop(self, chat_id: int) -> bool:
        """Stop answering in this chat, including messages waiting their turn. False if there weren't any."""
        answering = set(self._answering.get(chat_id, ()))  # a copy, as each one leaves the set as it stops
        for task in answering:
            task.cancel()
        if answering:
            await asyncio.wait(answering)
        return bool(answering)

    async def cmd_retry(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id  # type: ignore[union-attr]
        await self._stop(chat_id)
        last = self._last.get(chat_id)
        if last and not last.answered:  # stopped, or it went wrong
            text, photos = last.text, last.photos
        elif exchange := await self.s.take_back_last_exchange(str(chat_id)):
            text, photos = exchange.question, self._kept_photos(exchange.photos)  # its text says [Photo]
        else:
            await update.effective_message.reply_text("There's nothing to try again in this session.")  # type: ignore[union-attr]
            return
        shown = text if len(text) <= 200 else text[:199].rstrip() + "…"
        if photos and not shown.startswith(label(len(photos))):
            shown = f"{label(len(photos))} {shown}".rstrip()
        await ctx.bot.send_message(chat_id, f"🔁 Trying again: {shown}", link_preview_options=NO_PREVIEW)
        reply = await self._reply(ctx.bot, chat_id, text, photos=photos)
        if reply is not None:
            await self._send(ctx.bot, chat_id, reply)

    def _kept_photos(self, names: tuple[str, ...]) -> list[Photo]:
        return [photo for name in names if (photo := self.s.history.photos.load(name))]

    async def cmd_think(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        choice = " ".join(ctx.args or []).strip().lower()
        if choice:
            if choice not in THINKING:
                await update.effective_message.reply_text("Usage: /think on, /think short or /think off")  # type: ignore[union-attr]
                return
            await update.effective_message.reply_text(self._set_thinking(choice))  # type: ignore[union-attr]
            return
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            self._thinking_says(), reply_markup=self._thinking_buttons()
        )

    async def on_think_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        assert query is not None
        if not update.effective_user or update.effective_user.id not in self.allowed:
            await query.answer("Not allowed.")
            return
        choice = (query.data or "").partition(":")[2]
        if choice not in THINKING:
            await query.answer()
            return
        said = self._set_thinking(choice)
        await query.answer()
        with contextlib.suppress(TelegramError):  # say, pressing the one that's already chosen
            await query.edit_message_text(said, reply_markup=self._thinking_buttons())

    def _set_thinking(self, mode: str) -> str:
        try:
            saved = self.s.set_thinking(mode)
        except Exception as exc:  # such as config.toml being invalid
            log.warning("Couldn't change thinking: %s", exc)
            return f"I couldn't change that: {exc}"
        return self._thinking_says() + ("" if saved else " Only until I restart: there's no config.toml to save it in.")

    def _thinking_says(self) -> str:
        llm = self.s.config.llm
        return f"Thinking is {llm.thinking_now}: " + THINKING_SAYS[llm.thinking_now].format(
            tokens=f"{llm.short_thinking_tokens:,}"
        )

    def _thinking_buttons(self) -> InlineKeyboardMarkup:
        now = self.s.config.llm.thinking_now
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(("✓ " if mode == now else "") + mode.title(), callback_data=f"think:{mode}")
                    for mode in THINKING
                ]
            ]
        )

    async def cmd_tasks(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text, buttons = self._tasks_message()
        await update.effective_message.reply_text(text, reply_markup=buttons)  # type: ignore[union-attr]

    def _tasks_message(self) -> tuple[str, InlineKeyboardMarkup | None]:
        scheduler = self.s.tasks
        if scheduler is None:
            return "Tasks are switched off ([tasks] enabled = false in config.toml).", None
        tasks = [scheduler.describe(t) for t in scheduler.store.all()]
        if not tasks:
            return (
                "I've no tasks. Ask me to do something regularly, or in the background, such as "
                "“every weekday at 7:30 send me a briefing”. You can also add them on the dashboard.",
                None,
            )
        lines, rows = ["Tasks I do by myself:"], []
        for t in tasks:
            mark = {"active": "▶️", "paused": "⏸", "done": "✓", "running": "⚙️"}.get(t["status"], "")
            nxt = ""
            if t["status"] == "running":
                nxt = " · working on it now"
            elif t["waiting_for_you"]:
                nxt = " · due, waiting until you've finished chatting"
            elif t["next_run"]:
                when = datetime.fromtimestamp(t["next_run"], scheduler.tz)
                nxt = f" · next {when:%a %-d %b %H:%M}"
            last = f" · last: {t['last_outcome']}" if t["last_outcome"] else ""
            lines.append(f"{mark} #{t['id']} {t['title']}: {t['when']}{nxt}{last}")
            row = [InlineKeyboardButton(f"▶ Run #{t['id']}", callback_data=f"task:run:{t['id']}")]
            if t["status"] == "running":
                row = [InlineKeyboardButton(f"⏹ Stop #{t['id']}", callback_data=f"task:stop:{t['id']}")]
            elif t["status"] == "active":
                row.append(InlineKeyboardButton("⏸ Pause", callback_data=f"task:pause:{t['id']}"))
            else:
                row.append(InlineKeyboardButton("⏯ Resume", callback_data=f"task:resume:{t['id']}"))
            row.append(InlineKeyboardButton("🗑 Delete", callback_data=f"task:delete:{t['id']}"))
            rows.append(row)
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    async def on_task_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        assert query is not None
        if not update.effective_user or update.effective_user.id not in self.allowed:
            await query.answer("Not allowed.")
            return
        _, action, task_id = (query.data or "::").split(":", 2)
        scheduler = self.s.tasks
        done = {"run": "Running it now", "pause": "Paused", "resume": "Resumed", "stop": "Stopped", "delete": "Deleted"}
        try:
            if scheduler is None or action not in done:
                raise ValueError("That's not something I can do.")
            if action == "stop":
                scheduler.stop_running()
            else:
                getattr(scheduler, {"run": "run_now"}.get(action, action))(int(task_id))
        except ValueError as exc:
            await query.answer(str(exc))
            return
        await query.answer(done[action])
        text, buttons = self._tasks_message()
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(text, reply_markup=buttons)

    async def cmd_remember(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(ctx.args or []).strip()
        if not text:
            await update.effective_message.reply_text("Usage: /remember <fact>")  # type: ignore[union-attr]
            return
        memory_id, created = await self.s.memory.remember(text, source="telegram")
        msg = f"Saved as memory #{memory_id}." if created else f"I already knew that (memory #{memory_id})."
        await update.effective_message.reply_text(msg)  # type: ignore[union-attr]

    async def cmd_recall(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = " ".join(ctx.args or []).strip()
        memory = self.s.memory
        if query:
            found = await memory.find(query, 8)
            header = f"Memories matching '{query}':"
        else:
            found = [Found("fact", 1.0, h.text, id=h.id) for h in memory.store.recent(10)]
            header = "Most recent memories:"
        if not found:
            await update.effective_message.reply_text("Nothing found.")  # type: ignore[union-attr]
            return
        lines = [header]
        for f in found:
            text = f.text if len(f.text) < 300 else f.text[:300] + "..."
            score = f" ({1 - f.distance:.2f})" if query else ""
            # A document goes by its path: its passages come and go with it when you index, so /forget isn't for them.
            label = f"📄 {memory.show_path(f.path)}" if f.kind == "document" and f.path else f"#{f.id}"
            lines.append(f"{label}{score} {text}")
        await self._send(ctx.bot, update.effective_chat.id, "\n".join(lines))  # type: ignore[union-attr]

    async def cmd_forget(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            memory_id = int((ctx.args or [""])[0].lstrip("#"))
        except ValueError:
            await update.effective_message.reply_text("Usage: /forget <id> (find ids with /recall)")  # type: ignore[union-attr]
            return
        ok = self.s.memory.forget(memory_id)
        await update.effective_message.reply_text(  # type: ignore[union-attr]
            f"Forgot memory #{memory_id}." if ok else f"There's no memory #{memory_id}."
        )

    async def cmd_tools(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        lines = ["Tools:"]
        for tool in self.s.tools.all():
            flag = " (asks first)" if tool.needs_confirmation else ""
            lines.append(f"- {tool.name} [{tool.source}]{flag}")
        lines.append("")
        lines.append("MCP servers:")
        for st in self.s.mcp.status() or []:
            lines.append(f"- {st.name}: " + (f"connected, {st.tools} tools" if st.connected else f"down - {st.error}"))
        if not self.s.mcp.status():
            lines.append("- none configured")
        await ctx.bot.send_message(update.effective_chat.id, "\n".join(lines))  # type: ignore[union-attr]

    async def cmd_reload(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text("Reconnecting to MCP servers...")  # type: ignore[union-attr]
        try:
            await self.s.reload(reconnect=True)  # with any changes to config.toml's tools since it started
        except ConfigError as exc:
            await update.effective_message.reply_text(f"I kept the tools as they were. {exc}")  # type: ignore[union-attr]
            return
        await self.cmd_tools(update, ctx)

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        cfg = self.s.config
        lines = [f"Model: {cfg.llm.model} (thinking {cfg.llm.thinking_now})", f"Endpoint: {cfg.llm.base_url}"]
        try:
            models = await asyncio.wait_for(self.s.llm.list_models(), 10)
            ok = cfg.llm.model in models
            lines.append("Model server: reachable" + ("" if ok else f" (but '{cfg.llm.model}' isn't listed)"))
        except Exception as exc:
            lines.append(f"Model server: unreachable ({type(exc).__name__})")
        counts = self.s.memory.store.count()
        lines.append(f"Memory: {counts.get('fact', 0)} facts, {counts.get('document', 0)} document chunks")
        connected = sum(1 for st in self.s.mcp.status() if st.connected)
        lines.append(f"Tools: {len(self.s.tools.all())} ({connected}/{len(self.s.mcp.status())} MCP servers up)")
        await update.effective_message.reply_text("\n".join(lines))  # type: ignore[union-attr]
