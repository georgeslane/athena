# Installing and updating

## What you need

- **A Linux computer that's always on**, such as a Raspberry Pi 4 or 5 with 64-bit Raspberry Pi OS. Athena runs as a systemd service, and runs MCP servers and code in [bubblewrap](https://github.com/containers/bubblewrap) sandboxes, so it needs Linux.
- **A model server** the Pi can reach, with an OpenAI-compatible API, such as [oMLX](https://omlx.ai) on a Mac. It needs to accept connections from the Pi, not just from `localhost`, and should have an API key set. It serves the [embeddings model](memory.md#embeddings) too. See [Choosing a model](using.md#choosing-a-model).
- **A Telegram bot**, from [@BotFather](https://t.me/BotFather) (`/newbot`).
- **[Tailscale](https://tailscale.com)** on the Pi and your devices, for the dashboard and Siri. Optional, but recommended.

## Installing

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # uv, which installs Athena
uv tool install git+https://github.com/georgeslane/athena
athena setup
```

`uv tool install` puts Athena in an environment of its own, in `~/.local/share/uv/tools/athena`, and the `athena` command in `~/.local/bin`. No git checkout is needed on the Pi.

`athena setup` asks for:

1. **You and Athena:** your first name, what to call the assistant, and your time zone.
2. **The model:** the server's address, such as `http://my-mac.tail1234.ts.net:8000/v1`, its API key, and which of its models to use, from the list it gives.
3. **Memory:** the embeddings model, usually on the same server.
4. **Telegram:** the bot's token. To learn your Telegram ID, it asks you to message the bot, and only you may use it from then on.
5. **The dashboard:** a password, or it makes one.
6. **Running Athena:** it installs bubblewrap if it's missing, and the systemd service, with `sudo`, and starts it.

Settings go in `~/.athena/config.toml`, keeping its comments, and secrets in `~/.athena/.env`, readable only by you. Run `athena setup` again to change any of these. It shows how each is set, and you choose which to change. Everything else is in `config.toml`, which says what each setting does, or on the dashboard's Tools tab.

Then:

- **Check it:** `athena doctor`. Fix anything marked ✗.
- **Follow it:** `athena logs`.
- **Open the dashboard:** see [The dashboard](dashboard.md#set-it-up).

To keep Athena's files somewhere else, set `ATHENA_HOME` before running it, or pass `--config path/to/config.toml`.

## Updating

```bash
athena update
```

This updates Athena with uv, installs any new or changed systemd units, and restarts the service.

## The service

`athena setup` installs it, or run `athena service install`. It writes two systemd units, filled in for your user and your install:

- **`athena.service`** runs Athena, and restarts it if it fails.
- **`athena-stopped.service`** tells you in Telegram if Athena has stopped for good: see [Alerts](dashboard.md#alerts).

The service is hardened. If Athena, or a tool it runs, were taken over, it could change only its own folder, `~/.athena`, and uv's and npm's caches. It couldn't change anything else: not its own code, your home folder (`~/.ssh`, `~/.bashrc`) or the system. It has its own `/tmp`, and can't use `sudo` or gain privileges. If you move `data_dir` out of `~/.athena`, run `athena service install` again, so the unit lets Athena write there. `systemd-analyze security athena` shows what's restricted.

`athena service uninstall` stops and removes the service, and leaves `~/.athena` as it is.

## Moving from a git checkout

Athena used to run from a clone of this repository, as the `pi-assistant` service. To move to the installed version:

1. Install uv and Athena as above.
2. Run `athena setup`. It finds the old checkout, and offers to:
   - stop and remove the `pi-assistant` service;
   - move `config.toml`, `.env` and `data/` into `~/.athena`;
   - bring the config up to date, and install the new service.

   The MCP servers' environments are made again the first time each starts, which takes a few minutes on a Pi.
3. Once Athena's running, delete the old checkout, such as `~/athena`. Your settings and data aren't in it any more.

On a Mac set up from a checkout, run `athena mac install` again, as described in [Your Mac](mac.md).
