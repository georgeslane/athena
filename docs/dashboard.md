# The dashboard

A web page for Athena, on your iPhone or Mac. It follows your device's light or dark mode, and in Safari on an iPhone, Share > Add to Home Screen makes it an app. Its tabs are along the top: on a phone, in a row of their own.

- **Status:** what Athena is doing, and how much it's used: messages, tool calls by tool and the average reply time, for the master agent and in total. It shows how much of the model's context the conversation takes up, what's in memory, and its connections. **Thinking** switches the model's thinking on, short or off, as `/think` does.
- **Agents:** what Athena keeps an eye on and does for you, each with its chat, its DAG, the history of its runs, what its last run kept, and its guidance and files: see [Agents](#agents) and [Agents](agents.md).
- **Memory:** what Athena wanted to remember while it worked by itself, to save or discard. Search memories, past conversations and documents. Choose folders on your Mac to index. List your private words. See [Memory](memory.md) and [Privacy](privacy-and-security.md#privacy).
- **Skills:** write, change and delete skills, see each one's history, and approve the changes Athena suggests. See [Skills](memory.md#skills).
- **Tools:** switch tools and MCP servers on and off, change their settings and give them keys. Choose which of a server's tools Athena uses and which ask first, add servers of your own, and choose the tools found only when they're needed. Changes go into `config.toml`, keeping your comments, and keys into `.env`. They apply as soon as Athena's free, without a restart.

A number on the Memory and Skills tabs says how many of Athena's suggestions are waiting for you, and one on the Agents tab how many requests for your OK.

## Agents

Each agent is something Athena keeps an eye on or does for you, such as finding you jobs (see [Agents](agents.md)). The master agent comes first: Telegram, Siri and the terminal talk to it, and it hears the others' news. Then the others, the one with something said in it most recently first, with a dot on those with something new, and how many proposals each has waiting. **New agent** makes another. Each has five views:

- **Chat:** talk to it. What its DAG's tasks with `alert=True` say, and each run's summary, arrive here too, linked to the run. The master agent's chat also has the other agents' news, linked to their runs.
- **DAG:** one part below another. When it runs, with **Run now** and **Pause**, and what starts it: for each webhook, the address to send it to from an iPhone Shortcut or anything else, to copy, and for each asset, the agent whose task updates it. Then changes its runs propose, showing exactly what would change, to approve or reject; how it works, drawn, with each task that runs for each item as a stack, and the edges from a list ("each"), into a join ("all") and from a branch ("if picked") marked; and its file, which is checked as you write it, saying what's wrong and on which line. **Full page** fills the window with the file and its drawing, side by side, or the file above its drawing on a phone, drawn again as you write; **Done** or Escape puts it back. **How to write it** has the language in brief: [Writing DAGs](dags.md) has all of it.
- **History:** its last 10 runs, each drawn with how each task went, and what each task was given, said and did, and what a branch picked, for each item it ran for.
- **Context:** what its runs last told you, which the next run starts with, and which you can change.
- **Guidance:** how Athena acts in it, at a high level, and its files: PDFs and text files, such as your CV, which its chat and runs read when they'd help. Add them with **Add files**, or drop them in.

At the top, the bell mutes it, **Tools** chooses what may run without asking when you talk to it, and **⋯** names it, chooses its model, or deletes it, though not the master agent.

In its chat:

- **One message at a time.** While Athena's answering you, wherever you asked, nothing more can be sent, so messages can't pile up. The send button becomes a stop button. A message sent while its DAG runs is answered once the task that's running has finished.
- **Photos and files:** press the paper clip, paste or drop them in. Up to 5 photos, which the model sees, as in Telegram: a large one is made smaller first. Up to 5 PDFs or text files, such as Markdown, CSV or JSON, of up to 20 MB each, which go into Athena's workspace for it to read, if Files are on.
- **Commands** work as in Telegram: press **/**, or type `/` to see them. `/stop` and `/retry` act on what you sent here.
- **Requests for your OK** show in the conversation, with what the tool would do and everything it would be given, whether they're for a message you sent here, in Telegram, or one of its tasks. Allow or deny them here or in Telegram: whichever you answer first counts.
- **Questions from its DAG's tasks** (`ask_user`) show here too: press one of the answers it offers, write your own, or skip it, here or in Telegram.
- **Tools that ask first** can be allowed without asking, for your next message or from now on, wherever you're talking to it: press **Tools**. Its DAG says what its tasks may do. Some, such as changing a DAG and pushing to GitHub, always ask.
- **If Athena couldn't answer,** your message stays, to try again or change.
- Files Athena sends go to Telegram, as a task's do.

## Notifications

Athena can notify your iPhone and Mac when it answers a message you sent on the dashboard, an agent has news, something it needs stops working, or it needs your OK. Every agent notifies you, until you mute it with the bell at the top of it; requests for your OK always do. It doesn't while you're looking at the dashboard, which shows it anyway.

1. **On an iPhone or iPad,** add the dashboard to your Home Screen first (in Safari, Share > Add to Home Screen), and open it from there: iOS only notifies web apps you've added. On a Mac, Safari and other browsers can take them as they are, or File > Add to Dock makes it an app.
2. An agent's chat offers to turn them on: press **Turn on**, and allow notifications when asked. Or press the bell in Connections on the Status tab, where **Send a test** checks they arrive, and **Turn off** stops them on that device.

They come through your device's push service, Apple's for Safari, which needs the dashboard over HTTPS, as Tailscale Serve gives it. Each is encrypted so that only your device can read it, and signed with a key Athena keeps in its data folder, so no one else can notify you as Athena. Each shows the start of the message: `notification_text = false` under `[dashboard]` shows only that there's something new.

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

Once something has failed `after_failures` (2) checks in a row, you get a message in Telegram, and a notification on your devices, saying what and why, and another when it works again. Set these under `[alerts]`, or `enabled = false` to stop them.

When Athena itself stops, it can't tell you, so systemd does: `athena-stopped` watches what happens next.

- **Running steadily again within a minute:** that was a blip, and you hear nothing. Nor do you if you, or an update, stopped it.
- **Failing to start five times in ten minutes:** systemd stops trying, and you get a message saying why, with the end of its log.
- **Still stopping and restarting after 15 minutes:** you're told that.

It uses only Python's own library, so it works even if Athena's environment is what's broken. Everything in `.env`, and anything like a token, is taken out of what it sends. Once you've fixed the problem, `sudo systemctl restart athena` starts Athena again.
