"""Telegram front end. Uses long polling, so the Pi needs no open ports."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets
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
from pi_assistant.formatting import TELEGRAM_LIMIT, markdown_to_speech, markdown_to_telegram_html, split_message
from pi_assistant.siri import VOICE_NOTE, SiriServer
from pi_assistant.status import State

log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

COMMANDS = [
    ("reset", "Start a fresh conversation (memories are kept)"),
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
            app.add_handler(CommandHandler("reset", self.cmd_reset, filters=me))
            app.add_handler(CommandHandler("remember", self.cmd_remember, filters=me))
            app.add_handler(CommandHandler("recall", self.cmd_recall, filters=me))
            app.add_handler(CommandHandler("forget", self.cmd_forget, filters=me))
            app.add_handler(CommandHandler("tools", self.cmd_tools, filters=me))
            app.add_handler(CommandHandler("reload", self.cmd_reload, filters=me))
            app.add_handler(CommandHandler("status", self.cmd_status, filters=me))
            app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, self.on_text))
            app.add_handler(CallbackQueryHandler(self.on_button, pattern=r"^confirm:"))
        app.add_handler(MessageHandler(filters.ALL, self.on_stranger))
        app.add_error_handler(self.on_error)
        return app

    async def _post_init(self, app: Application) -> None:
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

    async def _post_shutdown(self, app: Application) -> None:
        if self._siri:
            await self._siri.stop()
        await self.s.close()

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

    async def _confirm(self, bot: Bot, chat_id: int, tool: str, args: dict[str, Any]) -> bool:
        key = secrets.token_hex(6)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        # Show every argument: approving content you can't see isn't approval. Link previews
        # stay off, or Telegram's servers would fetch a URL in the arguments before you decide.
        pretty = json.dumps(args, indent=2, ensure_ascii=False)
        prompt = f"Allow <b>{html.escape(tool)}</b>?\n<pre>{html.escape(pretty)}</pre>"
        if len(prompt) > TELEGRAM_LIMIT:
            chunks = split_message(pretty)
            for chunk in chunks:
                await bot.send_message(chat_id, chunk, link_preview_options=NO_PREVIEW)
            prompt = f"Allow <b>{html.escape(tool)}</b> with the arguments in the {len(chunks)} messages above?"
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
            approved = await asyncio.wait_for(future, timeout=self.cfg.confirm_timeout_seconds)
            verdict = "Allowed" if approved else "Denied"
        except TimeoutError:
            approved, verdict = False, "No answer, so denied"
        finally:
            self._pending.pop(key, None)
        try:
            await msg.edit_text(f"{verdict}: <b>{html.escape(tool)}</b>", parse_mode=ParseMode.HTML)
        except TelegramError:
            pass
        return approved

    # -- handlers -----------------------------------------------------------------------------

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.effective_chat and update.message and update.message.text
        chat_id = update.effective_chat.id
        reply = await self._reply(ctx.bot, chat_id, update.message.text)
        await self._send(ctx.bot, chat_id, reply)

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
        reply = await self._reply(bot, chat_id, prompt, note=VOICE_NOTE)
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

    async def _reply(self, bot: Bot, chat_id: int, text: str, note: str | None = None) -> str:
        """The agent's reply to ``text``, showing "typing..." and asking for approvals in the chat meanwhile."""
        typing = asyncio.create_task(self._keep_typing(bot, chat_id))

        async def confirm(tool: str, args: dict[str, Any]) -> bool:
            return await self._confirm(bot, chat_id, tool, args)

        try:
            result = await self.s.agent.respond(str(chat_id), text, confirm=confirm, note=note)
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
        except Exception as exc:
            log.exception("Agent failed")
            reply = f"Something went wrong: {type(exc).__name__}: {exc}"
        finally:
            typing.cancel()
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

    async def cmd_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self.s.history.reset(str(update.effective_chat.id))  # type: ignore[union-attr]
        await update.effective_message.reply_text("Started a fresh conversation. Long-term memories are kept.")  # type: ignore[union-attr]

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
        if query:
            hits = await self.s.memory.search(query, 8)
            header = f"Memories matching '{query}':"
        else:
            hits = self.s.memory.store.recent(10)
            header = "Most recent memories:"
        if not hits:
            await update.effective_message.reply_text("Nothing found.")  # type: ignore[union-attr]
            return
        lines = [header]
        for h in hits:
            text = h.text if len(h.text) < 300 else h.text[:300] + "..."
            score = f" ({1 - h.distance:.2f})" if query else ""
            lines.append(f"#{h.id}{score} {text}")
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
        await self.s.mcp.reload()
        await self.cmd_tools(update, ctx)

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        cfg = self.s.config
        lines = [f"Model: {cfg.llm.model}", f"Endpoint: {cfg.llm.base_url}"]
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
