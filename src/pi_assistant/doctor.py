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
from pi_assistant.config import Config
from pi_assistant.databases import Databases
from pi_assistant.llm import LLMClient
from pi_assistant.mcp_manager import MCPManager
from pi_assistant.memory import Embedder, MemoryStore
from pi_assistant.siri import MIN_TOKEN_CHARS
from pi_assistant.tools import Tool, ToolError
from pi_assistant.trading212 import Trading212

OK, WARN, FAIL = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m"

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
        report(FAIL, f"{type(exc).__name__}: {exc}")
        report(WARN, f"Is Ollama running? Try: ollama pull {cfg.embeddings.model}")
    finally:
        await embedder.close()

    # 3. Memory database --------------------------------------------------------------------
    print(f"\nMemory database ({cfg.db_path})")
    try:
        store = MemoryStore(cfg.db_path, cfg.embeddings.dimensions, cfg.embeddings.model)
        counts = store.count()
        store.close()
        report(OK, f"sqlite-vec loaded; {counts.get('fact', 0)} facts, {counts.get('document', 0)} document chunks")
    except Exception as exc:
        report(FAIL, f"{type(exc).__name__}: {exc}")

    # 4. MCP servers ---------------------------------------------------------------------------
    print("\nMCP servers")
    mcp_tools: list[Tool] = []
    if not cfg.mcp_servers:
        report(WARN, "none configured")
    else:
        mcp = MCPManager(cfg.mcp_servers, cfg.base_dir, cfg.log_dir)
        try:
            await mcp.start()
            for st in mcp.status():
                if st.connected:
                    names = ", ".join(t.name for t in mcp.tools() if t.source == f"mcp:{st.name}")
                    report(OK, f"{st.name}: {st.tools} tool{'' if st.tools == 1 else 's'} ({names})")
                else:
                    report(FAIL, f"{st.name}: {st.error}")
            mcp_tools = mcp.tools()
        finally:
            await mcp.stop()
        disabled = [n for n, c in cfg.mcp_servers.items() if not c.enabled]
        if disabled:
            report(WARN, f"disabled: {', '.join(disabled)}")

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

    # 7. Trading 212 ---------------------------------------------------------------------------
    if cfg.trading212.enabled:
        print(f"\nTrading 212 ({cfg.trading212.environment})")
        await check_trading212(cfg, report)

    # 8. Databases --------------------------------------------------------------------------------
    if cfg.sqlite.databases:
        print("\nDatabases")
        check_databases(cfg, report)

    # 9. The prompt ----------------------------------------------------------------------------
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
        schemas = [tool.schema() for tool in [*services.tools.all(), *mcp_tools]]
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


async def check_trading212(
    cfg: Config, report: Callable[[str, str], None], http: httpx.AsyncClient | None = None
) -> None:
    if not cfg.trading212.api_key:
        report(FAIL, "TRADING212_API_KEY is not set (.env)")
        return
    client = Trading212(cfg.trading212, cfg.agent.timezone, http=http)
    try:
        summary, missing = await client.check()
    except ToolError as exc:
        report(FAIL, str(exc))
        return
    finally:
        await client.close()
    report(OK, f"connected to your {client.account}, {summary.get('id')} in {summary.get('currency')}")
    if missing:
        report(WARN, f"the key can't read: {', '.join(missing)}. Add them in Trading 212, under Settings > API")
    else:
        report(OK, "the key can read everything Athena uses")


def check_databases(cfg: Config, report: Callable[[str, str], None]) -> None:
    for name, path in cfg.sqlite.databases.items():
        file = cfg.resolve(path)
        try:
            tables = Databases(cfg.sqlite, {name: file}).count_tables(name)
        except ToolError as exc:
            report(FAIL, f"{name}: {exc}")
        else:
            report(OK, f"{name}: {tables} table{'' if tables == 1 else 's'}, read-only ({file})")
