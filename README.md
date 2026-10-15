<img src="src/pi_assistant/assets/athena.svg" alt="Athena's shield: a bronze hoplite shield bearing an owl on an olive branch" width="128" align="right">

# pi-assistant

A personal AI assistant that runs on your own hardware. A Raspberry Pi hosts the assistant (Telegram bot, agent loop, tools and long-term memory) and calls a local model on a Mac through an OpenAI-compatible API such as [oMLX](https://omlx.ai).

```
 Telegram app                 Raspberry Pi (always on)                         Mac (headless)
 ────────────      ┌──────────────────────────────────────────┐      ┌─────────────────────────────┐
  you  ◄────────►  │ pi-assistant (this repo)                 │      │ oMLX                        │
       long poll   │   Telegram bot ─► agent loop ────────────┼─────►│   Gemma 4 26B A4B (4-bit)   │
                   │                   │  tool calls          │ HTTP │   EmbeddingGemma 2          │
                   │   memory ◄────────┤                      │      │   OpenAI-compatible /v1     │
                   │   (SQLite + vec)  │  MCP client ─────────┼─────►│ Calendar and Reminders      │
                   │       ▲           │      │               │ SSH  │   (iMCP)                    │
                   │       │                  ▼               │      └─────────────────────────────┘
                   │       │   MCP servers: time, fetch, ...  │      ┌─────────────────────────────┐
                   │   documents indexer ─────────────────────┼─────►│ Your laptop: documents      │
                   │   (when you ask, on the dashboard)       │ SSH  │   (read-only)               │
                   └──────────────────────────────────────────┘      └─────────────────────────────┘
```

## What it does

- **Chat over Telegram**: long polling, so the Pi needs no open ports. Only Telegram user IDs you list can use it.
- **Ask by voice**: say "Hey Siri, Ask Athena" on your iPhone or Mac. Siri reads the answer out, and your question and the answer also appear in the Telegram chat.
- **Tool calling with MCP**: connects to any MCP server, on the Pi, on your Mac over SSH, or remote over HTTP. Per-server filters choose which tools the model sees.
- **A general assistant**: [recommended servers](#mcp-servers) let it use your Calendar and Reminders, read the news, look up company filings and GitHub repos, and handle an email address of its own. Built-in tools read your [Trading 212](#trading-212) account and place the trades you approve, and query [SQLite databases](#databases) you choose.
- **Approval before actions**: MCP tools show *Allow / Deny* buttons in Telegram, with their full arguments, before they run. Use `confirm` to choose which tools ask; by default they all do.
- **Long-term memory**: the model saves facts with `remember` and looks them up with `search_memory`, which also finds what you talked about in earlier sessions, and your documents. Related facts are added to each message automatically. Embeddings come from [EmbeddingGemma 2](#embeddings), served by oMLX on the Mac with the model, and are stored in SQLite on the Pi with [sqlite-vec](https://github.com/asg017/sqlite-vec).
- **Sessions**: `/session` clears the conversation from the model's context, so replies stay quick, while the memory keeps everything you've talked about.
- **Tasks it does by itself**: a briefing every weekday morning, research while you're busy, or a weekly look for MCP servers that would help. Athena never starts one while you're chatting, and stops for you if you start. It messages you what it found. See [Tasks](#tasks).
- **Your documents, searched by meaning**: choose folders on your Mac in the dashboard, and Athena indexes them when you ask. Ask it in Telegram about something in them, and it says which file and can summarise it. The dashboard's search finds them too. See [Documents](#documents).
- **Fast replies with prompt caching**: the system prompt, tools and earlier messages stay identical between requests, and old history is trimmed in batches. That lets oMLX reuse its cached prompt, which matters a lot on Apple Silicon. The assistant also keeps that cache warm, reading each reply into it straight away, so your messages rarely wait for it, and `doctor` measures how long your Mac takes to read the prompt.
- **A dashboard**: a web page for your iPhone or Mac, over Tailscale. It shows what Athena is doing, how much it's used and how full the model's context is, for the session and in total. From it you can switch tools and MCP servers on and off, set them up and give them keys, without editing `config.toml`. See [Dashboard](#dashboard).
- **A status board**: with a Pimoroni Display HAT Mini, the Pi's screen shows what the assistant is doing: idle, working on your request, or waiting for your approval. The board is its own service, [pi-display-microservice](#status-board), which reads Athena's status API.
- **SSH-friendly tools**: `pi-assistant doctor` checks every connection, `pi-assistant chat` gives you a terminal chat, and `pi-assistant eval` compares models on tool calling.

## Install on the Pi

You need a Raspberry Pi 4/5 running 64-bit Raspberry Pi OS (Bookworm or later), a model served by oMLX (or any OpenAI-compatible server) that the Pi can reach, with the [embeddings model](#embeddings) beside it, and a Telegram bot token from [@BotFather](https://t.me/BotFather) (`/newbot`).

Over SSH:

```bash
git clone <your-repo-url> ~/pi-assistant
cd ~/pi-assistant
bash scripts/install.sh
```

The script installs uv and the Python dependencies, turns off `git push` for this clone, creates `config.toml` and `.env`, and registers a systemd service. It doesn't start the service. It's safe to re-run. For a Display HAT Mini, see [Status board](#status-board).

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
7. **Open the dashboard** on your phone or Mac: see [Dashboard](#dashboard).

> oMLX must accept connections from the Pi, not just from `localhost`, and should have an API key set. `doctor` tells you if the Pi can't reach it.

## Using it

**Telegram**: send any message. Commands:

| Command | What it does |
|---|---|
| `/session` | Start a new session: the conversation so far is cleared from the model's context, in every chat. Memories are kept, and earlier conversations can still be searched. `/reset` does the same. |
| `/stop` | Stop the answer Athena is working on, and any messages waiting their turn. |
| `/retry` | Answer your last message again, say after changing how much Athena thinks. If it's already been answered, that answer is taken out of the conversation and memory first. While Athena's still answering, it stops first. |
| `/tasks` | What Athena does by itself, with buttons to run, pause, resume, stop or delete each. See [Tasks](#tasks). |
| `/think [on\|short\|off]` | How much the model thinks before answering: on, briefly (`short`), or not at all. With no choice, it says which it is, with buttons to change it. It's saved in `config.toml`. |
| `/remember <fact>` | Save a fact directly. |
| `/recall [query]` | Search memories, with similarity scores. With no query it shows the most recent. |
| `/forget <id>` | Delete a memory. |
| `/tools` | List tools and the status of each MCP server. |
| `/reload` | Read the tools' settings in `config.toml` again and reconnect to MCP servers, for example after the Mac restarts. |
| `/status` | Model server, memory and tool health. |

**Command line** (run from the repo with `uv run pi-assistant <command>`):

| Command | What it does |
|---|---|
| `run` | Run the Telegram bot. This is what the systemd service runs. |
| `chat` | Chat in the terminal, with the same tools and memory. |
| `doctor` | Check the model server, embeddings, database, MCP servers and Telegram. |
| `ingest PATH...` | Add `.md`/`.txt` files or folders to memory. Re-running a file replaces its old chunks. |
| `reindex` | Re-embed everything with the configured embeddings model. |
| `embeddings test [-m MODEL]` | Test how well an embeddings model finds memories, and the cut-offs that suit it (see [Changing the embeddings model](#changing-the-embeddings-model)). |
| `embeddings use MODEL` | Test a model against the current one, then switch to it and re-embed everything. Stop Athena first, or use `scripts/switch-embeddings.sh`. |
| `eval -m MODEL [-m MODEL2]` | Compare models on tool calling (see below). |

## Dashboard

A web page for Athena, on your iPhone or Mac. It has four tabs:

- **Status:** what Athena is doing, as on the status board, and how much it's used: messages answered, tool calls by tool and the average reply time, for this session and in total. It shows how much of the model's context the conversation takes up, out of the most the model can read, and what's in Athena's memory. Under Connections, **Thinking** switches the model's thinking on, short or off, as `/think` does.
- **Tasks:** what Athena does by itself: add tasks, from scratch or from examples, run them now, pause, resume or delete them, and read what each said last time. See [Tasks](#tasks).
- **Memory:** search what Athena remembers, your past conversations and your documents, by meaning. Choose the folders on your Mac to index, and index them: see [Documents](#documents).
- **Tools:** switch the built-in tools and MCP servers on and off, change their settings and give them keys. For a connected server you can choose which of its tools Athena uses and which ask first, and you can add servers of your own. Changes go into `config.toml`, keeping your comments, and keys into `.env`. They apply straight away, once Athena has finished anything it's in the middle of, without a restart.

It follows your device's light or dark mode. In Safari on an iPhone, Share > Add to Home Screen makes it an app.

### Set it up

The bot serves the dashboard on the Pi only, at `http://127.0.0.1:8092`. Tailscale Serve makes it reachable from your own devices, and from nowhere else, over HTTPS:

```bash
sudo tailscale serve --bg --https=443 localhost:8092
grep DASHBOARD_TOKEN .env   # the password, which Athena made the first time it started
```

Then open `https://<the Pi's tailnet name>/`, such as `https://pi.tail1234.ts.net/`, and sign in. If HTTPS isn't on for your tailnet yet, the first command gives a link to turn it on. Siri's endpoint keeps working alongside it, on plain HTTP.

You stay signed in for 90 days. To change the password, set `DASHBOARD_TOKEN` in `.env` to at least 12 characters and restart the service. That signs every device out.

### Sessions and context

Athena keeps the conversation going from one message to the next, so each reply can build on the last. All of it counts towards the model's context, and the more there is, the longer the Mac takes to read it. A **session** is everything since you last started one: send `/session` in Telegram, or press **New session** on the dashboard. The conversation is cleared from the model's context in every chat, and Athena carries on with its memories. Every exchange is also kept in memory, so Athena can still search what you talked about in earlier sessions.

**Context** on the dashboard is what Athena reads before your next message: the system prompt, the tools' descriptions and this session's conversation. Athena measures it after each reply, when it warms up the model server's cache. The limit comes from the model server. If yours doesn't say, set `llm.context_window` in `config.toml`.

**Forget everything**, under Memory, deletes every memory: saved facts, the chunks of your notes, past exchanges and the conversation history. It also starts a new session. Usage statistics stay, because they're only counts and timings.

## Status board

With a [Pimoroni Display HAT Mini](https://pinout.xyz/pinout/display_hat_mini) on the Pi, a status board shows what Athena is doing:
- idle, working on your request, or waiting for your approval
- an LED that's blue while Athena works, and flashes amber when it needs you

The board is its own project, [pi-display-microservice](https://github.com/georgeslane/pi-display-microservice), with its own installer, updates and settings.

It asks Athena what it's doing through a small HTTP API. So either can be restarted, updated or replaced without the other, and when nothing answers, the board knows Athena isn't running.

Athena's side is the `[display]` section of `config.toml`:

- **Where it listens:** the API is on by default, and only listens on the Pi, at `http://127.0.0.1:8091/v1/status`. To serve a board on another machine, set `host`, and a `token` for the board to send.
- **Your messages:** `show_task = false` keeps them out of the API, and so off the screen.
- **The board's own settings,** like the LED, are in pi-display-microservice's `config.toml`.

The API is one request, `GET /v1/status`, answered with JSON. A board can ask it to wait for the next change (`?wait=25&after=<version>`), so it hears about each change straight away without asking all the time. pi-display-microservice's README has the full format, and how to add information to it for new features. `doctor` checks the API is answering.

Only one process can serve it at a time. Normally that's the service; `pi-assistant chat` only serves it when the service isn't running.

**Moving from the built-in board.** The board used to be part of this repo, as `pi-assistant display` and the `pi-assistant-display` service. To switch:
1. Update Athena with `scripts/update.sh`. It stops that service.
2. Install pi-display-microservice on the Pi (see its README).
3. Remove `led` from `[display]` here.

## Siri

Say **"Hey Siri, Ask Athena"** on your iPhone or Mac, then ask your question. Siri reads the answer out. Your question and the answer also go to the Telegram chat, so you can follow up there.

An Apple Shortcut sends what you said to the Pi over [Tailscale](https://tailscale.com), with a token. Athena answers just as if you'd typed it in Telegram, with the same memory, tools and approvals.

- **Approvals still happen in Telegram.** If a tool needs your OK, Siri tells you to look there.
- **Siri waits about 25 seconds at most.** If the answer takes longer, Siri says it'll be in Telegram, where it arrives when it's ready.
- **In Telegram, the bot posts your question** as "🎙️ You, via Siri", because a bot can't post messages as you.
- **Say the shortcut's name first, then your question.** Siri doesn't take the question in the same breath for a shortcut you made yourself.

Your iPhone and Mac need Tailscale, on the same tailnet as the Pi.

### Set it up

1. **On the Pi**, make a token and add it to `.env`:

   ```bash
   cd ~/pi-assistant
   echo "SIRI_TOKEN=$(openssl rand -hex 24)" >> .env
   grep SIRI_TOKEN .env   # you'll need it for the shortcut
   ```

   In `config.toml`, set `enabled = true` under `[siri]`. Then:

   ```bash
   sudo tailscale serve --bg --http=80 localhost:8090
   sudo systemctl restart pi-assistant
   uv run pi-assistant doctor   # the Siri section should say "listening"
   tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'
   ```

   The last command prints the Pi's name on your tailnet, such as `pi.tail1234.ts.net`. Athena's endpoint only listens on the Pi itself, and Tailscale Serve passes requests to it from devices on your tailnet, and from nowhere else. Plain HTTP is fine, because Tailscale encrypts the connection. `--bg` keeps it on after restarts; `sudo tailscale serve reset` turns it off.

2. **In the Shortcuts app** on your iPhone or Mac (iCloud syncs it to the other), make a shortcut called **Ask Athena** with three actions:

   1. **Ask for Input**: type *Text*, prompt *What do you want to ask?* When Siri runs the shortcut, you answer by voice.
   2. **Get Contents of URL**: `http://<the Pi's tailnet name>/ask`. Under *Show More*, set *Method* to *POST*. Add a header named `Authorization` with the value `Bearer <your SIRI_TOKEN>`. Set *Request Body* to *JSON* and add a *Text* field named `prompt`, set to *Provided Input*.
   3. **Show Result**, showing *Contents of URL*. Siri reads it out.

3. **Try it:** "Hey Siri, Ask Athena". To test from a Mac's terminal instead:

   ```bash
   curl -H "Authorization: Bearer <your SIRI_TOKEN>" -H "Content-Type: application/json" \
     -d '{"prompt": "What time is it?"}' http://<the Pi's tailnet name>/ask
   ```

The token is stored in the shortcut, which iCloud syncs between your devices. Take it out before sharing the shortcut with anyone. To change it, update `.env` and the shortcut, then restart the service.

## MCP servers

The [dashboard](#dashboard)'s Tools tab is the easy way to switch servers on and off, set them up and add new ones: it edits `config.toml` for you. This section is about the settings behind it, for doing it by hand.

Servers are listed under `[mcp_servers.<name>]` in `config.toml`. Each has either `command` (a local server spoken to over stdio) or `url` (a remote server over Streamable HTTP; URLs ending in `/sse` use the older SSE transport). Optional settings:

- `include`, `exclude` and `confirm` take glob patterns matched against the server's tool names.
- `confirm` lists the tools that ask for your approval before they run. It defaults to `["*"]`, every tool, because any tool might send your data off your network. If you narrow it to some tools, use `include` too, so tools you haven't checked can't run without asking.
- `headers` adds HTTP headers, such as an auth token.
- `env` passes extra environment variables to a local server. Local servers otherwise get only a minimal environment, so your Telegram token isn't exposed to them.
- `enabled = false` keeps a server's settings without starting it.

After changing servers by hand, send `/reload` in Telegram, which reads the tools' settings in `config.toml` again, or press **Reconnect all** on the dashboard. Check them with `uv run pi-assistant doctor`. Each local server's stderr is written to `data/logs/mcp-<name>.log`. If a server on the Mac stops answering, for example after the Mac restarts, send `/reload`.

### How servers are installed

Pinning `mcp-server-fetch==2026.8.18` pins the server, but not the 44 packages it depends on, which `uvx` would install at whatever version is newest. So Athena doesn't run servers through `uvx`. Each server whose `command` is `uvx` gets a small project of its own in `data/mcp/<name>/`, whose `uv.lock` records the exact version and hash of every package, and Athena runs the server from that.

- **Reviewed locks:** the recommended servers' locks are in `mcp-locks/` in this repo, so any change to them shows up in a pull request. Athena uses one when it's for the version in your `config.toml`. Any other server is locked the first time it starts, and stays that way until you change its version.
- **Installing:** every package's hash is checked, only ready-built packages (wheels) are installed, so no package's own code runs while it's installed, and uv asks [OSV](https://osv.dev) whether any of them is known malware first. If one is, or OSV can't be reached, the server isn't installed. That happens once for each lock: after that, starting a server needs no network.
- **Known vulnerabilities:** `doctor` checks each server's locked packages against OSV, and so does CI, for Athena and the recommended servers, on every push and every Monday. A failure emails you.
- **What isn't locked:** servers started with `uvx` options other than `--from` (doctor says which), and servers started another way, such as `npx`, `ssh` or a URL.

After changing a recommended server's version in `config.example.toml`, remake its lock with `uv run python -m pi_assistant.server_envs`.

### When a server's tools change

A tool's description and arguments go into every prompt, and a server can change them whenever it's updated: a hosted server like GitHub's at any time. A changed description can carry instructions to the model, so Athena remembers each tool as it was when you approved it. A tool that's new, or has changed since then, is held back from the model until you approve it. Athena sends you a Telegram message about it, and the server's card on the dashboard says **waiting for you**. Open the card to see what changed, then press **Approve**. Tools that haven't changed keep working meanwhile, and so do tools the config hides, which aren't checked until you use them.

A server's tools are approved as they are the first time it connects, since you've just chosen it and can see them on the dashboard. Removing a server forgets its approvals.

### Sandbox

Local servers run in a sandbox made with [bubblewrap](https://github.com/containers/bubblewrap), which `install.sh` installs. Inside it, a server sees:

- the system, read-only
- an empty home folder of its own, wiped when it stops, so your `.env`, memories (`data/`), Athena's code and `~/.ssh` aren't there
- its own `/tmp`
- read-only, only what it needs to run: its locked environment, uv's Pythons, the folder its command is in, and `~/.ssh` if the command is `ssh` (for the Mac's servers, where the program is OpenSSH itself). ssh there doesn't see `/etc/ssh/ssh_config.d`, as it would refuse the files in it: in the sandbox, root's files belong to nobody.

Nothing it starts outlives it.

- **`network = false`** cuts it off from the network too. The time server has this.
- **Network on means everywhere.** A server with network access can reach anything the Pi can, including services on the Pi itself, such as Athena's status API. The sandbox can't limit it to particular sites.
- **`read_only_paths = ["/home/pi/notes"]`** lets a server read files or folders it needs.
- **`sandbox = false`** runs a server without one. It can then read everything you can, so `doctor` warns about it.

`doctor` checks the sandbox for real. It writes a file in your home folder and checks that a sandboxed process can't see it, nor connect out without network access, and that Athena's own Python, set up by uv like a server's, starts in one, as does ssh. If bubblewrap is missing, or can't make a sandbox, local servers aren't started, and the dashboard says why. On a Mac, which bubblewrap doesn't support, servers run without one.

### Recommended servers

The dashboard lists each of these, ready to switch on, and `config.example.toml` has them ready to copy into your `config.toml`, switched off until you set them up. To add others, see [Adding a server](#adding-a-server).

| For | Server | Runs on | Asks first |
|---|---|---|---|
| The time | `mcp-server-time` | Pi | Never |
| Web pages | `mcp-server-fetch` | Pi | Every fetch |
| Web search | [DuckDuckGo](https://github.com/nickclyde/duckduckgo-mcp-server) | Pi | Every search |
| [Calendar and Reminders](#your-macs-calendar-reminders-and-documents) | [iMCP](https://github.com/mattt/iMCP) | Mac, over SSH | Adding events and reminders |
| [News](#news) | `read_news`, built in | Pi | Never |
| [Trading 212](#trading-212) | `trading212_*`, built in | Pi | Placing and cancelling orders |
| [Databases](#databases) | `database_*`, built in: read-only SQLite | Pi | Never |
| [Company filings](#company-filings) | [EdgarTools](https://github.com/dgunning/edgartools) | Pi | Never |
| [GitHub](#github) | [GitHub's own server](https://github.com/github/github-mcp-server): read-only | GitHub | Never |
| [Email](#email) | [mcp-email-server](https://github.com/Wh1isper/mcp-email-server) | Pi | Sending |

What asks first follows one rule: a tool asks if it can change something, or can send what's in the conversation somewhere someone else could read it. Reading your own files, calendar and databases keeps everything on your network, and reading your Trading 212 account only asks Trading 212 for what it already holds. Looking things up in public sources (your news feeds, the SEC, GitHub's public repos) sends only the lookup itself, to that service. Fetching a URL, searching the web and sending email can reach anyone, and adding a calendar event can email an alarm to any address, so those ask. To make any server ask first, set `confirm = ["*"]` on it.

Versions are pinned, as in `mcp-email-server==1.11.0`, so a new release can't change what runs on your Pi until you choose to update. So are the packages each server depends on: see [How servers are installed](#how-servers-are-installed).

Tools only run when the model uses them, but every tool's description is part of every prompt, so the model knows what it can use. With everything above switched on, the descriptions come to about 10,000 tokens, against about 1,300 for the system prompt and the first three servers. Email, SEC filings and GitHub account for two-thirds of that, while Trading 212's five tools add about 650 and the database tools about 200.

On most messages that costs almost nothing, because oMLX caches the part of the prompt it has already read, and the assistant keeps the cache warm: at startup, after each reply, when the tools change, and every 10 minutes (`llm.warm_up_minutes`). But when the cache is cold, for example just after the Mac restarts, the next message waits while the Mac reads the whole prompt again. On an M1 Pro that could be a minute with every server on. To see what yours costs, run `doctor`: its last section measures how many tokens the tools add, how long your Mac takes to read them, and whether oMLX's cache is working. If it's slow, switch off servers you rarely use, or narrow their `include` lists.

### Your Macs: Calendar, Reminders and documents

Athena reaches your Macs over SSH, with a key of its own that each Mac limits to Athena's servers there: no shell, no port forwarding, nothing else. On each Mac, launchd starts a server for each connection inside your login session, so macOS asks for access to your calendars or folders as it would for any app. Each Mac offers what you choose:

- **Calendar and Reminders**, through [iMCP](https://github.com/mattt/iMCP), on a Mac that's always on, such as the one running oMLX. The Pi calls it `athena-mac`.
- **Your documents**, on the Mac they're on, such as your laptop, which Athena reads only when you index them. The Pi calls it `athena-laptop`. See [Documents](#documents).

One Mac can offer both.

1. **On the Pi**, make the key (just once) and an SSH entry for each Mac, using its name on your tailnet and your user name on it. Name the one with your documents `laptop`:

   ```bash
   bash scripts/connect-mac.sh my-mac.tail1234.ts.net georges            # athena-mac
   bash scripts/connect-mac.sh my-laptop.tail1234.ts.net georges laptop  # athena-laptop
   ```

   Each prints the command for step 3, with the Pi's public key in it.
2. **On each Mac**, clone this repo and turn on Remote Login: System Settings > General > Sharing > Remote Login, allowing only your user. For Calendar, also install iMCP: `brew install --cask mattt/tap/iMCP`.
3. **On each Mac**, in the repo, run the command step 1 printed, saying what that Mac offers: `--calendar`, `--documents`, or both:

   ```bash
   bash scripts/mac/install.sh --pi-key "ssh-ed25519 AAAA... athena@pi" --calendar    # the Mac that's always on
   bash scripts/mac/install.sh --pi-key "ssh-ed25519 AAAA... athena@pi" --documents   # your laptop
   ```

4. **For Calendar**, open iMCP on that Mac. Turn on Calendar and Reminders only, allow access when macOS asks, and turn on "Start at login" in its settings. That Mac needs to stay on and logged in.
5. **On the Pi**, check each link: `ssh athena-mac hello` and `ssh athena-laptop hello` should each say there's no server called 'hello', which means the key works. Then switch on **Calendar and Reminders** on the dashboard's Tools tab, choose folders under **Memory**, and run `doctor`.
6. **The first time** the Pi uses each server, macOS asks on that Mac for access: to the local network for "imcp-server", and to your folders for "python3". Allow them, over Screen Sharing if the Mac has no screen.

After updating the repo on a Mac, run `bash scripts/mac/install.sh` again: without options, it keeps what that Mac offers and the Pi's key. A Mac set up before documents had a files server, which searched folders with Spotlight. Running the script again removes it. On the Pi, remove it too: on the dashboard's Tools tab, open **files** and press **Remove server**. `bash scripts/mac/install.sh --uninstall` removes everything, including the Pi's key.

**A laptop with Remote Login on** can be reached on any network it joins, and macOS lets you sign in over SSH with your password as well as with a key. To allow keys only, for every SSH login to the laptop, not just Athena's, run this on it:

```bash
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' | sudo tee /etc/ssh/sshd_config.d/050-keys-only.conf
```

### News

`read_news` reads the news feeds (RSS or Atom) listed under `[news.feeds]` in `config.toml`, and only those. All the model chooses is which feed to read and how many items, so it can't be used to send anything anywhere, and doesn't ask first. Most news sites have a feed: add their address with any name you like. To read a whole article, the model uses `fetch`, which asks first.

### Trading 212

`trading212_portfolio`, `trading212_history` and `trading212_find_instrument` read your Trading 212 account: its value and cash, each holding with its profit or loss, pending orders, and past orders, dividends, deposits and withdrawals. They don't ask first. They only fetch your own account from Trading 212, and the instrument search runs on a list the Pi keeps, so what you search for isn't sent anywhere.

`trading212_place_order` and `trading212_cancel_order` always ask first, and can't be set not to. The approval message says in words what Trading 212 calls the instrument and where it trades, the price terms, which account, how many you hold and, when it can, roughly what the order comes to, above the exact request. That way you'll notice if the model picked the wrong listing, such as Apple in euros instead of dollars. An order Trading 212 would refuse, such as selling shares you don't have, is stopped before you're asked. An approved order is sent once and never retried, because Trading 212 would place a repeated order twice. If its answer gets lost, Athena is told the order may or may not have gone through, and to check before trying again.

To set it up:

1. In the Trading 212 app, go to **Settings > API (Beta)** and generate a key. The API works with Invest and Stocks ISA accounts.
2. Choose its permissions: account data, portfolio, history, metadata and reading orders. To trade, also allow executing orders. Leave pies off: Athena doesn't use them.
3. Restrict it to trusted IPs, and give your home's public address, which `curl -s https://api.ipify.org` on the Pi prints. Then the key is useless anywhere else. If your provider changes your address, the key stops working until you update it, and `doctor` will tell you.
4. On the dashboard's Tools tab, open **Trading 212**, paste the key and its secret, and switch it on. (Or put them in `.env` as `TRADING212_API_KEY` and `TRADING212_API_SECRET`, set `enabled = true` under `[trading212]` in `config.toml` and send `/reload`.) Then run `doctor`: it checks the key and lists any permissions it's missing.

Orders are for a number of shares, which can be a fraction: Trading 212's API doesn't take orders by amount. Limit and stop prices are in the instrument's own currency, which is pence (GBX) for most London shares. The API only gives prices for what you hold, so for anything else ask Athena to look the price up first. To practise, make a key in Trading 212's practice mode and set `environment = "demo"`.

### Databases

`database_tables` and `database_query` read the SQLite databases listed under `[sqlite.databases]` in `config.toml`, by the names you give them, and no other files. The files must be on the Pi. A path can be absolute, or relative to the config's folder.

They can only read, and SQLite itself enforces that. Each file is opened read-only, and an authorizer refuses everything but reading before a statement runs. Both are needed: a read-only connection alone would still let `VACUUM INTO` copy the database anywhere, and `ATTACH` create files. Statements that don't start with SELECT, WITH or VALUES aren't tried at all, a query is stopped after 10 seconds, and at most 100 rows come back. So they don't ask first, and they're safe to use on a database another program is writing to.

### Company filings

[EdgarTools](https://github.com/dgunning/edgartools) searches and reads filings to the US Securities and Exchange Commission: annual and quarterly reports, financial statements, insider trades and fund holdings. The SEC asks callers to say who they are, so give your name and email, like `Jane Doe jane@example.com`: on the dashboard, or as `EDGAR_IDENTITY` in `.env`.

It needs about 300 MB of Python packages, mostly data libraries, so the first time it starts takes a few minutes on a Pi while they're installed. The dashboard shows it as starting until then.

### GitHub

This uses the server GitHub hosts, in read-only mode with lockdown on. Lockdown hides text in issues and pull requests from people without push access to the repo, which is where instructions aimed at an assistant would most likely be hidden.

Make a [fine-grained token](https://github.com/settings/personal-access-tokens/new) with **Public repositories (read-only)** access and an expiry date, and paste it into **GitHub** on the dashboard, or set it as `GITHUB_TOKEN` in `.env`. It can't see your private repositories or change anything, even if someone got hold of it.

### Email

Give the assistant an email address of its own, with any provider that offers IMAP, SMTP and app passwords, such as Fastmail, iCloud Mail or Gmail with 2-Step Verification. Then it can't read your own mail, and its password only opens its own mailbox. Forward it anything you'd like it to read.

On the dashboard, open **Email** and fill in the address, your provider's IMAP and SMTP servers and the app password, which goes in `.env` as `EMAIL_PASSWORD`. Or put them in the `email` block of `config.toml` yourself. Two settings in that block are enforced by the email server itself, whatever the model asks for and whatever you approve:

- `MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS`: who it may email, comma-separated, with `*` as a wildcard. Start with just your own address.
- `MCP_EMAIL_SERVER_ALLOWED_MUTATIONS = "send"`: it can send, but never delete, move, flag or draft.

Sending always asks first. Check the recipients, and the `attachments` list: it can attach any file the Pi can read. To have it only see mail from you, also set `MCP_EMAIL_SERVER_ALLOWED_SENDERS` to your own addresses.

### Adding a server

To give the assistant a new ability, look for an MCP server that provides it. Most are listed in the [MCP Registry](https://registry.modelcontextprotocol.io), and the assistant can search it for you: ask it to fetch `https://registry.modelcontextprotocol.io/v0/servers?search=<topic>`.

1. **Choose carefully.** A server sees whatever the assistant passes it and can act on your behalf. Prefer one from the service's own makers, or one that's widely used and recently updated, and read what each of its tools does.
2. **Give it as little as possible.** Use its read-only mode if it has one, and give it a token or account that can only do what you need. Use any limits the server itself enforces, like the email server's allowed recipients.
3. **Add it** on the dashboard (Tools > Add a server), or to `config.toml`:

   ```toml
   [mcp_servers.weather]
   command = "uvx"                                   # a Python server from PyPI
   args = ["some-weather-mcp==1.2.3"]                # pinned to a version you've checked
   env = { WEATHER_API_KEY = "${WEATHER_API_KEY}" }  # secrets go in .env
   include = ["get_forecast"]                        # only the tools you need
   confirm = []                                      # see below
   ```

   - **npm servers:** these need Node.js on the Pi (`sudo apt install nodejs npm`). Use `command = "npx"` and `args = ["-y", "some-server@1.2.3"]`.
   - **Hosted servers:** these take `url` and `headers` instead, like the GitHub one.
   - **Servers that need your Mac's apps or files** run on the Mac. Add a line to `SERVERS` at the top of `scripts/mac/install.sh` and run it again on the Mac with `--server <its name>`. Then on the Pi, use `command = "ssh"` and `args = ["athena-mac", "<its name>"]`.
   - **`confirm`:** leave it out, so every tool asks first, unless the tools only read your own data or look things up in public sources. Anything that changes something, or can send to anyone (email, messages, any URL), should ask.
4. **Check it:**
   - On the dashboard, open the server to see its tools, and choose which Athena can use and which ask first. Or send `/reload`, then run `uv run pi-assistant doctor`, which lists the server's tools and shows how much they add to every prompt.
   - Send `/tools` in Telegram to see them there too.
   - Run `uv run pi-assistant eval -m <your model>` to check the model still picks the right tools.

To update a server later, read what changed, change its pinned version and restart: Athena locks the new version's dependencies when it starts it. If no safe server exists for something, the assistant can have a built-in integration instead, like the news reader in `src/pi_assistant/news.py`. Each is one class in a module of its own, which says what it needs: its section of `config.toml` and the settings in it, how the dashboard shows it, how it starts, the tools it gives the model, and `doctor`'s check of it (see `src/pi_assistant/builtin.py`). Adding the class to `BUILTINS` in `src/pi_assistant/integrations.py` is then all it takes for Athena to run it, the dashboard to show it and `doctor` to check it. Each tool is a name, a description, a JSON schema for its arguments and a Python function. A tool that asks first can also have a `preview` function, which checks the arguments and says in words what the call will do, for the approval message. `trading212_place_order` has one.

## Memory

- **Auto-recall:** before each message, the closest facts (up to `recall_top_k`, within `recall_max_distance`) go into a `<context>` block together with the current time. Use `/recall something` to see similarity scores. If unrelated memories keep appearing, lower `recall_max_distance`. If relevant ones are missed, raise it.
- **Saving:** the model saves durable facts on its own, guided by `prompts/system.md`. `/remember` lets you add them yourself.
- **Past conversations:** after each reply, your message and the reply are added to memory, in an index of their own. `search_memory` finds them, so Athena can look up what you talked about in an earlier session, but they aren't added to messages automatically, so an old answer can't be mistaken for a current one. Anything said while the embeddings server was down is added when it's back.
- **Forgetting:** `/forget <id>` deletes one memory. **Forget everything** on the dashboard deletes them all, with the conversation history.
- **Duplicates:** a fact closer than `duplicate_distance` to one already saved isn't saved again.
- **Documents:** passages of your documents, indexed from your Mac when you ask (see [Documents](#documents)), and any notes you add with `pi-assistant ingest`. They're searched when the model asks, not added to every message.
- **Embeddings model:** EmbeddingGemma 2 (768 dimensions), on the Mac: see [Embeddings](#embeddings).
- **Storage:** everything (history, memory and usage statistics) lives in `data/assistant.db`. Back up that one file.

### Embeddings

Memories, past conversations and documents are searched by meaning, with vectors from an embeddings model. Athena uses [EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2), served by oMLX on the Mac, the same server as the model. So `[embeddings]` needs no address or key of its own: with `base_url` and `api_key` empty, it uses `[llm]`'s.

1. **Update oMLX** to 0.7.1 or later: the first version that serves EmbeddingGemma 2.
2. **Download the model:** in oMLX, add `mlx-community/embeddinggemma-2-bf16` (1.5 GB) beside your model.
3. **Check it:** on the Pi, run `uv run pi-assistant embeddings test`. If oMLX calls it something else, the error lists the names it has. Put the right one in `embeddings.model`.

The Mac does this much faster than the Pi could. Indexing a folder of documents takes minutes there, not hours. And it leaves the Pi's memory free. Athena already needs the Mac for the model, so this adds nothing new for it to depend on.

### Changing the embeddings model

Every memory is stored as a vector from the embeddings model, and vectors from different models can't be compared, so switching means re-embedding all of them. That includes another quantisation of the same model: a 4-bit build's vectors drift enough from the original's to throw off the cut-offs. Each model also scores distances on its own scale, so `recall_max_distance` and `duplicate_distance` change with it. One script does all of this:

```bash
bash scripts/switch-embeddings.sh embeddinggemma-2-8bit           # another model where embeddings come from now
bash scripts/switch-embeddings.sh embeddinggemma-2-bf16 --on-mac  # moving embeddings to the model's server (oMLX)
```

For a model on the Mac, download it in oMLX first. For a model in Ollama on the Pi, the script downloads it, offering to update Ollama if it's too old for it. Then it stops Athena, and tests the new model against the current one on 40 questions about made-up memories. It shows how often each finds the right memory, how fast it is and the cut-offs that suit it, then says what it will change and asks. If you agree, it sets the model and cut-offs in `config.toml` (keeping a copy in `config.toml.bak`) and re-embeds every memory. If that fails part way, nothing changes. Athena starts again either way.

To test models without switching, run `uv run pi-assistant embeddings test -m MODEL -m ANOTHER`, with `--on-mac` for models on the model's server. Avoid `float16` builds, which can return broken vectors: Athena refuses to save those. If you change `dimensions`, start a fresh database instead.

**From the Pi's Ollama to the Mac:** a Pi set up before embeddings moved to the Mac uses `embeddinggemma` in Ollama on the Pi. After [setting up EmbeddingGemma 2 in oMLX](#embeddings), switch with the first command above. Your memories are re-embedded on the Mac, so nothing is lost. Then `sudo systemctl disable --now ollama` frees the Pi's memory.

EmbeddingGemma 2 can't run on the Pi itself yet: Ollama's builds of it need its MLX engine, which doesn't run on Linux ([ollama#18825](https://github.com/ollama/ollama/issues/18825)). On text, it finds the same memories as `embeddinggemma` in the test above, and its distances are about half as large, so its cut-offs are lower: about 0.31 for recall and 0.03 for duplicates.

## Tasks

Athena can work on things by itself, then message you what it found: a briefing every weekday at 7:30, research into something while you get on with your day, or a weekly look through the [MCP Registry](https://registry.modelcontextprotocol.io) for servers that would help with what you've been asking about. Ask it in Telegram, such as "every Sunday, look for MCP servers I'd find useful", or add one on the dashboard's **Tasks** tab, which has examples to start from.

- **You come first.** A task doesn't start while you're chatting with Athena, or within `quiet_minutes` (5) of your last message. If you are, it looks again after a minute, then two, four and so on, up to 15. Once it's running, a message from you stops what it's doing at once, so the Mac is yours, and it takes that step again once you've finished. Your replies aren't slowed down by it.
- **What it tells you.** When a task is done, you get its findings as a message in Telegram, which also goes into your chat, so you can reply "tell me more about the second one". If there's nothing new, it stays quiet. The dashboard shows what each task said last time.
- **Longer work.** A task gets more steps than a reply (`max_tool_rounds`, 25), up to `max_minutes` (30) of work, not counting time it waits for you, and thinks before answering whatever `/think` is set to (`thinking`).
- **When.** `now`, `in 2h`, `once 2026-10-16 09:00`, `daily 07:30`, `weekdays 07:30`, `weekly mon,thu 18:00`, or `every 6h`, in your time zone (`[agent] timezone`). A regular run that was missed by more than six hours, say while the Pi was off, is skipped to the next one.
- **What it may do.**
  - A task Athena suggests needs your OK: scheduling one asks first, and the message shows what it'll do, when, and the tools it may use without asking.
  - Tasks you add on the dashboard are yours already.
  - A tool that asks first still asks for a task, in Telegram, saying which task wants it, unless you've allowed that task to use it without asking. Unanswered after `confirm_minutes` (60), it's denied.
  - Scheduling another task always asks, so a task can't start others by itself.
- **If it fails**, say because the Mac's asleep, it tries again after 10 minutes, then 30, then tells you and waits for its next run.
- **In Telegram**, `/tasks` lists them, with buttons to run, pause, resume, stop or delete each. On the dashboard, the **Tasks** tab does the same.

**Suggesting MCP servers:** the built-in `find_mcp_servers` searches the official MCP Registry, and only suggests: you add a server yourself, on the dashboard, after reading about it. Anyone can list a server there, so being listed isn't an endorsement. Searches send only the words searched for, and only to the registry, so they don't ask first. `[mcp_registry] enabled = false` turns it off.

## Documents

Athena can search the documents on your Mac by meaning, and tell you which file has what you're after. You choose folders on the dashboard, under **Memory**, and press **Index now**: Athena reads them then, and never on its own.

- **Asking Athena:** in Telegram, ask something like "find my council tax letter" or "what did the boiler quote say?". It finds the file with `search_memory` and gives you its path. It can summarise the file, or answer from it, with `read_document`. That reads the text Athena keeps, so it works while your Mac is asleep.
- **Searching on the dashboard:** the **Search** box under **Memory** finds documents, remembered facts and past conversations, and shows each document's path, when it last changed and the passage that matched.
- **What's read:** plain text and Markdown, PDF, Word, RTF, OpenDocument and HTML, up to `max_chars_per_file` (300,000 characters) of each. A scanned PDF has no text to read. Pages, Numbers and Keynote files aren't read.
- **Indexing again** reads only documents that are new or have changed, and takes out those that have gone. Stopping part way keeps what's done.
- **Removing a folder** takes its documents out of Athena's memory at once. They stay on your Mac.

**Setting up the Mac:** see [Your Macs](#your-macs-calendar-reminders-and-documents). In short:
1. On the Pi, run `bash scripts/connect-mac.sh <your laptop's address> <your user> laptop`.
2. On the laptop, turn on Remote Login and run the `scripts/mac/install.sh ... --documents` command step 1 printed.
3. On the dashboard, under **Memory**, choose folders and press **Index now**. The first time, macOS asks on the laptop for access to the folders, for "python3": allow it.

**What the Pi can see** is decided on your Mac, not the Pi:
- By default, only what's in your home folder. To narrow it, use `--scope ~/Documents` with `--documents`; repeat it for more folders.
- Never hidden files or folders, such as `.ssh` or `.env`, nor anything in `~/Library`, apart from iCloud Drive.
- Folders inside apps and code (`.app` bundles, `node_modules`) are skipped.
- A file that's only in iCloud, not downloaded to the Mac, is skipped rather than downloaded. Indexing says how many there are, and in Finder, Download Now makes them indexable.
- It can't change, move or delete anything.

**What's kept on the Pi:** each document's text, in passages, with its path, in `data/assistant.db`, which never leaves your network. Embeddings are made by oMLX on the Mac. **Forget everything** on the dashboard deletes it all with your memories, and indexing again brings it back.

## Choosing a model

`eval` runs nine short tool-calling scenarios against each model and reports pass/fail and speed:

```bash
uv run pi-assistant eval -m gemma-4-26b-a4b-it-4bit -m qwen3.6-35b-a3b-4bit --repeat 3
```

It covers picking the right tool, filling in arguments, answering without tools when none is needed, and a two-step task. It only needs the model server, so you can also run it on the Mac.

On an M1 Pro with 32 GB, mixture-of-experts models with about 3–4B active parameters are the right size: dense 27B+ models generate too slowly for multi-step tool use. Larger models may need a higher GPU memory limit, for example `sudo sysctl iogpu.wired_limit_mb=26624`.

## Updating

```bash
cd ~/pi-assistant && bash scripts/update.sh
```

It pulls the latest code, updates the dependencies and restarts the service.

## Development

```bash
uv sync          # includes the test dependencies
uv run pytest    # offline: fake model server, real sqlite-vec, real MCP servers in subprocesses
```

uv uses its own Python build for this project (`[tool.uv]` in `pyproject.toml`), because some others, including the python.org installer for macOS, can't load sqlite-vec.

### Keeping secrets out of GitHub

This repo is public, so anything pushed is public at once. On the computer you develop on, run this once:

```bash
brew install gitleaks
bash scripts/install-git-hooks.sh
```

From then on, every commit and push from that clone is checked by `scripts/check-secrets.sh`, and stopped if it contains:

- **a secret**, found by [gitleaks](https://github.com/gitleaks/gitleaks) with the rules in `.gitleaks.toml`
- **a git-ignored file added anyway** with `git add -f`, such as `.env`, `config.toml` or anything in `data/`
- **a personal detail** listed in `.personal-blocklist`, such as your Telegram user ID or address. The installer creates this file. It's git-ignored, so it stays on your computer.

The push check covers commit messages too, and commits made with `--no-verify`. To check every commit already made, run `bash scripts/check-secrets.sh history`.

On GitHub, CI (`.github/workflows/ci.yml`) scans every push and pull request for secrets and runs the tests on an ARM64 Linux machine like the Pi. `main` only accepts pull requests that pass both, so the Pi only installs code that has.

The icon is `src/pi_assistant/assets/athena.svg`. The status board has its own copy, in pi-display-microservice.

| Path | Purpose |
|---|---|
| `src/pi_assistant/agent.py` | Agent loop: prompt building, tool calls, approvals |
| `src/pi_assistant/llm.py` | OpenAI-compatible client |
| `src/pi_assistant/mcp_manager.py` | MCP connections (stdio, Streamable HTTP, SSE) |
| `src/pi_assistant/sandbox.py` | The bubblewrap sandbox local MCP servers run in |
| `src/pi_assistant/tool_approvals.py` | Which version of each MCP tool you've approved |
| `src/pi_assistant/server_envs.py`, `mcp-locks/` | Locked environments for servers started with `uvx`, and the recommended servers' locks |
| `src/pi_assistant/memory.py` | Embeddings, sqlite-vec store, chunking, memory tools |
| `src/pi_assistant/history.py` | Per-chat history, with cache-friendly trimming |
| `src/pi_assistant/telegram_bot.py` | Telegram handlers and approval buttons |
| `src/pi_assistant/siri.py` | The endpoint the Siri shortcut calls |
| `src/pi_assistant/documents.py` | Indexing your documents from your Mac, when you ask |
| `src/pi_assistant/mac_documents.py` | The read-only documents server that runs on your Mac |
| `src/pi_assistant/builtin.py`, `integrations.py` | What a built-in integration is, and the list of them |
| `src/pi_assistant/news.py`, `trading212.py`, `databases.py` | The built-in integrations: news feeds, Trading 212, and SQLite databases |
| `src/pi_assistant/status.py`, `status_api.py` | What the assistant is doing, and the API the status board reads it from |
| `src/pi_assistant/dashboard.py`, `web/` | The dashboard: its API, and the page itself (plain HTML, CSS and JavaScript) |
| `src/pi_assistant/tasks.py`, `channel.py` | Tasks Athena does by itself, and how it reaches you about them |
| `src/pi_assistant/mcp_registry.py` | The built-in search of the official MCP Registry, for suggesting servers |
| `src/pi_assistant/settings.py`, `catalog.py` | What the dashboard's Tools tab can change, and how it edits `config.toml` and `.env` |
| `src/pi_assistant/stats.py` | Usage statistics, per session and in total |
| `src/pi_assistant/webserver.py` | The small HTTP server behind Siri's endpoint, the status API and the dashboard |
| `src/pi_assistant/assets/` | The Athena icon, used by the dashboard and this README |
| `src/pi_assistant/doctor.py`, `evals.py`, `search_eval.py`, `cli.py` | Command-line tools |
| `prompts/system.md` | System prompt. Point `agent.system_prompt_file` at a `*.local.md` copy to customise it privately. |
| `scripts/install.sh`, `scripts/update.sh`, `deploy/` | Pi setup and updates |
| `scripts/connect-mac.sh`, `scripts/mac/install.sh` | Linking the Pi to your Mac's files, Calendar and Reminders |

## Troubleshooting

- **Start with `uv run pi-assistant doctor`.** Then check `journalctl -u pi-assistant -f` and `data/logs/mcp-*.log`.
- **"Can't reach the model server":** oMLX isn't running, is only listening on localhost, or the hostname doesn't resolve from the Pi. Try `curl http://my-mac.local:8000/v1/models` from the Pi.
- **"The embeddings server doesn't have …":** oMLX is older than 0.7.1, or hasn't downloaded `mlx-community/embeddinggemma-2-bf16`, or calls it something else: the error lists what it has.
- **Memories show up in replies about something else, or don't when they should:** the recall cut-off doesn't suit the embeddings model. Run `uv run pi-assistant embeddings test` and set `recall_max_distance` to the cut-off it suggests.
- **"The memory index was built with … but the config now uses …":** `config.toml` names a different embeddings model from the one your memories were stored with. Run the switch script it names, or put the old model back.
- **The model answers instead of using tools:** check that tool calling works in `doctor`, then compare models with `eval`.
- **The bot ignores you:** your ID isn't in `telegram.allowed_user_ids`. Empty the list temporarily to get your ID.
- **Telegram "Conflict: terminated by other getUpdates request":** two copies of the bot are running with the same token.
- **The dashboard doesn't load:** run `doctor` and see what it says under "Dashboard". If it's answering there, check `sudo tailscale serve status` shows `localhost:8092`, and that Tailscale is connected on your phone or Mac.
- **The dashboard says "Can't reach Athena":** the service has stopped or is restarting. `journalctl -u pi-assistant -f` says why.
- **"Couldn't start it in a sandbox":** install bubblewrap (`sudo apt install bubblewrap`), then run `doctor`, which says whether it can make a sandbox. A server that needs something from your home folder can be given it with `read_only_paths`.
- **A server says "waiting for you":** some of its tools are new or have changed since you approved them. Open its card on the dashboard to see what changed and approve them.
- **A server's switch won't stay on:** the dashboard says what it needs first, such as a key. If it's on but shows an error, open it: the error and the server's log (`data/logs/mcp-<name>.log`) usually say why.
- **The status board says "Offline" while Athena runs:** run `doctor` and see what it says under "Status API". Problems with the board itself are covered in pi-display-microservice's README.
- **`zsh: no such file or directory: …/Library/Application`:** the Mac was set up by a version of `scripts/mac/install.sh` with a quoting bug. On the Mac, pull the repo and run `bash scripts/mac/install.sh` again: it keeps your folders and the Pi's key.
- **A server on a Mac fails:** run `ssh athena-mac hello` on the Pi (or `ssh athena-laptop hello`). If that doesn't say there's no server called 'hello', the problem is SSH: check Remote Login is on and the Mac is awake. If it does, check `~/Library/Logs/Athena/` on the Mac, and that iMCP is running.
- **"Couldn't reach your Mac" when indexing:** the laptop is asleep, off your tailnet, or not set up yet (see [Your Macs](#your-macs-calendar-reminders-and-documents)). What SSH said is at the end of the message, and in `data/logs/documents.log` on the Pi.
- **"macOS hasn't allowed access" when indexing:** allow "python3" when the laptop asks, or in System Settings > Privacy & Security > Files and Folders, then index again.
- **"Trading 212 didn't accept the API key":** check the key and secret in `.env`, and that the key is for the account `environment` says (live or practice). If the key is restricted to trusted IPs, check your home's address hasn't changed: compare `curl -s https://api.ipify.org` on the Pi with the key's settings in Trading 212.
- **The Siri shortcut fails:** check `doctor` says Siri is "listening", and that Tailscale is connected on the device you're asking from. Try the `curl` command from the Siri section. A 401 means the token in the shortcut doesn't match `SIRI_TOKEN`.

## Security notes

- Only allowlisted Telegram users can use the bot. Messages from anyone else are ignored and logged.
- Secrets stay in `.env` (mode 600). `config.toml`, `.env` and `data/` are git-ignored.
- `install.sh` turns off `git push` in the Pi's clone (`scripts/disable-git-push.sh`), so nothing on the Pi can be pushed to GitHub. `git pull` still works.
- Text from tools, such as fetched web pages, is untrusted: it can tell the model to send your data somewhere. That's why MCP tools ask first by default and the approval message shows their full arguments. Only set `confirm = []` on servers that can't send data off your network.
- Servers started with `uvx` run from locked environments: every package's version and hash is fixed, nothing is built from source, and new installs are checked for known malware ([How servers are installed](#how-servers-are-installed)).
- Local servers run in a [sandbox](#sandbox) without your home folder, so they can't read `.env`, your memories or your SSH keys, and without the network if they don't need it. A server with network access can reach anything the Pi can, though, so the approval before a tool runs is still what stops your data being sent somewhere.
- A server's tools that are new or have changed since you approved them are held back from the model until you approve them on the dashboard ([When a server's tools change](#when-a-servers-tools-change)).
- Tasks run without you watching, so they can't do more than you could approve: one Athena suggests needs your OK, any tool that asks first still asks you in Telegram unless you've allowed that task to use it, and a task can't schedule others without asking. Allowing a task to fetch or search without asking lets it send what's in its instructions and what it's read to those sites unseen, so allow only what it needs.
- Telegram bot chats aren't end-to-end encrypted, so messages pass through Telegram's servers even though the model is local.
- The Pi reaches your Macs over SSH with a key of its own, which `~/.ssh/authorized_keys` on each Mac restricts to starting Athena's servers there: no shell, no port forwarding. The documents server is read-only, only sees what's in your home folder (or the scope you gave), never hidden files or `~/Library` apart from iCloud Drive, and is only started when you index or choose folders.
- The email server only sends to addresses in `MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS`, and can't delete or move mail, even if you approve. The GitHub token can only read public repositories.
- Reading your Trading 212 account doesn't ask, but placing or cancelling an order always does, and that can't be switched off. The key's own permissions have the last word: without permission to execute orders it can't trade at all, and restricted to your IP address it's useless anywhere else.
- SQLite databases are opened read-only, and SQLite itself refuses anything but reading, so a query can't change them or write files.
- The Siri endpoint is off unless you turn it on. It listens only on the Pi itself, Tailscale Serve passes on requests from your tailnet, and each request needs the token. Anyone with both can ask Athena anything you could, so keep the token private.
- The dashboard only listens on the Pi itself, and Tailscale Serve passes on requests from your tailnet. Everything but the sign-in page needs its password. Changes are only accepted from the dashboard's own page, so another website can't make them with your cookie. Keys you enter go into `.env` and are never sent back to the browser. The dashboard can't change Athena's own secrets (the Telegram token, the model's API key, the Siri token and its own password) or pass them to a server. Anyone with the password can add an MCP server, which runs a command on the Pi, so keep it as private as an SSH password.
- The status API includes the start of your latest message, for the status board. It only listens on the Pi itself unless you set `host`, and then it needs a token. If other people can see the screen, set `show_task = false` under `[display]`.

## License

MIT.
