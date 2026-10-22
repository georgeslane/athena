"""Command-line entry point: `athena <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from athena import __version__
from athena.config import Config, ConfigError, EmbeddingsConfig, athena_home, find_config, load_config


def _setup_logging(level: int) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "httpcore", "mcp", "telegram.ext.Updater", "apscheduler"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


# -- commands ----------------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    from athena.app import build_services
    from athena.settings import Settings, SettingsError
    from athena.telegram_bot import TelegramBot

    path = find_config(args.config)
    try:
        if changed := Settings(path, path.parent / ".env").upgrade():
            logging.getLogger(__name__).info("Brought config.toml up to date: %s", "; ".join(changed))
    except (SettingsError, OSError) as exc:  # it may still load as it is
        logging.getLogger(__name__).warning("Couldn't bring config.toml up to date: %s", exc)
    cfg = load_config(path)
    services = build_services(cfg)
    services.status.channel = "Telegram"
    services.status.approval_timeout = cfg.telegram.confirm_timeout_seconds
    TelegramBot(services).run()
    return 0


async def _chat(args: argparse.Namespace) -> int:
    from athena import commands
    from athena.app import build_services
    from athena.steps import ToolEvent

    cfg = load_config(args.config)
    services = build_services(cfg)
    services.status.channel = "the terminal"
    await services.start()
    name = cfg.agent.assistant_name
    print(f"Chatting with {name} via {cfg.llm.model}, in session {services.history.session.id}. /help for commands.\n")
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

    async def on_tool(event: ToolEvent) -> None:
        preview = event.output.replace("\n", " ")
        print(f"  · {event.tool}({json.dumps(event.args, ensure_ascii=False)[:120]}) -> {preview[:160]}")

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
            if command := commands.parse(line):
                said = await commands.run(services, *command, source="terminal")
                print("  " + (said or "There's no such command here: /help lists them.").replace("\n", "\n  "))
                continue
            session = services.history.session  # your conversation, wherever you're having it
            try:
                result = await services.agent.respond(
                    session.chat_id,
                    line,
                    confirm=confirm,
                    allowed=session.allowed_tools,
                    on_tool=on_tool,
                    channel="Terminal",
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
    from athena.app import build_memory
    from athena.memory import iter_text_files

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
    from athena.memory import Embedder, MemoryService, MemoryStore

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
    from athena.search_eval import QUESTIONS, print_report, run_search_test

    cfg = load_config(args.config)
    server = _embeddings_server(cfg, args.on_mac)
    reports = []
    for model in args.model or [cfg.embeddings.model]:
        reports.append(await run_search_test(server, model))
        print_report(reports[-1], cfg.memory)
    print(f"\n({len(QUESTIONS)} questions about made-up memories: a rough guide, not a benchmark.)")
    return 0 if all(not r.error for r in reports) else 1


async def _embeddings_use(args: argparse.Namespace) -> int:
    """Switch the embeddings model: test it against the current one, then (if you agree) change
    config.toml and re-embed every memory. If re-embedding fails, config.toml is put back."""
    from athena.config_files import write_private
    from athena.search_eval import print_report, run_search_test
    from athena.settings import Settings, SettingsError

    cfg = load_config(args.config)
    indexed, count = _index_info(cfg)
    old, new = indexed or cfg.embeddings.model, args.model  # the index's model, if config.toml was changed first
    target = _embeddings_server(cfg, args.on_mac).model_copy(update={"model": new})
    moving = target.base_url != cfg.embeddings.base_url
    if new == old == cfg.embeddings.model and not moving:
        print(f"Athena already uses {new}. To check it: athena embeddings test")
        return 0
    if _athena_running():
        print(
            "Athena is running, and would keep saving memories with the old model while this re-embeds them.\n"
            "Stop it first (sudo systemctl stop athena), or use athena embeddings switch, "
            "which does that for you."
        )
        return 1
    print(f"Testing {old} (now) and {new} on the same made-up memories...")
    current = await run_search_test(cfg.embeddings, old)
    print_report(current, cfg.memory)
    report = await run_search_test(target)
    print_report(report, cfg.memory)
    if report.error:
        print(f"\nNothing was changed: {new} didn't work.")
        return 1

    changes: dict[str, object] = {"embeddings.model": new}
    if moving:  # empty: the model server, which config.toml then follows if it moves
        changes |= {"embeddings.base_url": "", "embeddings.api_key": ""}
    recall = report.best_recall_cutoff()
    if recall != cfg.memory.recall_max_distance:
        changes["memory.recall_max_distance"] = recall
    duplicate = report.best_duplicate_cutoff()
    if duplicate is not None and duplicate != cfg.memory.duplicate_distance:
        changes["memory.duplicate_distance"] = duplicate
    print("\nThis will:")
    if cfg.embeddings.model != new:
        print(f"  - change embeddings.model from {cfg.embeddings.model} to {new}")
    if moving:
        print(f"  - get embeddings from the model server ({target.base_url}), not {cfg.embeddings.base_url}")
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
    if _is_local_ollama(cfg.embeddings.base_url):
        if moving:
            print("Athena no longer needs Ollama on the Pi: `sudo systemctl disable --now ollama` frees its memory.")
        else:
            print(f"If you won't go back to {old}, `ollama rm {old}` frees its disk space.")
    return 0


async def _embeddings_where(args: argparse.Namespace) -> int:
    """Print "ollama" if Ollama on the Pi serves the embeddings, so a new model is downloaded into it
    first, or "server" if another server does, such as oMLX on the Mac, which has its own models."""
    cfg = load_config(args.config)
    print("ollama" if _is_local_ollama(cfg.embeddings.base_url) else "server")
    return 0


def _embeddings_server(cfg: Config, on_mac: bool) -> EmbeddingsConfig:
    """The embeddings settings to test or switch to: as configured, or with the model's server (oMLX on the Mac)."""
    if not on_mac:
        return cfg.embeddings
    return cfg.embeddings.model_copy(update={"base_url": cfg.llm.base_url, "api_key": cfg.llm.api_key})


