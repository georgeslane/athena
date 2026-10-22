# The dashboard

A web page for Athena, on your iPhone or Mac. It follows your device's light or dark mode, and in Safari on an iPhone, Share > Add to Home Screen makes it an app. On a phone, tap the shield in the corner for the other tabs.

- **Status:** what Athena is doing, and how much it's used: messages, tool calls by tool and the average reply time, for this session and in total. It shows how much of the model's context the conversation takes up, what's in memory, and its connections. **Thinking** switches the model's thinking on, short or off, as `/think` does.
- **Chat:** talk to Athena, in any of your sessions: see [Chat](#chat).
- **Activity:** what Athena did, newest first: your messages and its answers, the tasks it ran, its checks on what it's watching, alerts, and when it started. Each opens to its steps: every tool it used, what that was given and gave back, whether it worked, and whether you allowed it, a task was allowed it without asking, or you declined it. It also says what the privacy check found in what a tool sent. Checks that found nothing, one after another, show as one line. It's kept for 30 days.
- **Tasks:** what Athena does by itself, and what it's watching. Add, change, run, pause and delete them, and see what each said and did last time, tool by tool. See [Tasks](tasks.md).
- **Memory:** what Athena wanted to remember while it worked by itself, to save or discard. Search memories, past conversations and documents. Choose folders on your Mac to index. List your private words. See [Memory](memory.md) and [Privacy](privacy-and-security.md#privacy).
- **Skills:** write, change and delete skills, see each one's history, and approve the changes Athena suggests. See [Skills](memory.md#skills).
- **Board:** the coding projects Athena has handed to Claude. See [Projects for Claude](claude-projects.md).
- **Tools:** switch tools and MCP servers on and off, change their settings and give them keys. Choose which of a server's tools Athena uses and which ask first, add servers of your own, and choose the tools found only when they're needed. Changes go into `config.toml`, keeping your comments, and keys into `.env`. They apply as soon as Athena's free, without a restart.

A number on the Memory and Skills tabs says how many of Athena's suggestions are waiting for you, and one on the Chat tab how many requests for your OK.

## Chat

Each session is a conversation, which Telegram, Siri and the terminal share: what you start in one, you can carry on in another. The current session is the one used last, which the model has read already. Your other sessions are listed newest first, and sending a message in one carries it on, as the current session. **New chat** starts another.

- **One message at a time.** While Athena's answering you, wherever you asked, nothing more can be sent, and sessions can't be changed, so messages can't pile up. The send button becomes a stop button.
- **Commands** work as in Telegram: type `/` to see them. `/stop` and `/retry` act on what you sent here.
- **Requests for your OK** show in the conversation, with what the tool would do and everything it would be given, whether they're for a message you sent here, in Telegram, or a task. Allow or deny them here or in Telegram: whichever you answer first counts.
- **Tools that ask first** can be allowed without asking, for your next message or for the rest of the session, wherever you're talking to Athena in it: press **Tools**. Some, such as scheduling tasks and pushing to GitHub, always ask.
- **If Athena couldn't answer,** your message stays, to try again or change.
- Files Athena sends go to Telegram, as a task's do.

## Set it up

Athena serves the dashboard on the Pi only, at `http://127.0.0.1:8092`. Tailscale Serve makes it reachable from your own devices, and from nowhere else, over HTTPS:

```bash
sudo tailscale serve --bg --https=443 localhost:8092
```

Then open `https://<the Pi's tailnet name>/`, such as `https://pi.tail1234.ts.net/`, and sign in with the password from `athena setup`. It's `DASHBOARD_TOKEN` in `~/.athena/.env`. If HTTPS isn't on for your tailnet yet, the command gives a link to turn it on.

You stay signed in for 90 days. To change the password, run `athena setup` and choose the dashboard: when you're done, it offers to restart Athena, which reads the password when it starts. That signs every device out. After 10 wrong passwords in 10 minutes, it waits a few minutes before taking another, or until Athena restarts.

## The status board

With a [Pimoroni Display HAT Mini](https://pinout.xyz/pinout/display_hat_mini) on the Pi, a status board shows what Athena is doing: idle, working on your request, or waiting for your approval, with an LED that's blue while it works and flashes amber when it needs you.

The board is its own project, [pi-display-microservice](https://github.com/georgeslane/pi-display-microservice), with its own installer and settings. It asks Athena what it's doing through a small HTTP API, so either can be restarted or updated without the other. Athena's side is `[display]` in `config.toml`:

- **Where it listens:** only on the Pi, at `http://127.0.0.1:8091/v1/status`. To serve a board on another machine, set `host`, and a `token` for the board to send.
- **Your messages:** the API includes the start of your latest message. `show_task = false` keeps it off the screen.

The API is one request, `GET /v1/status`, answered with JSON, which can wait for the next change (`?wait=25&after=<version>`). pi-display-microservice's README has the format. `doctor` checks it's answering.

## Alerts

Every `every_minutes` (5), Athena checks that:

- the model server and embeddings server answer;
- each MCP server that's switched on is connected, and each built-in tool started;
- there's more than `min_free_gb` (1 GB) free where its data is.

Once something has failed `after_failures` (2) checks in a row, you get a message in Telegram saying what and why, and another when it works again. Set these under `[alerts]`, or `enabled = false` to stop them.

When Athena itself stops, it can't tell you, so systemd does: `athena-stopped` watches what happens next.

- **Running steadily again within a minute:** that was a blip, and you hear nothing. Nor do you if you, or an update, stopped it.
- **Failing to start five times in ten minutes:** systemd stops trying, and you get a message saying why, with the end of its log.
- **Still stopping and restarting after 15 minutes:** you're told that.

It uses only Python's own library, so it works even if Athena's environment is what's broken. Everything in `.env`, and anything like a token, is taken out of what it sends. Once you've fixed the problem, `sudo systemctl restart athena` starts Athena again.
