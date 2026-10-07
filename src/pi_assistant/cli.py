"""Command-line entry point: `pi-assistant <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import subprocess
import sys
from typing import Any

from pi_assistant import __version__
from pi_assistant.config import Config, ConfigError, load_config


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
    cfg = load_config(args.config)
    n = await _reembed(cfg)
    print(f"Re-embedded {n} entries with {cfg.embeddings.model}.")
    return 0


async def _reembed(cfg: Config) -> int:
    from pi_assistant.memory import Embedder, MemoryService, MemoryStore

    store = MemoryStore(cfg.db_path, cfg.embeddings.dimensions, cfg.embeddings.model, allow_model_change=True)
    memory = MemoryService(store, Embedder(cfg.embeddings), cfg.memory)

    def progress(done: int, total: int) -> None:
        print(f"\r  {done}/{total}", end="" if done < total else "\n", flush=True)

    try:
        return await memory.reindex(progress)
    finally:
        store.close()
        await memory.embedder.close()


async def _embeddings_test(args: argparse.Namespace) -> int:
    from pi_assistant.search_eval import QUESTIONS, print_report, run_search_test

    cfg = load_config(args.config)
    reports = []
    for model in args.model or [cfg.embeddings.model]:
        reports.append(await run_search_test(cfg.embeddings, model))
        print_report(reports[-1], cfg.memory)
    print(f"\n({len(QUESTIONS)} questions about made-up memories: a rough guide, not a benchmark.)")
    return 0 if all(not r.error for r in reports) else 1


async def _embeddings_use(args: argparse.Namespace) -> int:
    """Switch the embeddings model: test it against the current one, then (if you agree) change
    config.toml and re-embed every memory. If re-embedding fails, config.toml is put back."""
    from pi_assistant.search_eval import print_report, run_search_test
    from pi_assistant.settings import Settings, SettingsError, write_private

    cfg = load_config(args.config)
    indexed, count = _index_info(cfg)
    old, new = indexed or cfg.embeddings.model, args.model  # the index's model, if config.toml was changed first
    if new == old == cfg.embeddings.model:
        print(f"Athena already uses {new}. To check it: pi-assistant embeddings test")
        return 0
    if _athena_running():
        print(
            "Athena is running, and would keep saving memories with the old model while this re-embeds them.\n"
            "Stop it first (sudo systemctl stop pi-assistant), or use scripts/switch-embeddings.sh, "
            "which does that for you."
        )
        return 1
    print(f"Testing {old} (now) and {new} on the same made-up memories...")
    current = await run_search_test(cfg.embeddings, old)
    print_report(current, cfg.memory)
    report = await run_search_test(cfg.embeddings, new)
    print_report(report, cfg.memory)
    if report.error:
        print(f"\nNothing was changed: {new} didn't work.")
        return 1

    changes: dict[str, object] = {"embeddings.model": new}
    recall = report.best_recall_cutoff()
    if recall != cfg.memory.recall_max_distance:
        changes["memory.recall_max_distance"] = recall
    duplicate = report.best_duplicate_cutoff()
    if duplicate is not None and duplicate != cfg.memory.duplicate_distance:
        changes["memory.duplicate_distance"] = duplicate
    print("\nThis will:")
    if cfg.embeddings.model != new:
        print(f"  - change embeddings.model from {cfg.embeddings.model} to {new}")
    if "memory.recall_max_distance" in changes:
        print(f"  - change memory.recall_max_distance from {cfg.memory.recall_max_distance} to {recall}")
    if "memory.duplicate_distance" in changes:
        print(f"  - change memory.duplicate_distance from {cfg.memory.duplicate_distance} to {duplicate}")
    minutes = count * report.per_text_ms / 60_000
    print(f"  - re-embed {count} memories with {new}" + (f" (about {minutes:.0f} min)" if minutes >= 1 else ""))
    worse = not current.error and report.top1 < current.top1
    if worse:
        print(f"\n{new} found the right memory for fewer questions than {old}.")
    if args.yes and worse:
        print("Not switching without asking: run it again without --yes to switch anyway.")
        return 1
    if not args.yes and input("\nGo ahead? [y/N] ").strip().lower() not in {"y", "yes"}:
        print("Nothing was changed.")
        return 0

    if cfg.path is None or not cfg.path.exists():
        print("There's no config.toml to change.")
        return 1
    settings = Settings(cfg.path, cfg.env_path)
    before = cfg.path.read_text()
    try:
        new_cfg = settings.set_values(changes)
    except SettingsError as exc:
        print(exc)
        return 1
    try:
        n = await _reembed(new_cfg)
    except BaseException as exc:
        write_private(cfg.path, before)
        print(f"\nRe-embedding stopped ({exc or type(exc).__name__}), so nothing was changed: ", end="")
        print("config.toml is as it was and memory still uses the old model.")
        if isinstance(exc, Exception):
            return 1
        raise
    print(f"\nDone: {n} memories re-embedded with {new}. config.toml was backed up to config.toml.bak.")
    print(f"If you won't go back to {old}, `ollama rm {old}` frees its disk space.")
    return 0


def _index_info(cfg: Config) -> tuple[str | None, int]:
    """The model the memory index was built with, and how many memories are in it."""
    from pi_assistant.memory import connect_db

    if not cfg.db_path.exists():
        return None, 0
    conn = connect_db(cfg.db_path)
    try:
        row = conn.execute("SELECT value FROM memory_meta WHERE key = 'embedding_model'").fetchone()
        return (row[0] if row else None), conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    except sqlite3.OperationalError:  # no memory tables yet
        return None, 0
    finally:
        conn.close()


def _athena_running() -> bool:
    """Whether the pi-assistant service is running (only systemd's is looked for)."""
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "pi-assistant"], timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


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
    p_emb = sub.add_parser("embeddings", help="test embeddings models, or switch to another one")
    emb = p_emb.add_subparsers(dest="action", required=True)
    p_test = emb.add_parser("test", help="how well a model finds memories, and the cut-offs that suit it")
    p_test.add_argument("--model", "-m", action="append", help="model (repeatable; default: the configured one)")
    p_use = emb.add_parser("use", help="test a model, then switch to it and re-embed every memory")
    p_use.add_argument("model")
    p_use.add_argument("--yes", "-y", action="store_true", help="don't ask first")
    p_eval = sub.add_parser("eval", help="compare models on tool calling")
    p_eval.add_argument("--model", "-m", action="append", help="model id (repeatable; default: the configured model)")
    p_eval.add_argument("--repeat", type=int, default=1, help="run each case N times")
    # The status board moved to its own service; this says where, for anything still running it.
    p_display = sub.add_parser("display", help=argparse.SUPPRESS)
    p_display.add_argument("--preview", help=argparse.SUPPRESS)
    p_display.add_argument("--demo", "--once", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    command = args.command or "run"
    quiet = command in {"chat", "doctor", "ingest", "reindex", "embeddings", "eval"}
    _setup_logging(logging.DEBUG if args.verbose else (logging.WARNING if quiet else logging.INFO))

    handlers = {
        "chat": _chat,
        "doctor": _doctor,
        "ingest": _ingest,
        "reindex": _reindex,
        "eval": _eval,
    }
    if command == "embeddings":
        handlers["embeddings"] = _embeddings_use if args.action == "use" else _embeddings_test
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
