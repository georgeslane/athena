"""`pi-assistant doctor`: check every connection the assistant depends on."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from pi_assistant.app import build_services
from pi_assistant.builtin import FAIL, OK, WARN
from pi_assistant.config import Config
from pi_assistant.dashboard import MIN_TOKEN_CHARS as MIN_PASSWORD_CHARS
from pi_assistant.documents import DOCUMENTS_INDEXED, DocumentsError, connect_mac
from pi_assistant.integrations import BUILTINS
from pi_assistant.llm import LLMClient
from pi_assistant.mcp_manager import MCPManager, ServerStatus
from pi_assistant.memory import Embedder, MemoryStore
from pi_assistant.photos import can_see
from pi_assistant.sandbox import Sandbox, SandboxError
from pi_assistant.search_eval import describe_embeddings_error
from pi_assistant.server_envs import ServerEnvs
from pi_assistant.siri import MIN_TOKEN_CHARS
from pi_assistant.status_api import is_loopback
from pi_assistant.tool_approvals import ToolApprovals
from pi_assistant.tools import Tool

_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}


async def run_doctor(cfg: Config) -> bool:
    healthy = True

    def report(mark: str, msg: str) -> None:
        nonlocal healthy
        if mark == FAIL:
            healthy = False
        print(f" {mark} {msg}")

    print(f"Config: {cfg.base_dir / 'config.toml'}")

    # 1. Model server --------------------------------------------------------------------
    print(f"\nModel server ({cfg.llm.base_url})")
    llm = LLMClient(cfg.llm)
    model_ok = False
    try:
        models = await asyncio.wait_for(llm.list_models(), 15)
        report(OK, f"reachable; {len(models)} model(s) available")
        if cfg.llm.model in models:
            report(OK, f"model '{cfg.llm.model}' is available")
        else:
            report(FAIL, f"model '{cfg.llm.model}' not found. Available: {', '.join(models) or 'none'}")
        started = time.monotonic()
        reply = await llm.chat(
            [{"role": "user", "content": "What's the weather in Paris? Use the tool."}], [_PROBE_TOOL], max_tokens=256
        )
        took = time.monotonic() - started
        if reply.tool_calls and reply.tool_calls[0].name == "get_weather":
            speed = f", {reply.tokens_per_second:.0f} tok/s" if reply.tokens_per_second else ""
            report(OK, f"tool calling works ({took:.1f}s{speed})")
            model_ok = True
        else:
            report(FAIL, f"model answered without calling the tool: {reply.content[:120]!r}")
        try:
            sees, said = await can_see(llm)
        except Exception as exc:
            report(WARN, f"photos: the model server refused one ({type(exc).__name__}), so photos you send won't work")
        else:
            if sees:
                report(OK, "photos: the model can see them")
            else:
                report(WARN, f"photos: shown a red square, the model said {said[:60]!r}, so it may not see photos")
    except Exception as exc:
        report(FAIL, f"{type(exc).__name__}: {exc}")
        report(WARN, "Check oMLX is running, listening on the network (not just localhost), and the API key matches.")
    finally:
        await llm.close()

    # 2. Embeddings ------------------------------------------------------------------------
    print(f"\nEmbeddings ({cfg.embeddings.base_url}, {cfg.embeddings.model})")
    embedder = Embedder(cfg.embeddings)
    try:
        vec = await asyncio.wait_for(embedder.embed_one("hello world", "query"), 60)
        report(OK, f"working; {len(vec)} dimensions")
    except Exception as exc:
        report(FAIL, await describe_embeddings_error(exc, cfg.embeddings))
        if ":11434" not in cfg.embeddings.base_url:
            report(WARN, 'Check the model server serves the embeddings model too (README, "Embeddings").')
    finally:
        await embedder.close()

    # 3. Memory database --------------------------------------------------------------------
    print(f"\nMemory database ({cfg.db_path})")
    try:
        store = MemoryStore(cfg.db_path, cfg.embeddings.dimensions, cfg.embeddings.model)
        counts = store.count()
        words = store.words
        store.close()
        report(OK, f"sqlite-vec loaded; {counts.get('fact', 0)} facts, {counts.get('document', 0)} document chunks")
        if words:
            report(OK, "Memory can be searched by its words, as well as by meaning")
        else:
            report(WARN, "Memory can only be searched by meaning: this Python's SQLite has no FTS5. Use uv's Python.")
    except Exception as exc:
        report(FAIL, f"{type(exc).__name__}: {exc}")

    # 4. MCP servers ---------------------------------------------------------------------------
    print("\nMCP servers")
    mcp_tools: list[Tool] = []
    if not cfg.mcp_servers:
        report(WARN, "none configured")
    else:
        envs = ServerEnvs.for_config(cfg)
        approvals = ToolApprovals(cfg.db_path)
        sandbox = Sandbox.detect()
        await check_sandbox(sandbox, cfg, report)
        mcp = MCPManager(cfg.mcp_servers, cfg.base_dir, cfg.log_dir, envs, approvals, sandbox)
        try:
            await mcp.start()
            for st in mcp.status():
                if st.connected:
                    names = ", ".join(t.name for t in mcp.tools() if t.source == f"mcp:{st.name}")
                    report(OK, f"{st.name}: {st.tools} tool{'' if st.tools == 1 else 's'} ({names})")
                else:
                    report(FAIL, f"{st.name}: {st.error}")
                server = cfg.mcp_servers[st.name]
                if server.command and sandbox.supported and not server.sandbox:
                    report(WARN, f"{st.name}: runs without a sandbox (sandbox = false), so it can read your files")
                if st.pending:
                    held = ", ".join(c.tool + (" (changed)" if c.approved else " (new)") for c in st.pending)
                    report(WARN, f"{st.name}: held back until you approve them on the dashboard: {held}")
                await check_lock(envs, st, report)
            mcp_tools = mcp.tools()
        finally:
            await mcp.stop()
            approvals.close()
        disabled = [n for n, c in cfg.mcp_servers.items() if not c.enabled]
        if disabled:
            report(WARN, f"disabled: {', '.join(disabled)}")

    # Documents from your Mac, if you've chosen folders -------------------------------------------
    if cfg.documents.folders:
        print(f"\nDocuments ({' '.join([cfg.documents.command, *cfg.documents.args])})")
        await check_documents(cfg, report)

    # 5. Telegram -------------------------------------------------------------------------------
    print("\nTelegram")
    if not cfg.telegram.bot_token:
        report(FAIL, "TELEGRAM_BOT_TOKEN is not set (.env)")
    else:
        from telegram import Bot

        try:
            async with Bot(cfg.telegram.bot_token) as bot:
                me = await bot.get_me()
            report(OK, f"token valid: @{me.username}")
        except Exception as exc:
            report(FAIL, f"{type(exc).__name__}: {exc}")
    if cfg.telegram.allowed_user_ids:
        report(OK, f"allowed users: {', '.join(map(str, cfg.telegram.allowed_user_ids))}")
    else:
        report(WARN, "no allowed_user_ids yet: the bot will run in setup mode and tell you your ID")

    # 6. Siri ----------------------------------------------------------------------------------
    if cfg.siri.enabled:
        print(f"\nSiri ({cfg.siri.host}:{cfg.siri.port})")
        await check_siri(cfg, report)

    # 7. The status board's API -------------------------------------------------------------------
    print(f"\nStatus API, for the status board ({cfg.display.host}:{cfg.display.port})")
    await check_status_api(cfg, report)

    # 8. The dashboard ---------------------------------------------------------------------------
    print(f"\nDashboard ({cfg.dashboard.host}:{cfg.dashboard.port})")
    await check_dashboard(cfg, report)

    # 9. Built-in integrations that are switched on --------------------------------------------
    for kind in BUILTINS:
        if kind.wanted(cfg):
            print(f"\n{kind.entry.title}")
            await kind.check(cfg, report)

    # 10. The prompt ---------------------------------------------------------------------------
    print("\nThe prompt: what the model reads before every reply")
    if model_ok:
        await check_prompt(cfg, mcp_tools, report)
    else:
        report(WARN, "skipped, since the model server isn't working")

    print("\nAll good." if healthy else "\nSome checks failed (see above).")
    return healthy


@dataclass
class PromptCost:
    tools: int
    tool_tokens: int | None  # how many tokens the tools' descriptions add to every request
    cold_seconds: float  # reading the whole prompt when none of it is cached
    tool_seconds: float  # how much of that went on the tools
    cached_seconds: float  # reading the same prompt again, now that it's cached


async def measure_prompt(llm: LLMClient, system_prompt: str, schemas: list[dict[str, Any]]) -> PromptCost:
    """Time the model server reading the prompt: without the tools, with them, then with them again."""
    run = secrets.token_hex(4)

    def messages(tag: str) -> list[dict[str, Any]]:
        # A new first line, so none of the prompt is cached from earlier requests.
        system = {"role": "system", "content": f"[doctor {run}{tag}]\n{system_prompt}"}
        return [system, {"role": "user", "content": "Hi"}]

    bare = await llm.chat(messages("a"), None, max_tokens=1)
    cold = await llm.chat(messages("b"), schemas or None, max_tokens=1)
    cached = await llm.chat(messages("b"), schemas or None, max_tokens=1)
    tokens = cold.prompt_tokens - bare.prompt_tokens if cold.prompt_tokens and bare.prompt_tokens else None
    return PromptCost(len(schemas), tokens, cold.elapsed, max(0.0, cold.elapsed - bare.elapsed), cached.elapsed)


async def check_documents(cfg: Config, report: Callable[[str, str], None]) -> None:
    """Whether the Mac with your documents answers, and what's indexed from it."""
    try:
        async with connect_mac(cfg.documents, cfg.log_dir / "documents.log") as mac:
            about = await mac.call("about")
        report(OK, f"{about.get('host') or 'your Mac'} answers; it lets Athena see {', '.join(about.get('scope', []))}")
    except DocumentsError as exc:
        report(FAIL, str(exc))
    try:
        store = MemoryStore(cfg.db_path, cfg.embeddings.dimensions, cfg.embeddings.model)
        try:
            indexed = [d for d in store.documents().values() if d.folder in cfg.documents.folders]
            when = store.get_meta(DOCUMENTS_INDEXED)
        finally:
            store.close()
    except Exception as exc:
        report(FAIL, f"{type(exc).__name__}: {exc}")
        return
    folders = ", ".join(cfg.documents.folders)
    if indexed and when:
        report(OK, f"{len(indexed)} documents indexed from {folders}, last on {when[:16].replace('T', ' at ')} UTC")
    else:
        report(WARN, f"{folders}: not indexed yet. Press Index now on the dashboard, under Memory.")


