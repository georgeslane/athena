# Troubleshooting

Start with `athena doctor`, then `athena logs`, and the MCP servers' logs in `~/.athena/data/logs/mcp-*.log`.

**The model**
- **"Can't reach the model server":** it isn't running, is only listening on localhost, or its name doesn't resolve from the Pi. Try `curl http://my-mac.local:8000/v1/models` from the Pi.
- **The model answers instead of using tools:** check that tool calling works in `doctor`, then compare models with `athena eval`.

**Memory**
- **"The embeddings server doesn't have …":** oMLX is older than 0.7.1, hasn't downloaded `mlx-community/embeddinggemma-2-bf16`, or calls it something else: the error lists what it has.
- **Memories show up in replies about something else, or don't when they should:** the recall cut-off doesn't suit the embeddings model. Run `athena embeddings test` and set `recall_max_distance` to the cut-off it suggests.
- **"The memory index was built with … but the config now uses …":** `config.toml` names a different embeddings model from the one your memories were stored with. Run `athena embeddings switch` as it says, or put the old model back.

**Telegram**
- **The bot ignores you:** your ID isn't in `telegram.allowed_user_ids`. Run `athena setup` and choose Telegram.
- **"Conflict: terminated by other getUpdates request":** two copies of Athena are running with the same token.
- **No message when Athena stopped:** check `systemctl status athena-stopped` and `journalctl -u athena-stopped`. It needs the bot token and your ID. For the end of the log to be included, your user must be able to read the journal (it's in the `adm` group on Raspberry Pi OS).

**The dashboard**
- **It doesn't load:** see what `doctor` says under "Dashboard". If it's answering there, check `sudo tailscale serve status` shows `localhost:8092`, and that Tailscale is connected on your device.
- **"Can't reach Athena":** the service has stopped or is restarting. `athena logs` says why.

**Tools**
- **"Couldn't start it in a sandbox":** install bubblewrap (`sudo apt install bubblewrap`), then run `doctor`, which says whether it can make a sandbox. A server that needs something from your home folder can be given it with `read_only_paths`.
- **A server says "waiting for you":** some of its tools are new or have changed since you approved them. Open its card on the Tools tab to see what changed, and approve them.
- **A server's switch won't stay on:** the dashboard says what it needs first, such as a key. If it's on but shows an error, open it: the error and the server's log usually say why.
- **"Trading 212 didn't accept the API key":** check the key and secret, and that the key is for the account `environment` says. If it's restricted to your IP address, check that hasn't changed: compare `curl -s https://api.ipify.org` with the key's settings.

**Your Mac**
- **A server on a Mac fails:** run `ssh athena-mac hello` on the Pi (or `ssh athena-laptop hello`). If that doesn't say there's no server called 'hello', the problem is SSH: check Remote Login is on and the Mac is awake. If it does, check `~/Library/Logs/Athena/` on the Mac, and that iMCP is running.
- **"Couldn't reach your Mac" when indexing:** the laptop is asleep, off your tailnet, or not set up yet. What SSH said is at the end of the message, and in `~/.athena/data/logs/documents.log`.
- **"macOS hasn't allowed access" when indexing:** allow "python3" when the laptop asks, or in System Settings > Privacy & Security > Files and Folders, then index again.

**Everything else**
- **The status board says "Offline" while Athena runs:** see what `doctor` says under "Status API". Problems with the board itself are in pi-display-microservice's README.
- **The Siri shortcut fails:** check `doctor` says Siri is "listening", and that Tailscale is connected on the device you're asking from. A 401 means the token in the shortcut doesn't match `SIRI_TOKEN`.