def _is_local_ollama(url: str) -> bool:
    return any(host in url for host in ("127.0.0.1:11434", "localhost:11434"))


def _index_info(cfg: Config) -> tuple[str | None, int]:
    """The model the memory index was built with, and how many memories are in it."""
    from athena.memory import connect_db

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
    """Whether the athena service is running (only systemd's is looked for)."""
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "athena"], timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


async def _doctor(args: argparse.Namespace) -> int:
    from athena.doctor import run_doctor

    return 0 if await run_doctor(load_config(args.config)) else 1


async def _eval(args: argparse.Namespace) -> int:
    from athena.evals import run_eval
    from athena.llm import LLMClient

    cfg = load_config(args.config)
    llm = LLMClient(cfg.llm)
    try:
        reports = await run_eval(llm, args.model or [cfg.llm.model], repeat=args.repeat)
    finally:
        await llm.close()
    return 0 if all(not r.error for r in reports) else 1


def _setup(args: argparse.Namespace) -> int:
    from athena.setup_wizard import main as setup

    return setup(args.config)


def _service(args: argparse.Namespace) -> int:
    from athena import service

    if args.action == "install":
        written = service.install(config=args.config)
        print(f"Installed {', '.join(written)}." if written else "The units were already up to date.")
        print("Start Athena with: sudo systemctl restart athena")
    else:
        service.uninstall()
        print(f"Athena's service is stopped and removed. Its settings and data are still in {athena_home()}.")
    return 0


def _update(args: argparse.Namespace) -> int:
    from athena import service

    service.update(config=args.config)
    print("Updated and restarted Athena.")
    return 0


def _logs(args: argparse.Namespace) -> int:
    return subprocess.run(["journalctl", "-u", "athena", "-f", "-n", str(args.lines)]).returncode


def _script(name: str, *script_args: str, **env: str) -> int:
    """Run one of the scripts that come with Athena (resources/scripts/)."""
    from athena.resources import SCRIPTS

    return subprocess.run(["bash", str(SCRIPTS / name), *script_args], env={**os.environ, **env}).returncode


def _mac(args: argparse.Namespace) -> int:
    if args.action == "connect":
        source = _source()  # for the command it prints, so the Mac has the same version: from GitHub, not a folder
        env = {"ATHENA_SOURCE": source} if source.startswith("git+") else {}
        return _script("connect-mac.sh", args.address, args.user, *([args.name] if args.name else []), **env)
    options = [*(["--pi-key", args.pi_key] if args.pi_key else []), *(["--calendar"] if args.calendar else [])]
    options += ["--documents"] if args.documents else []
    options += [o for server in args.server for o in ("--server", server)]
    options += [o for scope in args.scope for o in ("--scope", scope)]
    options += ["--uninstall"] if args.uninstall else []
    return _script("mac-install.sh", *options, ATHENA_SOURCE=_source())


def _source() -> str:
    """Where this Athena was installed from, for the Mac to install the same: its git URL and commit, or its folder."""
    from importlib.metadata import distribution

    try:
        info = json.loads(distribution("athena").read_text("direct_url.json") or "{}")
    except Exception:
        info = {}
    vcs = info.get("vcs_info") or {}
    if info.get("url") and vcs.get("vcs") == "git":
        return f"git+{info['url']}@{vcs.get('commit_id', 'main')}"
    return Path(__file__).resolve().parents[2].as_uri()  # a checkout, as when developing


def _switch_embeddings(args: argparse.Namespace) -> int:
    from athena.service import program

    return _script("switch-embeddings.sh", args.model, *(["--on-mac"] if args.on_mac else []), ATHENA=str(program()))