async def check_sandbox(sandbox: Sandbox, cfg: Config, report: Callable[[str, str], None]) -> None:
    """Whether local servers can be sandboxed here, tried for real: can a sandboxed process see files or connect out?"""
    if not any(s.command and s.enabled for s in cfg.mcp_servers.values()):
        return
    if not sandbox.supported:
        report(WARN, "sandbox: not on Linux, so local servers run without one")
        return
    try:
        results = await sandbox.self_test()
    except SandboxError as exc:
        report(FAIL, f"sandbox: {exc}")
        return
    for ok, what in results:
        report(OK if ok else FAIL, f"sandbox: {what}")


async def check_lock(envs: ServerEnvs, st: ServerStatus, report: Callable[[str, str], None]) -> None:
    """For a server started with uvx: whether its dependencies are locked, and if they have known vulnerabilities."""
    if st.lock == "unlocked":
        report(WARN, f"{st.name}: uvx is given options Athena can't lock, so its dependencies aren't pinned")
        return
    if st.lock is None:
        return
    audit = await envs.audit(st.name)
    origin = "a reviewed lock (mcp-locks/)" if st.lock == "reviewed" else "a lock made when it was first used"
    fixed = envs.fixed(st.name)
    if fixed:
        origin += f", with security updates ({'; '.join(fixed)})"
    if audit.clean:
        report(OK, f"{st.name}: dependencies pinned by {origin}; {audit.summary.lower()}")
    elif audit.clean is None:
        report(WARN, f"{st.name}: dependencies pinned by {origin}, but not checked: {audit.summary}")
    else:
        found = "; ".join(
            f.describe() + (f", fixed in {f.fixed}" if f.fixed else ", no fix yet") for f in audit.findings
        )
        report(FAIL, f"{st.name}: {audit.summary}: {found}")
        if any(f.fixed for f in audit.findings):
            report(WARN, f"{st.name}: press Fix on it in the dashboard's Tools tab, or Athena updates it within a day")


