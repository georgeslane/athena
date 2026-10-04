"""`pi-assistant doctor`: check every connection the assistant depends on."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import httpx

from pi_assistant.config import Config
from pi_assistant.llm import LLMClient
from pi_assistant.mcp_manager import MCPManager
from pi_assistant.memory import Embedder, MemoryStore
from pi_assistant.siri import MIN_TOKEN_CHARS

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

    print("\nAll good." if healthy else "\nSome checks failed (see above).")
    return healthy


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