# -- argument parsing ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    about = "Athena, a personal assistant that runs on your own hardware."
    parser = argparse.ArgumentParser(prog="athena", description=about)
    parser.add_argument("--config", "-c", help="path to config.toml (default: ~/.athena/config.toml, or $ATHENA_HOME)")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="set Athena up, or change its main settings")
    sub.add_parser("run", help="run Athena (default): the service does this")
    p_service = sub.add_parser("service", help="install or remove the systemd service that runs Athena")
    p_service.add_argument("action", choices=["install", "uninstall"])
    sub.add_parser("update", help="update Athena, its service, and restart it")
    p_logs = sub.add_parser("logs", help="follow Athena's log")
    p_logs.add_argument("--lines", "-n", type=int, default=50, help="how many lines back to start (default: 50)")
    p_mac = sub.add_parser("mac", help="let Athena use your Mac's calendars and documents")
    mac = p_mac.add_subparsers(dest="action", required=True)
    p_connect = mac.add_parser("connect", help="on the Pi: an SSH key and entry for a Mac")
    p_connect.add_argument("address", help="the Mac's address, such as my-mac.tail1234.ts.net")
    p_connect.add_argument("user", help="your user name on the Mac")
    p_connect.add_argument("name", nargs="?", help="what to call it: athena-<name> (default: mac)")
    p_mac_install = mac.add_parser("install", help="on the Mac: the servers the Pi may use")
    p_mac_install.add_argument("--pi-key", help="the public key `athena mac connect` printed on the Pi")
    p_mac_install.add_argument("--calendar", action="store_true", help="Calendar and Reminders, through iMCP")
    p_mac_install.add_argument("--documents", action="store_true", help="your documents, for Athena to index")
    p_mac_install.add_argument("--server", action="append", default=[], help="another server the script lists")
    p_mac_install.add_argument("--scope", action="append", default=[], help="with --documents: what the Pi may see")
    p_mac_install.add_argument("--uninstall", action="store_true", help="remove everything it set up")
    sub.add_parser("chat", help="chat in the terminal, in the current session (handy over SSH)")
    sub.add_parser("doctor", help="check the model, embeddings, MCP servers, Telegram and the dashboard")
    p_ingest = sub.add_parser("ingest", help="add text/markdown files or folders to long-term memory")
    p_ingest.add_argument("paths", nargs="+")
    sub.add_parser("reindex", help="re-embed all memories (after changing the embeddings model)")
    p_emb = sub.add_parser("embeddings", help="test embeddings models, or switch to another one")
    emb = p_emb.add_subparsers(dest="action", required=True)
    p_test = emb.add_parser("test", help="how well a model finds memories, and the cut-offs that suit it")
    p_test.add_argument("--model", "-m", action="append", help="model (repeatable; default: the configured one)")
    on_mac = "on the model's own server ([llm] base_url, such as oMLX on the Mac), not the embeddings server"
    p_test.add_argument("--on-mac", action="store_true", help=f"test models {on_mac}")
    p_use = emb.add_parser("use", help="test a model, then switch to it and re-embed every memory")
    p_use.add_argument("model")
    p_use.add_argument("--on-mac", action="store_true", help=f"a model {on_mac}")
    p_use.add_argument("--yes", "-y", action="store_true", help="don't ask first")
    emb.add_parser("where", help="where embeddings come from: ollama (on the Pi) or server")
    p_switch = emb.add_parser("switch", help="switch model, stopping Athena while every memory's re-embedded")
    p_switch.add_argument("model")
    p_switch.add_argument("--on-mac", action="store_true", help=f"a model {on_mac}")
    p_eval = sub.add_parser("eval", help="compare models on tool calling")
    p_eval.add_argument("--model", "-m", action="append", help="model id (repeatable; default: the configured model)")
    p_eval.add_argument("--repeat", type=int, default=1, help="run each case N times")

    args = parser.parse_args(argv)
    command = args.command or "run"
    quiet = command not in {"run"}
    _setup_logging(logging.DEBUG if args.verbose else (logging.WARNING if quiet else logging.INFO))

    handlers = {
        "chat": _chat,
        "doctor": _doctor,
        "ingest": _ingest,
        "reindex": _reindex,
        "eval": _eval,
    }
    plain = {"setup": _setup, "service": _service, "update": _update, "logs": _logs, "mac": _mac}
    if command == "embeddings":
        handlers["embeddings"] = {"use": _embeddings_use, "where": _embeddings_where}.get(args.action, _embeddings_test)
        if args.action == "switch":
            plain["embeddings"] = _switch_embeddings
    try:
        if command == "run":
            code = cmd_run(args)
        elif command in plain:
            code = plain[command](args)
        else:
            code = asyncio.run(handlers[command](args))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