def report_prompt(cost: PromptCost, report: Callable[[str, str], None]) -> None:
    tokens = f", adding {cost.tool_tokens:,} tokens to every request" if cost.tool_tokens is not None else ""
    report(OK, f"{cost.tools} tools{tokens}")
    speed = ""
    if cost.tool_tokens and cost.tool_seconds > 0.1:
        speed = f" (about {cost.tool_tokens / cost.tool_seconds:.0f} tokens a second)"
    cold = f"with nothing cached, reading it takes {cost.cold_seconds:.1f}s"
    report(OK, f"{cold}, {cost.tool_seconds:.1f}s of it for the tools{speed}")
    if cost.cold_seconds < 2:
        return  # quick enough that caching hardly matters
    if cost.cached_seconds < cost.cold_seconds / 2:
        report(OK, f"once it's cached, {cost.cached_seconds:.1f}s")
    else:
        report(
            WARN,
            f"once it's cached, still {cost.cached_seconds:.1f}s: the model server doesn't seem to reuse "
            "what it has read, so every message pays the full cost",
        )
    if cost.tool_seconds > 15:
        report(WARN, "the tools are slow to read: switch off servers you rarely use, or narrow their include lists")


async def check_prompt(cfg: Config, mcp_tools: list[Tool], report: Callable[[str, str], None]) -> None:
    try:
        services = build_services(cfg)  # for the exact system prompt and built-in tools the assistant uses
    except Exception as exc:
        report(WARN, f"skipped: {type(exc).__name__}: {exc}")
        return
    try:
        services.tools.add_provider(lambda: mcp_tools)
        schemas = services.tools.schemas()  # as they're sent: without those found when they're needed
        report_prompt(await measure_prompt(services.llm, services.agent.system_prompt, schemas), report)
    except Exception as exc:
        report(FAIL, f"{type(exc).__name__}: {exc}")
    finally:
        await services.close()


