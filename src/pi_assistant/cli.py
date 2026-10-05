"""Command-line entry point: `pi-assistant <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from pi_assistant import __version__
from pi_assistant.config import ConfigError, load_config


def _setup_logging(level: int) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "httpcore", "mcp", "telegram.ext.Updater", "apscheduler"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


# -- commands ----------------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    from pi_assistant.app import build_services
    from pi_assistant.telegram_bot import TelegramBot

    cfg = load_config(args.config)
    services = build_services(cfg)
    services.status.channel = "Telegram"
    services.status.approval_timeout = cfg.telegram.confirm_timeout_seconds
    TelegramBot(services).run()
    return 0


def cmd_display(args: argparse.Namespace) -> int:
    print(
        "The status board is now its own service, pi-display-microservice, which gets Athena's status from its\n"
        'status API. See README, "Status board", to set it up.',
        file=sys.stderr,
    )
    return 1


async def _chat(args: argparse.Namespace) -> int:
    from pi_assistant.app import build_services

    cfg = load_config(args.config)
    services = build_services(cfg)
    services.status.channel = "the terminal"
    await services.start()
    name = cfg.agent.assistant_name
    print(f"Chatting with {name} via {cfg.llm.model}. Commands: /session, /tools, /quit\n")
    for st in services.mcp.status():
        print(
            f"  MCP {st.name}: "
            + (f"{st.tools} tool{'' if st.tools == 1 else 's'}" if st.connected else f"unavailable ({st.error})")
        )

    async def confirm(tool: str, tool_args: dict[str, Any], summary: str | None = None) -> bool:
        said = "\n  " + summary.replace("\n", "\n  ") if summary else ""
        answer = await asyncio.to_thread(
            input, f"{said}\n  Allow {tool}({json.dumps(tool_args, ensure_ascii=False)})? [y/N] "
        )
        return answer.strip().lower() in {"y", "yes"}

    async def on_tool(tool: str, tool_args: dict[str, Any], output: str) -> None:
        preview = output.replace("\n", " ")
        print(f"  · {tool}({json.dumps(tool_args, ensure_ascii=False)[:120]}) -> {preview[:160]}")

    try:
        while True:
            try:
                line = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in {"/quit", "/exit"}:
                break
            if line in {"/session", "/reset"}:
                session = services.new_session()
                print(f"  (session {session.id}: the conversation so far is cleared from the model's context)")
                continue
            if line == "/tools":
                for tool in services.tools.all():
                    print(f"  - {tool.name} [{tool.source}]{' (asks first)' if tool.needs_confirmation else ''}")
                continue
            try:
                result = await services.agent.respond(
                    args.chat_id, line, confirm=confirm, on_tool=on_tool, channel="Terminal"
                )
            except Exception as exc:
                print(f"  error: {type(exc).__name__}: {exc}")
                continue
            print(f"\n{name}> {result.text}")
            print(f"  ({result.elapsed:.1f}s, {result.model_calls} model call(s))")
    finally:
        await services.close()
    return 0


async def _ingest(args: argparse.Namespace) -> int:
    from pi_assistant.app import build_memory
    from pi_assistant.memory import iter_text_files

    cfg = load_config(args.config)
    memory = build_memory(cfg)
    try:
        files = iter_text_files(args.paths)
        if not files:
            print("No text files found (supported: .md, .markdown, .txt, .org, .rst).")
            return 1
        total = 0
        for path in files:
            n = await memory.ingest_file(path)
            total += n
            print(f"  {n:4d} chunks  {path}")
        print(f"Indexed {total} chunks from {len(files)} file(s).")
    finally:
        memory.store.close()
        await memory.embedder.close()
    return 0


async def _reindex(args: argparse.Namespace) -> int:
    from pi_assistant.memory import Embedder, MemoryService, MemoryStore

    cfg = load_config(args.config)
    store = MemoryStore(cfg.db_path, cfg.embeddings.dimensions, cfg.embeddings.model, allow_model_change=True)
    memory = MemoryService(store, Embedder(cfg.embeddings), cfg.memory)
    try:
        n = await memory.reindex()
        print(f"Re-embedded {n} entries with {cfg.embeddings.model}.")
    finally:
        store.close()
        await memory.embedder.close()
    return 0


async def _doctor(args: argparse.Namespace) -> int:
    from pi_assistant.doctor import run_doctor

    return 0 if await run_doctor(load_config(args.config)) else 1


async def _eval(args: argparse.Namespace) -> int:
    from pi_assistant.evals import run_eval
    from pi_assistant.llm import LLMClient

    cfg = load_config(args.config)
    llm = LLMClient(cfg.llm)
    try:
        reports = await run_eval(llm, args.model or [cfg.llm.model], repeat=args.repeat)
    finally:
        await llm.close()
    return 0 if all(not r.error for r in reports) else 1


# -- argument parsing ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pi-assistant", description="Personal AI assistant for a Raspberry Pi.")
    parser.add_argument("--config", "-c", help="path to config.toml (default: ./config.toml)")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="run the Telegram bot (default)")
    p_chat = sub.add_parser("chat", help="chat in the terminal (handy over SSH)")
    p_chat.add_argument("--chat-id", default="cli", help="conversation id (default: cli)")
    sub.add_parser("doctor", help="check the model, embeddings, MCP servers, Telegram and the dashboard")
    p_ingest = sub.add_parser("ingest", help="add text/markdown files or folders to long-term memory")
    p_ingest.add_argument("paths", nargs="+")
    sub.add_parser("reindex", help="re-embed all memories (after changing the embeddings model)")
    p_eval = sub.add_parser("eval", help="compare models on tool calling")
    p_eval.add_argument("--model", "-m", action="append", help="model id (repeatable; default: the configured model)")
    p_eval.add_argument("--repeat", type=int, default=1, help="run each case N times")
    # The status board moved to its own service; this says where, for anything still running it.
    p_display = sub.add_parser("display", help=argparse.SUPPRESS)
    p_display.add_argument("--preview", help=argparse.SUPPRESS)
    p_display.add_argument("--demo", "--once", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    command = args.command or "run"
    quiet = command in {"chat", "doctor", "ingest", "reindex", "eval"}
    _setup_logging(logging.DEBUG if args.verbose else (logging.WARNING if quiet else logging.INFO))

    handlers = {
        "chat": _chat,
        "doctor": _doctor,
        "ingest": _ingest,
        "reindex": _reindex,
        "eval": _eval,
    }
    try:
        if command == "run":
            code = cmd_run(args)
        elif command == "display":
            code = cmd_display(args)
        else:
            code = asyncio.run(handlers[command](args))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
