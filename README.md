# pi-assistant

A personal AI assistant that runs on your own hardware. A Raspberry Pi hosts the assistant (Telegram bot, agent loop, tools and long-term memory) and calls a local model on a Mac through an OpenAI-compatible API such as [oMLX](https://omlx.ai).

```
 Telegram app                 Raspberry Pi (always on)                         Mac (headless)
 ────────────      ┌──────────────────────────────────────────┐      ┌─────────────────────────────┐
  you  ◄────────►  │ pi-assistant (this repo)                 │      │ oMLX                        │
       long poll   │   Telegram bot ─► agent loop ────────────┼─────►│   Gemma 4 26B A4B (4-bit)   │
                   │                   │  tool calls          │ HTTP │   OpenAI-compatible /v1     │
                   │   memory ◄────────┤                      │      │                             │
                   │   (SQLite + vec)  │  MCP client ─────────┼─────►│ MCP servers for Apple apps  │
                   │                   │      │               │ HTTP │   (optional, see below)     │
                   │ Ollama: embeddinggemma   ▼               │      └─────────────────────────────┘
                   │ MCP servers (stdio): time, fetch, ...    │
                   └──────────────────────────────────────────┘
```

## What it does

- **Chat over Telegram**: long polling, so the Pi needs no open ports. Only Telegram user IDs you list can use it.
- **Tool calling with MCP**: connects to any MCP server, either local (stdio) on the Pi or remote (HTTP) on the Mac. Per-server filters choose which tools the model sees.
- **Approval before actions**: MCP tools show *Allow / Deny* buttons in Telegram, with their full arguments, before they run. Use `confirm` to choose which tools ask; by default they all do.
- **Long-term memory**: the model saves facts with `remember` and looks them up with `search_memory`. Related memories are also added to each message automatically. Embeddings come from EmbeddingGemma on the Pi through Ollama, and are stored in SQLite with [sqlite-vec](https://github.com/asg017/sqlite-vec).
- **Your notes as memory**: `pi-assistant ingest ~/notes` indexes Markdown and text files so the assistant can search them.
- **Fast replies with prompt caching**: the system prompt and earlier messages stay identical between requests, and old history is trimmed in batches. That lets oMLX reuse its cached prompt, which matters a lot on Apple Silicon.
- **SSH-friendly tools**: `pi-assistant doctor` checks every connection, `pi-assistant chat` gives you a terminal chat, and `pi-assistant eval` compares models on tool calling.

## Install on the Pi

You need a Raspberry Pi 4/5 running 64-bit Raspberry Pi OS (Bookworm or later), a model served by oMLX (or any OpenAI-compatible server) that the Pi can reach, and a Telegram bot token from [@BotFather](https://t.me/BotFather) (`/newbot`).

Over SSH:

```bash
git clone <your-repo-url> ~/pi-assistant
cd ~/pi-assistant
bash scripts/install.sh
```

The script installs uv and Ollama, pulls `embeddinggemma`, installs the Python dependencies, turns off `git push` for this clone, creates `config.toml` and `.env`, and registers a systemd service. It doesn't start the service. It's safe to re-run.

Then:

1. **Secrets:** run `nano .env` and set `TELEGRAM_BOT_TOKEN`, plus `LLM_API_KEY` if your oMLX server uses one.
2. **Settings:** run `nano config.toml`. At minimum set:
   - `llm.base_url`: your Mac, for example `http://my-mac.local:8000/v1` or its Tailscale name.
   - `llm.model`: the model id exactly as oMLX lists it. The next step prints the ids if you're unsure.
   - `agent.user_name` and `agent.timezone`.
3. **Check:** run `uv run pi-assistant doctor`. Fix anything marked ✗.
4. **Try it in the terminal:** run `uv run pi-assistant chat`.
5. **Start the bot:** run `sudo systemctl start pi-assistant`, and follow its logs with `journalctl -u pi-assistant -f`.
6. **Lock it down:** message your bot. Because no users are allowed yet, it replies with your Telegram user ID. Put that ID in `telegram.allowed_user_ids` and run `sudo systemctl restart pi-assistant`.

> oMLX must accept connections from the Pi, not just from `localhost`, and should have an API key set. `doctor` tells you if the Pi can't reach it.

## Using it

**Telegram**: send any message. Commands:

| Command | What it does |
|---|---|
| `/reset` | Start a fresh conversation. Long-term memories are kept. |
| `/remember <fact>` | Save a fact directly. |
| `/recall [query]` | Search memories, with similarity scores. With no query it shows the most recent. |
| `/forget <id>` | Delete a memory. |
| `/tools` | List tools and the status of each MCP server. |
| `/reload` | Reconnect to MCP servers, for example after the Mac restarts. |
| `/status` | Model server, memory and tool health. |

**Command line** (run from the repo with `uv run pi-assistant <command>`):

| Command | What it does |
|---|---|
| `run` | Run the Telegram bot. This is what the systemd service runs. |
| `chat` | Chat in the terminal, with the same tools and memory. |
| `doctor` | Check the model server, embeddings, database, MCP servers and Telegram. |
| `ingest PATH...` | Add `.md`/`.txt` files or folders to memory. Re-running a file replaces its old chunks. |
| `reindex` | Re-embed everything after changing the embeddings model. |
| `eval -m MODEL [-m MODEL2]` | Compare models on tool calling (see below). |

## MCP servers

Servers are listed under `[mcp_servers.<name>]` in `config.toml`. Each has either `command` (a local server spoken to over stdio) or `url` (a remote server over Streamable HTTP; URLs ending in `/sse` use the older SSE transport). Optional settings:

- `include`, `exclude` and `confirm` take glob patterns matched against the server's tool names.
- `confirm` lists the tools that ask for your approval before they run. It defaults to `["*"]`, every tool, because any tool might send your data off your network. Set `confirm = []` only for servers that can't, like `time`. If you narrow it to some tools, use `include` too, so tools you haven't checked can't run without asking.
- `headers` adds HTTP headers, such as an auth token.
- `env` passes extra environment variables to a local server. Local servers otherwise get only a minimal environment, so your Telegram token isn't exposed to them.

Keep the tool list short. Every tool's description goes into every prompt, and a long prompt is the slowest part of a request on a Mac.

### On the Pi

These two work out of the box:

```toml
[mcp_servers.time]
command = "uvx"
args = ["mcp-server-time", "--local-timezone=Europe/London"]
confirm = []

[mcp_servers.fetch]
command = "uvx"
args = ["mcp-server-fetch"]
```

Every fetch asks for approval first, because a URL can carry your data to any website.

Each local server's stderr is written to `data/logs/mcp-<name>.log`.

### On the Mac (Apple apps)

Calendar, Reminders, Notes and Messages servers have to run on the Mac itself. Most of them speak stdio, so put a small proxy in front to expose them over HTTP. [mcp-proxy](https://github.com/sparfenyuk/mcp-proxy) is one option:

```bash
# On the Mac. Bind to its LAN or Tailscale address, not 0.0.0.0.
uvx mcp-proxy --host=<mac-ip> --port=8765 <your Apple MCP server command>
```

```toml
# On the Pi
[mcp_servers.mac]
url = "http://my-mac.local:8765/sse"
include = ["list_*", "get_*", "search_*", "create_*"]
confirm = ["create_*"]
```

Tips:

- The first time a server touches Calendar or Contacts, macOS shows a permission prompt. On a headless Mac, approve it over Screen Sharing.
- mcp-proxy has no authentication, so only expose it on a network you trust, such as Tailscale.
- If the Mac restarts, send `/reload` to reconnect.

## Memory

- **Auto-recall:** before each message, the closest memories (up to `recall_top_k`, within `recall_max_distance`) go into a `<context>` block together with the current time. Use `/recall something` to see similarity scores. If unrelated memories keep appearing, lower `recall_max_distance`. If relevant ones are missed, raise it.
- **Saving:** the model saves durable facts on its own, guided by `prompts/system.md`. `/remember` lets you add them yourself.
- **Embeddings model:** `embeddinggemma` (768 dimensions, about 600 MB of RAM) runs on the Pi's CPU through Ollama. To switch models, change `[embeddings]` and run `pi-assistant reindex`. If you change `dimensions`, start a fresh database instead.
- **Storage:** everything (history and memory) lives in `data/assistant.db`. Back up that one file.

## Choosing a model

`eval` runs nine short tool-calling scenarios against each model and reports pass/fail and speed:

```bash
uv run pi-assistant eval -m gemma-4-26b-a4b-it-4bit -m qwen3.6-35b-a3b-4bit --repeat 3
```

It covers picking the right tool, filling in arguments, answering without tools when none is needed, and a two-step task. It only needs the model server, so you can also run it on the Mac.

On an M1 Pro with 32 GB, mixture-of-experts models with about 3–4B active parameters are the right size: dense 27B+ models generate too slowly for multi-step tool use. Larger models may need a higher GPU memory limit, for example `sudo sysctl iogpu.wired_limit_mb=26624`.

## Updating

```bash
cd ~/pi-assistant && git pull && uv sync --no-dev && sudo systemctl restart pi-assistant
```

## Development

```bash
uv sync          # includes the test dependencies
uv run pytest    # offline: fake model server, real sqlite-vec, real MCP servers in subprocesses
```

uv uses its own Python build for this project (`[tool.uv]` in `pyproject.toml`), because some others, including the python.org installer for macOS, can't load sqlite-vec.

| Path | Purpose |
|---|---|
| `src/pi_assistant/agent.py` | Agent loop: prompt building, tool calls, approvals |
| `src/pi_assistant/llm.py` | OpenAI-compatible client |
| `src/pi_assistant/mcp_manager.py` | MCP connections (stdio, Streamable HTTP, SSE) |
| `src/pi_assistant/memory.py` | Embeddings, sqlite-vec store, chunking, memory tools |
| `src/pi_assistant/history.py` | Per-chat history, with cache-friendly trimming |
| `src/pi_assistant/telegram_bot.py` | Telegram handlers and approval buttons |
| `src/pi_assistant/doctor.py`, `evals.py`, `cli.py` | Command-line tools |
| `prompts/system.md` | System prompt. Point `agent.system_prompt_file` at a `*.local.md` copy to customise it privately. |
| `scripts/install.sh`, `deploy/pi-assistant.service` | Pi setup |

## Troubleshooting

- **Start with `uv run pi-assistant doctor`.** Then check `journalctl -u pi-assistant -f` and `data/logs/mcp-*.log`.
- **"Can't reach the model server":** oMLX isn't running, is only listening on localhost, or the hostname doesn't resolve from the Pi. Try `curl http://my-mac.local:8000/v1/models` from the Pi.
- **The model answers instead of using tools:** check that tool calling works in `doctor`, then compare models with `eval`.
- **The bot ignores you:** your ID isn't in `telegram.allowed_user_ids`. Empty the list temporarily to get your ID.
- **Telegram "Conflict: terminated by other getUpdates request":** two copies of the bot are running with the same token.

## Security notes

- Only allowlisted Telegram users can use the bot. Messages from anyone else are ignored and logged.
- Secrets stay in `.env` (mode 600). `config.toml`, `.env` and `data/` are git-ignored.
- `install.sh` turns off `git push` in the Pi's clone (`scripts/disable-git-push.sh`), so nothing on the Pi can be pushed to GitHub. `git pull` still works.
- Text from tools, such as fetched web pages, is untrusted: it can tell the model to send your data somewhere. That's why MCP tools ask first by default and the approval message shows their full arguments. Only set `confirm = []` on servers that can't send data off your network.
- Telegram bot chats aren't end-to-end encrypted, so messages pass through Telegram's servers even though the model is local.

## License

MIT