async def check_siri(cfg: Config, report: Callable[[str, str], None]) -> None:
    if len(cfg.siri.token) < MIN_TOKEN_CHARS:
        report(FAIL, f"SIRI_TOKEN is missing or shorter than {MIN_TOKEN_CHARS} characters (.env)")
    if not cfg.telegram.allowed_user_ids:
        report(FAIL, "needs telegram.allowed_user_ids: questions from Siri and their answers go to your chat")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"http://{cfg.siri.host}:{cfg.siri.port}/health")
        response.raise_for_status()
        report(OK, "listening")
    except httpx.HTTPError:
        report(WARN, "not answering. It runs inside the bot: is the pi-assistant service running?")


async def check_status_api(cfg: Config, report: Callable[[str, str], None]) -> None:
    display = cfg.display
    if display.led is not None:
        report(WARN, "[display] led is no longer used here: set led in pi-display-microservice's config.toml instead")
    if not display.enabled:
        report(WARN, "off ([display] enabled = false), so a status board can't show anything")
        return
    if not is_loopback(display.host) and not display.token:
        report(FAIL, f"it won't listen on {display.host} without [display] token")
        return
    host = "127.0.0.1" if display.host in ("0.0.0.0", "::") else display.host
    headers = {"Authorization": f"Bearer {display.token}"} if display.token else {}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"http://{host}:{display.port}/v1/status", headers=headers)
        response.raise_for_status()
        state = response.json()["status"]["state"]
        report(OK, f"answering: Athena is {state}")
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
            report(FAIL, "it refused [display] token: restart the service if you've changed the token")
        else:
            report(WARN, "not answering. It runs inside the bot: is the pi-assistant service running?")


async def check_dashboard(cfg: Config, report: Callable[[str, str], None]) -> None:
    dashboard = cfg.dashboard
    if not dashboard.enabled:
        report(WARN, "off ([dashboard] enabled = false)")
        return
    if not dashboard.token:
        report(WARN, "no password yet: Athena makes one, DASHBOARD_TOKEN in .env, when the service starts")
    elif len(dashboard.token) < MIN_PASSWORD_CHARS:
        report(FAIL, f"its password, DASHBOARD_TOKEN in .env, must be at least {MIN_PASSWORD_CHARS} characters")
        return
    host = "127.0.0.1" if dashboard.host in ("0.0.0.0", "::") else dashboard.host
    base = f"http://{host}:{dashboard.port}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            (await client.get(f"{base}/health")).raise_for_status()
            if dashboard.token:
                response = await client.get(
                    f"{base}/api/overview", headers={"Authorization": f"Bearer {dashboard.token}"}
                )
                if response.status_code == 401:
                    report(FAIL, "it refused DASHBOARD_TOKEN: restart the service if you've changed it")
                    return
                response.raise_for_status()
    except httpx.HTTPError:
        report(WARN, "not answering. It runs inside the bot: is the pi-assistant service running?")
        return
    report(OK, f"answering at {base}. Sign in with DASHBOARD_TOKEN from .env")
