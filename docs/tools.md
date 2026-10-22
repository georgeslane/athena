# Tools

Athena's tools come from two places: tools built into Athena, and [MCP servers](#mcp-servers), on the Pi, on your Mac over SSH, or on the web. The dashboard's Tools tab switches them on and off, sets them up and gives them keys, editing `config.toml` for you.

**What asks first** follows one rule: a tool asks you, in Telegram, if it can change something, or can send what's in the conversation somewhere someone else could read it. The approval message shows its full arguments.

- **Never asks:** reading your own files, calendar and databases keeps everything on your network. Reading your Trading 212 account only asks Trading 212 for what it already holds. Looking things up in public sources (your news feeds, Yahoo Finance, the SEC, GitHub's public repos) sends only the lookup itself, to that service.
- **Asks:** fetching a URL, searching the web and sending email can reach anyone, and adding a calendar event can email an alarm to any address.

## Built in

| Tool | What it does | Asks first |
|---|---|---|
| [News](#news) | `read_news`: the feeds you list | Never |
| [Markets](#markets) | `market_*`: prices and history from Yahoo Finance | Never |
| [Trading 212](#trading-212) | `trading212_*`: your account, and orders you approve | Placing and cancelling orders |
| [Databases](#databases) | `database_*`: read-only SQLite | Never |
| [Running code](#running-code) | `run_python`, in a sandbox | Never |
| [Files](#files) | `read_file`, `write_file`, `send_file`, PDFs included | Never |
| Finding MCP servers | `find_mcp_servers`: searches the [MCP Registry](https://registry.modelcontextprotocol.io) to suggest servers | Never |
| [Projects for Claude](claude-projects.md) | Hands coding projects to Claude | Always |

### News

`read_news` reads the RSS or Atom feeds listed under `[news.feeds]`, and only those. All the model chooses is which feed to read and how many items, so it can't send anything anywhere. Add any news site's feed, with any name you like. To read a whole article, the model uses `fetch`, which asks first.

### Markets

Prices and history for stocks, funds, indices, currencies and commodities, from Yahoo Finance, through [yfinance](https://github.com/ranaroussi/yfinance):

- `market_quote`: the latest price of up to 10 symbols, with the change on the day, the day's and year's range, and for stocks the market cap, P/E and dividend yield
- `market_history`: how one symbol has moved over a period, with up to 40 closing prices
- `market_search`: symbols by name, such as "Rolls-Royce" (RR.L) or "gold" (GC=F)

Symbols are Yahoo's: `.L` for London, `^` for indices (`^FTSE`), `=F` for futures (`GC=F` gold), `=X` for currencies (`GBPUSD=X`). Only the symbols, and searches of up to 60 characters, leave the Pi. Prices can be 15 minutes or more behind. yfinance isn't an official Yahoo API, so it can stop working until it's updated.

### Trading 212

`trading212_portfolio`, `trading212_history` and `trading212_find_instrument` read your Trading 212 account: its value and cash, holdings with their profit or loss, pending orders, and past orders, dividends, deposits and withdrawals. They don't ask first: they only fetch your own account from Trading 212, and the instrument search runs on a list the Pi keeps.

`trading212_place_order` and `trading212_cancel_order` always ask first, and can't be set not to. The approval message says in words which instrument and where it trades, the price terms, the account, how many you hold and roughly what the order comes to, so you'd notice the wrong listing. An order Trading 212 would refuse is stopped before you're asked. An approved order is sent once and never retried.

1. In the Trading 212 app, under **Settings > API (Beta)**, generate a key for an Invest or Stocks ISA account.
2. Allow account data, portfolio, history, metadata and reading orders, and, to trade, executing orders. Leave pies off.
3. Restrict it to your home's public address, which `curl -s https://api.ipify.org` on the Pi prints, so the key is useless anywhere else.
4. On the Tools tab, open **Trading 212**, paste the key and its secret, and switch it on. Then run `athena doctor`, which checks the key's permissions.

Orders are for a number of shares, which can be a fraction. Limit and stop prices are in the instrument's own currency: pence (GBX) for most London shares. To practise, make a key in practice mode and set `environment = "demo"`.

### Databases

`database_tables` and `database_query` read the SQLite databases on the Pi listed under `[sqlite.databases]`, by the names you give them. A path can be absolute, or relative to `~/.athena`.

They can only read, and SQLite itself enforces that: each file is opened read-only, and an authorizer refuses everything but reading. Statements that don't start with SELECT, WITH or VALUES aren't tried, a query stops after 10 seconds, and at most 100 rows come back.

### Running code

`run_python` runs Python that Athena writes, for what models are bad at by themselves: sums, statistics, dates, and reading or making files such as CSVs. numpy and pandas are there.

The code runs in a sandbox of its own, stricter than the MCP servers', so it doesn't ask first:

- **What it sees:** only what Python needs, read-only. Your home folder, Athena's settings, secrets, memory and logs, and the rest of the Pi, aren't there at all.
- **Where it writes:** its own folder, `~/.athena/data/workspace`, which keeps files between runs, and a small `/tmp` of its own.
- **No network,** almost no environment variables, and only its own processes.
- **Limits** it can't raise: memory (`memory_mb`, 1 GB), CPU time, file size (`max_file_mb`, 50 MB) and open files. It's stopped after `timeout_seconds` (30), and the model sees at most `max_output_chars` (10,000) of what it printed. Once its folder holds more than `workspace_mb` (500 MB), code can only delete until there's room.

`doctor` checks code can run, can't connect out, and can't see Athena's files. Switch it off under **Running code** on the Tools tab.

### Files

Athena has a folder of its own, `~/.athena/data/workspace`, shared with the code it runs:

- `read_file` reads a file's text, 5,000 characters at a time, and a PDF's page by page. Given a folder, it lists what's there.
- `write_file` writes Markdown, text, CSV and the like. A `.pdf` is laid out from Markdown, with headings, lists, tables and code, on A4.
- `send_file` sends one to you in Telegram, after the answer. A task can send files with what it found.

**Sending Athena a file:** send a PDF, or a text file such as Markdown, CSV or JSON, up to 20 MB, with your question as its caption. It's kept in the folder's `inbox/`.

**Safety:** code can put anything in the folder, including a link to Athena's `.env`. So Athena opens what's there one folder at a time without following links, and only ordinary files. PDFs are read and made in the code's sandbox, so a PDF made to trouble a PDF reader can do no harm. Forgetting everything empties the folder.

PDFs use the DejaVu fonts if the Pi has them (`sudo apt install fonts-dejavu-core`), for most of the world's letters. `doctor` makes and reads a PDF to check.

## MCP servers

### Recommended servers

The Tools tab lists these, ready to switch on:

| For | Server | Runs on | Asks first |
|---|---|---|---|
| The time | `mcp-server-time` | Pi | Never |
| Web pages | `mcp-server-fetch` | Pi | Every fetch |
| Web search | [DuckDuckGo](https://github.com/nickclyde/duckduckgo-mcp-server) | Pi | Every search |
| [Calendar and Reminders](mac.md) | [iMCP](https://github.com/mattt/iMCP) | Mac, over SSH | Adding events and reminders |
| [Company filings](#company-filings) | [EdgarTools](https://github.com/dgunning/edgartools) | Pi | Never |
| [GitHub](#github) | [GitHub's own server](https://github.com/github/github-mcp-server), read-only | GitHub | Never |
| [Email](#email) | [mcp-email-server](https://github.com/Wh1isper/mcp-email-server) | Pi | Sending |

Versions are pinned, so a new release can't change what runs on your Pi until you choose to update, and so are the packages each server depends on: see [How servers are installed](#how-servers-are-installed).

#### Company filings

EdgarTools searches and reads filings to the US SEC: annual and quarterly reports, financial statements, insider trades and fund holdings. The SEC asks callers to say who they are, so give your name and email, like `Jane Doe jane@example.com`, on the Tools tab. It needs about 300 MB of packages, so its first start takes a few minutes on a Pi.

#### GitHub

GitHub's hosted server, read-only, with lockdown on, which hides text in issues and pull requests from people without push access: where instructions aimed at an assistant would most likely be. Make a [fine-grained token](https://github.com/settings/personal-access-tokens/new) with **Public repositories (read-only)** access and an expiry date, and paste it into **GitHub** on the Tools tab.

#### Email

Give Athena an email address of its own, with a provider that offers IMAP, SMTP and app passwords, such as Fastmail, iCloud Mail or Gmail. Then it can't read your own mail: forward it anything you'd like it to read. On the Tools tab, open **Email** and fill in the address, your provider's servers and the app password.

The email server itself enforces two settings, whatever the model asks for and whatever you approve:

- `MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS`: who it may email, comma-separated, with `*` as a wildcard. Start with your own address.
- `MCP_EMAIL_SERVER_ALLOWED_MUTATIONS = "send"`: it can send, but never delete, move, flag or draft.

Sending always asks first: check the recipients and attachments. To have it see only mail from you, set `MCP_EMAIL_SERVER_ALLOWED_SENDERS` to your own addresses.

### Settings

Servers are listed under `[mcp_servers.<name>]` in `config.toml`. Each has `command` (a local server, spoken to over stdio) or `url` (a remote server over Streamable HTTP; URLs ending in `/sse` use the older SSE transport). Optional settings:

- `include`, `exclude` and `confirm` take glob patterns matched against the server's tool names. `confirm` lists the tools that ask first: by default, every tool. If you narrow it, use `include` too, so tools you haven't checked can't run without asking.
- `arguments` are arguments Athena gives the server's tools itself, such as `{ account_name = "athena" }` for the email server. The model isn't shown them, so it can't get them wrong or choose others.
- `about` tells the model what the server is when its tools don't say, such as that the email inbox is Athena's own, not yours.
- `headers` adds HTTP headers, such as an auth token, and `env` passes environment variables to a local server, which otherwise gets almost none. Secrets go in `.env`, referred to as `${NAME}`.
- `enabled = false` keeps a server's settings without starting it.

After changing servers by hand, send `/reload`, or press **Reconnect all** on the Tools tab, and run `athena doctor`. Each local server's errors go to `~/.athena/data/logs/mcp-<name>.log`.

### Adding a server

Most servers are in the [MCP Registry](https://registry.modelcontextprotocol.io), and Athena can search it for you.

1. **Choose carefully.** A server sees what Athena passes it and can act for you. Prefer one from the service's own makers, or one that's widely used and recently updated, and read what each tool does.
2. **Give it as little as possible:** its read-only mode, a token that can only do what you need, and any limits the server enforces itself.
3. **Add it** on the Tools tab (**Add a server**), or in `config.toml`:

   ```toml
   [mcp_servers.weather]
   command = "uvx"                                   # a Python server from PyPI
   args = ["some-weather-mcp==1.2.3"]                # pinned to a version you've checked
   env = { WEATHER_API_KEY = "${WEATHER_API_KEY}" }  # secrets go in .env
   include = ["get_forecast"]                        # only the tools you need
   ```

   - **npm servers** need Node.js 20 or later on the Pi: `command = "npx"`, `args = ["-y", "some-server@1.2.3"]`.
   - **Hosted servers** take `url` and `headers` instead.
   - **Servers that need your Mac's apps or files** run on the Mac: see [Your Mac](mac.md#adding-a-server-on-the-mac).
   - **`confirm`:** leave it out, so every tool asks first, unless the tools only read your own data or public sources.
4. **Check it:** open it on the Tools tab to see its tools and choose which Athena uses and which ask first. `athena doctor` shows how much they add to every message, and `athena eval` checks the model still picks the right tools.

To update a server, read what changed, change its pinned version, and restart: Athena locks the new version's dependencies when it starts it.

### How servers are installed

Pinning `mcp-server-fetch==2026.8.18` pins the server, but not the dozens of packages it depends on, which `uvx` would install at whatever version is newest. So Athena doesn't run servers through `uvx`. Each server whose `command` is `uvx` gets a small project of its own, in `~/.athena/data/mcp/<name>/`, whose `uv.lock` records the exact version and hash of every package.

- **Reviewed locks:** the recommended servers' locks come with Athena, so any change to them shows up in a pull request. Any other server is locked the first time it starts, until you change its version.
- **Installing:** every hash is checked, only ready-built wheels are installed, so no package's own code runs while it's installed, and [OSV](https://osv.dev) is asked whether any package is known malware first. If one is, or OSV can't be reached, the server isn't installed.
- **What isn't locked:** servers started with `uvx` options other than `--from`, and servers started another way, such as `npx`, `ssh` or a URL. `doctor` says which.

### Security updates

Once a day (`[security_updates] every_hours`, 24), Athena asks OSV whether any package locked for a server has a known vulnerability. For each with a fixed version, it updates just that package to the lowest version that fixes it, restarts the server once it's free, and tells you what it did. Each server's card shows when it was last checked, anything found, and a **Fix** button. With `automatic = false`, it only tells you.

### When a server's tools change

A server can change its tools' descriptions whenever it's updated, and a description can carry instructions to the model. So Athena remembers each tool as it was when you approved it, and holds back a tool that's new or has changed until you approve it. You're told in Telegram, and the server's card on the Tools tab shows what changed. Tools that haven't changed keep working.

### Tools found when needed

Every tool's description goes into every message. Tools you rarely use can be left out, and found when they're needed instead. Choose them on the Tools tab under **Prompt size**, which lists every tool by how many tokens it adds and how often it's used, or in `config.toml`, where patterns work too:

```toml
[tools]
on_demand = ["trading212_*", "database_query", "find_mcp_servers"]
```

In their place the model has `find_tools`, which names them and describes those it asks for, and `use_tool`, which runs one. The tools sent with every message stay the same, so the model server's cache still holds. A tool run this way is treated exactly as if it were called by name: it asks first if it would anyway, and a task may use it without asking only if you've allowed that tool. Looking tools up takes about 200 tokens, plus 30 for each, so it's worth it for the bigger ones.

### Sandbox

Local servers run in a [bubblewrap](https://github.com/containers/bubblewrap) sandbox. Inside it, a server sees the system read-only, an empty home folder of its own, its own `/tmp`, and only what it needs to run: its locked environment, uv's Pythons, and `~/.ssh` if its command is `ssh`. Your `.env`, memories and Athena's code aren't there. Nothing it starts outlives it.

- **`network = false`** cuts it off from the network too. A server with network access can reach anything the Pi can.
- **`read_only_paths = ["/home/pi/notes"]`** lets it read files or folders it needs.
- **`sandbox = false`** runs it without one. It can then read everything you can, and `doctor` warns about it.

`doctor` checks the sandbox for real. Without bubblewrap, local servers aren't started, and the dashboard says why.

## Adding a built-in tool

If no safe server exists for something, it can be built in, like the news reader in `src/athena/news.py`. Each is one class in a module of its own, which says its section of `config.toml`, how the dashboard shows it, how it starts, its tools and `doctor`'s check of it (`src/athena/builtin.py`). Adding it to `BUILTINS` in `src/athena/integrations.py` is then all it takes. A tool that asks first can have a `preview`, which says in words what the call will do, for the approval message.
