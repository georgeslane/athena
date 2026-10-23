# Development

```bash
uv sync                  # with the test dependencies
uv run pytest            # offline: a fake model server, real sqlite-vec, real MCP servers in subprocesses
uv run ruff check .      # lint; CI fails on anything either finds
uv run ruff format .
```

uv uses its own Python build for this project (`[tool.uv]` in `pyproject.toml`), because some others, including the python.org installer for macOS, can't load sqlite-vec.

To run Athena from a checkout, use `uv run athena`, with its files somewhere of their own: `ATHENA_HOME=~/athena-dev uv run athena setup`.

Agents' DAGs run on [agentic-airflow](https://github.com/georgeslane/agentic-airflow), which isn't on PyPI: `pyproject.toml` installs it from its repository at a pinned commit, so nothing else of that name can be installed instead. To move to a newer one, change the commit there and run `uv lock`. To work on both at once, `uv pip install -e ../agentic-airflow` puts your checkout in the environment, until the next `uv sync`.

Some tests need more than the rest, and are skipped where it isn't there. CI, on an ARM64 Linux machine like the Pi, has all of it:

- **Linux and bubblewrap:** the sandboxes, for running code and MCP servers.
- **systemd and sudo:** the service's hardening, and the alert when Athena stops.
- **A browser:** the dashboard, in headless Chromium through Playwright (`tests/test_dashboard_browser.py`). Install one with `uv run playwright install --only-shell chromium`, or point `ATHENA_TEST_BROWSER` at a Chromium you have.

## Keeping secrets out of GitHub

This repo is public, so anything pushed is public at once. On the computer you develop on, run this once:

```bash
brew install gitleaks
bash scripts/install-git-hooks.sh
```

From then on, every commit and push is checked by `scripts/check-secrets.sh`, and stopped if it contains:

- **a secret**, found by [gitleaks](https://github.com/gitleaks/gitleaks) with the rules in `.gitleaks.toml`
- **a git-ignored file added anyway**, such as `.env`, `config.toml` or anything in `data/`
- **a personal detail** listed in `.personal-blocklist`, such as your Telegram user ID. The installer makes this file, which is git-ignored.

The push check covers commit messages too, and commits made with `--no-verify`. To check every commit already made: `bash scripts/check-secrets.sh history`. CI scans every push and pull request for secrets, checks the dependencies of Athena and the recommended MCP servers for known vulnerabilities, and runs the tests. `main` only accepts pull requests that pass.

## The recommended servers' locks

After changing a recommended server's version in `src/athena/resources/config.example.toml`, remake its lock with:

```bash
uv run python -m athena.server_envs
```

`src/athena/recommended.py` describes the same servers for the dashboard, and the tests check the two agree.

## Where things are

| Path | What it is |
|---|---|
| `src/athena/cli.py` | The `athena` command |
| `src/athena/setup_wizard.py`, `service.py` | `athena setup`; the systemd service, updating, and moving from a checkout |
| `src/athena/config.py`, `config_files.py` | Settings, read from `~/.athena`; editing `config.toml` and `.env`, keeping their comments |
| `src/athena/app.py` | Builds and runs Athena's services |
| `src/athena/agent.py`, `llm.py`, `compaction.py`, `history.py` | The agent loop, the model client, compacting, and sessions and their conversations |
| `src/athena/chat.py`, `commands.py`, `approvals.py`, `channel.py`, `push.py` | Chatting on the dashboard, the commands every front end has, requests for your OK wherever they're answered, how Athena reaches you, and notifications |
| `src/athena/tools.py`, `tool_search.py`, `privacy.py` | Tools, those found when needed, and keeping personal details out of what they send |
| `src/athena/mcp_manager.py`, `server_envs.py`, `server_security.py`, `tool_approvals.py`, `sandbox.py` | MCP servers: connecting, their locked environments and security updates, the tools you've approved, and the sandbox |
| `src/athena/builtin.py`, `integrations.py`, `catalog.py`, `recommended.py`, `settings.py` | Built-in tools, the recommended servers, and what the Tools tab can change |
| `src/athena/news.py`, `markets.py`, `trading212.py`, `databases.py`, `run_code.py`, `files.py`, `workspace.py`, `pdfs.py`, `mcp_registry.py` | The built-in tools |
| `src/athena/memory/` | Memory: the store, embeddings, searching by words, text and the service with its tools |
| `src/athena/documents.py`, `mac_documents.py` | Indexing your documents, and the read-only server on your Mac |
| `src/athena/dags/` | Agents and their DAGs, run by [agentic-airflow](https://github.com/georgeslane/agentic-airflow)'s engine: reading a DAG file into the engine's objects without running it (`reader.py`), the service that registers and runs them, asks you about tools and tells you what they found (`service.py`), what agents' conversations and runs start with (`intros.py`), the engine's model and tools, which are Athena's (`backend.py`), the tasks every DAG ends with (`ends.py`), what's proposed for a DAG, in the engine's queue (`proposals.py`), how runs are shown (`runs.py`), what's kept (`store.py`), the model's tools for them (`tools.py`), agents' files (`files.py`), the dashboard's API (`api.py`), and upgrades to what's kept (`migrate.py`) |
| `src/athena/gate.py` | Your messages first, between the tasks Athena does by itself |
| `src/athena/cron.py`, `triggers.py` | Cron schedules, and events from outside that start DAGs |
| `src/athena/skills.py`, `skill_reviews.py`, `claude_projects.py`, `project_files.py`, `github_repo.py` | Skills, looking back at how they went after a chat, and projects handed to Claude |
| `src/athena/telegram_bot.py`, `channel.py`, `photos.py`, `formatting.py`, `siri.py` | Telegram, and Siri's endpoint |
| `src/athena/dashboard.py`, `web/` | The dashboard: its API, and the page itself, plain HTML, CSS and JavaScript modules with no build step. `web/md.js` turns Markdown into the page's elements, never HTML, and `web/sw.js` shows notifications |
| `src/athena/status.py`, `status_api.py`, `stats.py`, `alerts.py`, `steps.py` | What Athena is doing, the status board's API, usage statistics, alerts, and what a run did, tool by tool |
| `src/athena/doctor.py`, `evals.py`, `search_eval.py` | `athena doctor`, `athena eval` and the embeddings test |
| `src/athena/resources/` | What comes with Athena: the example config and `.env`, the system prompt, the recommended servers' locks, the systemd units, the stopped alert and the Mac's scripts |
| `docs/claude-projects/CLAUDE.md` | The guide for Claude's side of [Projects for Claude](claude-projects.md) |
| `scripts/` | For developing: the secret checks and the git hooks that run them |

A built-in tool is one class in a module of its own: see [Adding a built-in tool](tools.md#adding-a-built-in-tool).
