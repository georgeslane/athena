# Development

```bash
uv sync                  # with the test dependencies
uv run pytest            # offline: a fake model server, real sqlite-vec, real MCP servers in subprocesses
uv run ruff check .      # lint; CI fails on anything either finds
uv run ruff format .
```

uv uses its own Python build for this project (`[tool.uv]` in `pyproject.toml`), because some others, including the python.org installer for macOS, can't load sqlite-vec.

To run Athena from a checkout, use `uv run athena`, with its files somewhere of their own: `ATHENA_HOME=~/athena-dev uv run athena setup`.

Some tests need more than the rest, and are skipped where it isn't there. CI, on an ARM64 Linux machine like the Pi, has all of it:

- **Linux and bubblewrap:** the sandboxes, for running code and MCP servers.
- **systemd and sudo:** the service's hardening, and the alert when Athena stops.
- **A browser:** the dashboard, in headless Chromium through Playwright (`tests/test_dashboard_browser.py`). Install one with `uv run playwright install --only-shell chromium`, or point `ATHENA_TEST_BROWSER` at a Chromium you have.

Tasks are tested over days in a moment (`tests/test_tasks_over_time.py`): the scheduler reads the time from a `Clock` (`src/athena/clock.py`), and the tests' clock jumps ahead whenever the scheduler waits.

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
| `src/athena/chat.py`, `commands.py`, `approvals.py`, `channel.py` | Chatting on the dashboard, the commands every front end has, requests for your OK wherever they're answered, and how Athena reaches you |
| `src/athena/tools.py`, `tool_search.py`, `privacy.py` | Tools, those found when needed, and keeping personal details out of what they send |
| `src/athena/mcp_manager.py`, `server_envs.py`, `server_security.py`, `tool_approvals.py`, `sandbox.py` | MCP servers: connecting, their locked environments and security updates, the tools you've approved, and the sandbox |
| `src/athena/builtin.py`, `integrations.py`, `catalog.py`, `recommended.py`, `settings.py` | Built-in tools, the recommended servers, and what the Tools tab can change |
| `src/athena/news.py`, `markets.py`, `trading212.py`, `databases.py`, `run_code.py`, `files.py`, `workspace.py`, `pdfs.py`, `mcp_registry.py` | The built-in tools |
| `src/athena/memory/` | Memory: the store, embeddings, searching by words, text and the service with its tools |
| `src/athena/documents.py`, `mac_documents.py` | Indexing your documents, and the read-only server on your Mac |
| `src/athena/tasks/`, `clock.py` | Tasks: schedules, the store, the scheduler and the model's tools; the time it goes by |
| `src/athena/cron.py`, `watching.py`, `triggers.py` | Cron schedules, what's watched, and events that start tasks |
| `src/athena/skills.py`, `claude_projects.py`, `project_files.py`, `github_repo.py` | Skills, and projects handed to Claude |
| `src/athena/telegram_bot.py`, `channel.py`, `photos.py`, `formatting.py`, `siri.py` | Telegram, and Siri's endpoint |
| `src/athena/dashboard.py`, `web/` | The dashboard: its API, and the page itself, plain HTML, CSS and JavaScript modules with no build step. `web/md.js` turns Markdown into the page's elements, never HTML |
| `src/athena/status.py`, `status_api.py`, `stats.py`, `alerts.py`, `timeline.py` | What Athena is doing, the status board's API, usage statistics, alerts, and what it did for the Activity tab |
| `src/athena/doctor.py`, `evals.py`, `search_eval.py` | `athena doctor`, `athena eval` and the embeddings test |
| `src/athena/resources/` | What comes with Athena: the example config and `.env`, the system prompt, the recommended servers' locks, the systemd units, the stopped alert and the Mac's scripts |
| `docs/claude-projects/CLAUDE.md` | The guide for Claude's side of [Projects for Claude](claude-projects.md) |
| `scripts/` | For developing: the secret checks and the git hooks that run them |

A built-in tool is one class in a module of its own: see [Adding a built-in tool](tools.md#adding-a-built-in-tool).
