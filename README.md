<img src="src/athena/assets/athena.svg" alt="Athena's shield: a bronze hoplite shield bearing an owl on an olive branch" width="128" align="right">

# Athena

A personal AI assistant that runs on your own hardware. A small Linux computer, such as a Raspberry Pi, runs Athena: you talk to it in Telegram, and it uses a model on your own machine, such as a Mac running [oMLX](https://omlx.ai), with tools, long-term memory and tasks it does by itself.

```
 Telegram     Raspberry Pi (always on)                      Mac
 ────────     ┌──────────────────────────────────────┐      ┌───────────────────────────────┐
  you ◄─────► │ Athena: agent, memory, tasks,        │─────►│ oMLX: the model and           │
              │ tools and MCP servers, dashboard     │ HTTP │ embeddings, OpenAI-style API  │
              │                                      │─────►│ Calendar, Reminders and your  │
              └──────────────────────────────────────┘ SSH  │ documents, read-only          │
                                                            └───────────────────────────────┘
```

## What it does

- **Chat in Telegram**, with photos and files, or **ask by voice** through Siri.
- **Tools**: any MCP server, on the Pi, your Mac or the web, plus built-in news, markets, Trading 212, SQLite databases, files and PDFs, and Python it runs in a sandbox. Anything that can change something or send your data elsewhere asks you first.
- **Memory**: facts it saves, your past conversations and your Mac's documents, searched by meaning and by words.
- **Agents**: each is something it keeps an eye on or does for you, such as finding you jobs, with a chat, guidance, and a DAG of tasks, in Airflow-style Python, that it runs by itself, on a schedule or when something happens. Every run ends by reviewing how it went and proposing improvements for you to approve. It keeps an eye on things every hour too, and your messages come first.
- **Skills**: how you like things done, which it reads before doing them, and improves with your approval.
- **Projects for Claude**: coding projects it hands to Claude as pull requests, which start DAGs when they're done.
- **A dashboard** for its agents: their chats, with photos and files, and notifications on your iPhone and Mac, their DAGs, drawn and edited, and the history of their runs; and for what it's watching, its memory, skills, tools and status, over Tailscale.
- **Private by design**: secrets stay in one file, MCP servers and code run in sandboxes, and the service can't change its own code. See [Privacy and security](docs/privacy-and-security.md).

## Install

You need a Raspberry Pi 4 or 5 (or any Debian-based Linux) and a model server the Pi can reach, such as [oMLX](https://omlx.ai) on a Mac, with a model and [EmbeddingGemma 2](docs/memory.md#embeddings). You also need a Telegram bot, from [@BotFather](https://t.me/BotFather) (`/newbot`).

On the Pi, over SSH:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # uv, which installs Athena
uv tool install git+https://github.com/georgeslane/athena
athena setup
```

`athena setup` makes Athena's home folder, `~/.athena`, and walks you through the model, memory, Telegram and the dashboard. It finds your Telegram ID when you message the bot. Then it installs and starts the service. Run it again any time to change those settings. If Athena ran from a git checkout before, setup moves its settings, secrets and data into `~/.athena`.

Then check everything with `athena doctor`, and open the [dashboard](docs/dashboard.md).

## Commands

| Command | What it does |
|---|---|
| `athena setup` | Set Athena up, or change its main settings |
| `athena doctor` | Check the model, memory, tools, Telegram and the dashboard |
| `athena update` | Update Athena and restart it |
| `athena logs` | Follow what it's doing |
| `athena chat` | Chat in the terminal |
| `athena service install` / `uninstall` | The systemd service that runs it |
| `athena mac connect` / `install` | Let it use your Mac's calendars and documents ([Your Mac](docs/mac.md)) |
| `athena embeddings test` / `switch` | Test or change the embeddings model ([Memory](docs/memory.md)) |
| `athena eval` | Compare models on tool calling |

In Telegram, `/new` starts a new agent, `/dags` shows what each does by itself, and `/status` says how Athena is: see [Using Athena](docs/using.md).

## Documentation

- [Installing and updating](docs/install.md)
- [Using Athena](docs/using.md): Telegram, photos, files, agents and Siri
- [The dashboard](docs/dashboard.md), the status board and alerts
- [Tools](docs/tools.md): built-in tools, MCP servers and adding your own
- [Memory](docs/memory.md): memories, documents, embeddings and skills
- [Agents](docs/agents.md): DAGs, their runs, triggers and watching
- [Your Mac](docs/mac.md): Calendar, Reminders and documents
- [Projects for Claude](docs/claude-projects.md)
- [Privacy and security](docs/privacy-and-security.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Development](docs/development.md)

## License

MIT.
