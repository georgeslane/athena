"""Telegram front end. Uses long polling, so the Pi needs no open ports."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
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

from athena import commands, workspace
from athena.app import Services
from athena.commands import COMMANDS
from athena.config import THINKING, ConfigError
from athena.files import SENT, TEXT
from athena.formatting import TELEGRAM_LIMIT, markdown_to_speech, markdown_to_telegram_html, split_message
from athena.photos import MAX_BYTES, MAX_PER_MESSAGE, Photo, label, mime_of
from athena.siri import VOICE_NOTE, SiriServer
from athena.status import State
from athena.tool_approvals import ToolChange
from athena.tools import ToolError

log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


@dataclass
class _Answer:
    """What Athena said, and the files it's sending with it (send_file, in files.py)."""

    text: str
    files: list[str] = field(default_factory=list)  # as send_file noted them


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
DOWNLOAD_MB = 20  # the largest file a Telegram bot can download


class TelegramBot:
    def __init__(self, services: Services):
        self.s = services
        self.cfg = services.config.telegram
        self.allowed = set(self.cfg.allowed_user_ids)
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
            app.add_handler(MessageHandler(me & filters.Document.ALL & ~filters.Document.IMAGE, self.on_file))
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
        """Keep the model server's prompt cache ready for your conversation, the current session
        (llm.warm_up_minutes)."""
        while True:
            try:
                reply = await self.s.agent.warm_up(self.s.history.current_chat)
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
        timeout = timeout or self.cfg.confirm_timeout_seconds
        session = None if why else self.s.history.session.id  # your message's, or a task's
        approval = self.s.approvals.open(tool, args, summary, timeout=timeout, why=why, session=session)
        key = approval.id
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
        try:
            msg = await bot.send_message(
                chat_id, prompt, parse_mode=ParseMode.HTML, reply_markup=buttons, link_preview_options=NO_PREVIEW
            )
        except BaseException:
            self.s.approvals.answer(key, False)  # it can't be answered here: it mustn't wait on the dashboard
            raise
        try:
            approved = await self.s.approvals.wait(approval)  # answered here, or on the dashboard
        except asyncio.CancelledError:  # /stop
            await self._settle(msg, f"Stopped: <b>{html.escape(tool)}</b>")
            raise
        await self._settle(msg, f"{approval.verdict}: <b>{html.escape(tool)}</b>")
        return approved

    async def _settle(self, msg: Any, text: str) -> None:
        """Replace an approval's buttons with what became of it."""
        try:
            await msg.edit_text(text, parse_mode=ParseMode.HTML)
        except TelegramError:
            pass

    # -- reaching you when you haven't just asked something (channel.py) ----------------------

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
        await self._deliver(ctx.bot, chat_id, await self._reply(ctx.bot, chat_id, update.message.text))

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

    async def on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """A file, such as a PDF or a Markdown note: kept in the workspace's inbox/, and the message says where,
        with its caption as what you're asking."""
        assert update.effective_chat and update.message and update.message.document
        msg, chat_id, doc = update.message, update.effective_chat.id, update.message.document
        name = workspace.safe_name(doc.file_name or "file")
        suffix = PurePosixPath(name).suffix.lower()
        limit = min(self.s.config.workspace.max_read_mb, DOWNLOAD_MB) * 1024 * 1024
        if not self.s.config.workspace.enabled:
            problem = "Files are switched off, so I can't read that: switch them on in the dashboard's Tools tab."
        elif suffix != ".pdf" and suffix not in TEXT:
            problem = "I can read PDFs and text files, such as Markdown, CSV and JSON, but not that kind of file."
        elif (doc.file_size or 0) > limit:
            problem = f"That's over {limit // 1024 // 1024} MB, more than I can take."
        else:
            problem = None
        if problem:
            await self._send(ctx.bot, chat_id, problem)
            return
        data = bytes(await (await ctx.bot.get_file(doc.file_id)).download_as_bytearray())
        try:
            kept = workspace.save_new(self._workspace, workspace.INBOX, name, data)
        except ToolError as exc:
            await self._send(ctx.bot, chat_id, f"I couldn't keep that file: {exc}")
            return
        text = f"[File: {kept}] {(msg.caption or '').strip()}".strip()  # read_file reads it
        await self._deliver(ctx.bot, chat_id, await self._reply(ctx.bot, chat_id, text))

    async def _album_done(self, bot: Bot, group: str) -> None:
        await asyncio.sleep(ALBUM_SECONDS)
        album = self._albums.pop(group)  # a photo arriving after this starts another
        photos, left_out = album.photos[:MAX_PER_MESSAGE], len(album.photos) - MAX_PER_MESSAGE
        if left_out > 0:
            await self._send(bot, album.chat_id, f"I'll look at the first {MAX_PER_MESSAGE} photos, not all of them.")
        await self._answer_photos(bot, album.chat_id, album.caption, photos)

    async def _answer_photos(self, bot: Bot, chat_id: int, caption: str, photos: list[Photo]) -> None:
        await self._deliver(bot, chat_id, await self._reply(bot, chat_id, caption, photos=photos))

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
        answer = await self._reply(bot, chat_id, prompt, note=VOICE_NOTE, channel="Siri")
        if answer is None:
            return "You stopped that in Telegram."
        try:
            await self._deliver(bot, chat_id, answer, reply_to=question)
        except TelegramError as exc:  # Siri still gets the answer
            log.warning("Couldn't post the answer for Siri to Telegram: %s", exc)
        return markdown_to_speech(answer.text)

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
    ) -> _Answer | None:
        """The agent's reply to ``text``, and ``photos`` with it, showing "typing..." and asking for approvals in
        the chat meanwhile, with any files it's sending.

        None if /stop stopped it.
        """
        files: list[str] = []
        typing = asyncio.create_task(self._keep_typing(bot, chat_id))
        asked = self._last[chat_id] = _Asked(text, photos=list(photos or []))

        async def confirm(tool: str, args: dict[str, Any], summary: str | None = None) -> bool:
            return await self._confirm(bot, chat_id, tool, args, summary)

        session = self.s.history.session  # what you say goes into the current session
        answering = asyncio.create_task(
            self.s.agent.respond(
                session.chat_id,
                text,
                confirm=confirm,
                allowed=session.allowed_tools,
                note=note,
                channel=channel,
                photos=photos,
            )
        )
        self._answering.setdefault(chat_id, set()).add(answering)
        try:
            await asyncio.wait({answering})
            if answering.cancelled():
                log.info("Stopped answering, as asked")
                return None
            result = answering.result()
            asked.answered = result.saved
            reply, files = result.text, result.notes.get(SENT, [])
            log.info(
                "Replied in %.1fs (%d model calls, tools: %s)",
                result.elapsed,
                result.model_calls,
                ", ".join(result.tools_used) or "none",
            )
        except Exception as exc:
            reply = commands.failure(self.s, exc, photos=bool(photos))
        finally:
            typing.cancel()
            self._answering[chat_id].discard(answering)
            answering.cancel()  # if this was cancelled itself, say as Athena shuts down
        return _Answer(reply, files)

    async def _deliver(self, bot: Bot, chat_id: int, answer: _Answer | None, reply_to: int | None = None) -> None:
        """Send an answer, and then the files it came with."""
        if answer is None:
            return
        await self._send(bot, chat_id, answer.text, reply_to=reply_to)
        for noted in answer.files:
            await self._send_file(bot, chat_id, noted)

    async def _send_file(self, bot: Bot, chat_id: int, noted: str) -> None:
        """Send a file send_file noted, from the workspace, never through a link (workspace.py)."""
        file = json.loads(noted)
        name = PurePosixPath(file["path"]).name
        try:
            data = workspace.read(self._workspace, file["path"], self.s.config.workspace.max_send_mb * 1024 * 1024)
            await bot.send_document(chat_id, InputFile(data, filename=name), caption=file.get("caption") or None)
        except (ToolError, TelegramError) as exc:
            log.warning("Couldn't send a file: %s", exc)
            await self._send(bot, chat_id, f"I couldn't send you {name}: {exc}")

    async def send_file(self, noted: str) -> None:
        """Send you a file a task's sending (Channel)."""
        assert self._bot is not None
        await self._send_file(self._bot, self.cfg.allowed_user_ids[0], noted)

    @property
    def _workspace(self) -> Path:
        return workspace.workspace_of(self.s.config)

    async def on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        assert query is not None
        if not update.effective_user or update.effective_user.id not in self.allowed:
            await query.answer("Not allowed.")
            return
        _, key, choice = (query.data or "::").split(":", 2)
        if self.s.approvals.answer(key, choice == "yes"):
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
        await update.effective_message.reply_text(commands.help_text(self.s))  # type: ignore[union-attr]

    async def cmd_session(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(commands.new_session(self.s))  # type: ignore[union-attr]

    async def cmd_compact(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        assert message is not None
        said = await message.reply_text("Compacting our conversation…")
        text = await commands.compact(self.s, self.s.history.current_chat)
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
        elif exchange := await self.s.take_back_last_exchange(self.s.history.current_chat):
            text, photos = exchange.question, self._kept_photos(exchange.photos)  # its text says [Photo]
        else:
            await update.effective_message.reply_text("There's nothing to try again in this session.")  # type: ignore[union-attr]
            return
        shown = text if len(text) <= 200 else text[:199].rstrip() + "…"
        if photos and not shown.startswith(label(len(photos))):
            shown = f"{label(len(photos))} {shown}".rstrip()
        await ctx.bot.send_message(chat_id, f"🔁 Trying again: {shown}", link_preview_options=NO_PREVIEW)
        await self._deliver(ctx.bot, chat_id, await self._reply(ctx.bot, chat_id, text, photos=photos))

    def _kept_photos(self, names: tuple[str, ...]) -> list[Photo]:
        return [photo for name in names if (photo := self.s.history.photos.load(name))]

    async def cmd_think(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        choice = " ".join(ctx.args or [])
        buttons = None if choice.strip() else self._thinking_buttons()
        await update.effective_message.reply_text(commands.think(self.s, choice), reply_markup=buttons)  # type: ignore[union-attr]

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
        said = commands.think(self.s, choice)
        await query.answer()
        with contextlib.suppress(TelegramError):  # say, pressing the one that's already chosen
            await query.edit_message_text(said, reply_markup=self._thinking_buttons())

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
        text, listed = commands.tasks(self.s)
        rows = []
        for t in listed:
            row = [InlineKeyboardButton(f"▶ Run #{t['id']}", callback_data=f"task:run:{t['id']}")]
            if t["status"] == "running":
                row = [InlineKeyboardButton(f"⏹ Stop #{t['id']}", callback_data=f"task:stop:{t['id']}")]
            elif t["status"] == "active":
                row.append(InlineKeyboardButton("⏸ Pause", callback_data=f"task:pause:{t['id']}"))
            else:
                row.append(InlineKeyboardButton("⏯ Resume", callback_data=f"task:resume:{t['id']}"))
            row.append(InlineKeyboardButton("🗑 Delete", callback_data=f"task:delete:{t['id']}"))
            rows.append(row)
        return text, InlineKeyboardMarkup(rows) if rows else None

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
        said = await commands.remember(self.s, " ".join(ctx.args or []), source="telegram")
        await update.effective_message.reply_text(said)  # type: ignore[union-attr]

    async def cmd_recall(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        said = await commands.recall(self.s, " ".join(ctx.args or []))
        await self._send(ctx.bot, update.effective_chat.id, said)  # type: ignore[union-attr]

    async def cmd_forget(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        said = commands.forget(self.s, " ".join(ctx.args or []))
        await update.effective_message.reply_text(said)  # type: ignore[union-attr]

    async def cmd_tools(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await ctx.bot.send_message(update.effective_chat.id, commands.tools(self.s))  # type: ignore[union-attr]

    async def cmd_reload(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text("Reconnecting to MCP servers...")  # type: ignore[union-attr]
        try:
            await self.s.reload(reconnect=True)  # with any changes to config.toml's tools since it started
        except ConfigError as exc:
            await update.effective_message.reply_text(f"I kept the tools as they were. {exc}")  # type: ignore[union-attr]
            return
        await self.cmd_tools(update, ctx)

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(await commands.status(self.s))  # type: ignore[union-attr]
