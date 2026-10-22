# Using Athena

## Telegram

Send any message, a [photo](#photos), or a PDF or text file (see [Files](tools.md#files)). Commands:

| Command | What it does |
|---|---|
| `/session` | Start a new session: the conversation so far is cleared from the model's context, in every chat. Memories are kept, and earlier conversations can still be searched. `/reset` does the same. |
| `/stop` | Stop the answer Athena is working on, and any messages waiting their turn. |
| `/compact` | Summarise the conversation so far, keeping the last two exchanges, to make room in the model's context. Athena also does this by itself: see [Sessions and context](#sessions-and-context). |
| `/retry` | Answer your last message again, say after changing how much Athena thinks. An answer already given is taken out of the conversation and memory first. |
| `/tasks` | What Athena does by itself, with buttons to run, pause, resume, stop or delete each. See [Tasks](tasks.md). |
| `/think [on\|short\|off]` | How much the model thinks before answering. With no choice, it says which it is, with buttons to change it. |
| `/remember <fact>` | Save a fact. |
| `/recall [query]` | Search memories, with similarity scores. With no query, the most recent. |
| `/forget <id>` | Delete a memory. |
| `/tools` | Tools, and how each MCP server is. |
| `/reload` | Read the tools' settings in `config.toml` again and reconnect to MCP servers, such as after the Mac restarts. |
| `/status` | The model server, memory and tools. |

Only the Telegram users in `telegram.allowed_user_ids` can use it: `athena setup` puts you there. If the list is empty, the bot replies to any message with the sender's ID.

## Photos

Send a photo, with what you want to know as its caption, such as "what's wrong with this plant?". Several sent together (up to 5) are answered as one message, and so is an image sent as a file (JPEG, PNG, WebP or GIF, up to 8 MB). The last 3 messages with photos keep them, so you can ask about one again. Older ones become "[Photo]" in the conversation.

It needs a model that can see, such as Qwen 3.6 35B-A3B. Some MLX conversions of a model leave out the part that sees, so `doctor` shows the model a red square and checks it says red. Photos go only to your model server, never to a tool. They're kept in `~/.athena/data/photos/`, readable only by Athena's user, until you forget everything.

## Sessions and context

Athena keeps the conversation going, so each reply can build on the last. All of it counts towards the model's context, and the more there is, the longer the model takes to read it.

- **A session** is everything since you last started one: send `/session`, or press **New session** on the dashboard. The conversation is cleared from the model's context, and Athena carries on with its memories. Every exchange is kept in memory, so it can still search earlier sessions.
- **Compacting** keeps a long conversation going without filling the context. Once it's `max_history_messages` long (40), or an answer fills `compact_at` of the context window (80%), Athena has the model summarise all but the last two exchanges, in the background. From then on the model sees the summary, then the messages since. `/compact` does it straight away.
- **Context**, on the dashboard, is what Athena reads with your next message: the system prompt, the tools' descriptions and this session's conversation. The limit comes from the model server. If yours doesn't say, set `llm.context_window`.
- **Forget everything**, on the dashboard's Status tab, deletes every memory and the conversation history, empties Athena's folder, and forgets what tasks and checks last found. The tasks and what's watched stay. It starts a new session. Usage statistics stay, as they're only counts and timings.

## Prompt caching

The system prompt, tools and earlier messages stay the same from one request to the next, and old history is compacted in batches. That lets the model server reuse the prompt it has already read, which matters a lot on Apple Silicon. Athena keeps that cache warm: at start-up, after each reply, when the tools change, and every 10 minutes (`llm.warm_up_minutes`). So your messages rarely wait for it.

When the cache is cold, such as just after the Mac restarts, the next message waits while the whole prompt is read again. `doctor`'s last section measures how many tokens the tools add, how long your Mac takes to read them, and whether the cache is working. If it's slow, see [Tools found when needed](tools.md#tools-found-when-needed).

## Siri

Say **"Hey Siri, Ask Athena"** on your iPhone or Mac, then ask your question. Siri reads the answer out, and your question and the answer go to the Telegram chat too.

- **Approvals still happen in Telegram.** If a tool needs your OK, Siri tells you to look there.
- **Siri waits about 25 seconds.** A longer answer arrives in Telegram when it's ready.
- **Say the shortcut's name first,** then your question.

An Apple Shortcut sends what you said to the Pi over Tailscale, with a token. Your iPhone and Mac need Tailscale, on the same tailnet as the Pi.

1. **On the Pi**, make a token and turn Siri on:

   ```bash
   echo "SIRI_TOKEN=$(openssl rand -hex 24)" >> ~/.athena/.env
   grep SIRI_TOKEN ~/.athena/.env      # you'll need it for the shortcut
   ```

   Set `enabled = true` under `[siri]` in `~/.athena/config.toml`, then:

   ```bash
   sudo tailscale serve --bg --http=80 localhost:8090
   sudo systemctl restart athena
   athena doctor                      # the Siri section should say "listening"
   ```

   Tailscale Serve passes requests to Athena from your tailnet, and from nowhere else. Plain HTTP is fine, as Tailscale encrypts the connection.

2. **In the Shortcuts app**, make a shortcut called **Ask Athena** with three actions:
   1. **Ask for Input**: type *Text*, prompt *What do you want to ask?*
   2. **Get Contents of URL**: `http://<the Pi's tailnet name>/ask`. Under *Show More*, set *Method* to *POST*, add a header `Authorization` with the value `Bearer <your SIRI_TOKEN>`, and set *Request Body* to *JSON* with a *Text* field `prompt`, set to *Provided Input*.
   3. **Show Result**, showing *Contents of URL*.

3. **Try it:** "Hey Siri, Ask Athena". From a terminal:

   ```bash
   curl -H "Authorization: Bearer <your SIRI_TOKEN>" -H "Content-Type: application/json" \
     -d '{"prompt": "What time is it?"}' http://<the Pi's tailnet name>/ask
   ```

The token is in the shortcut, which iCloud syncs: take it out before sharing the shortcut.

## Choosing a model

`athena eval` runs short tool-calling scenarios against each model, and reports what passed and how fast:

```bash
athena eval -m gemma-4-26b-a4b-it-4bit -m qwen3.6-35b-a3b-4bit --repeat 3
```

It covers picking the right tool, filling in arguments, answering without tools when none is needed, and a two-step task.

On an M1 Pro with 32 GB, mixture-of-experts models with about 3–4B active parameters are the right size: dense 27B+ models generate too slowly for tool use. Larger models may need a higher GPU memory limit, such as `sudo sysctl iogpu.wired_limit_mb=26624`.

## The command line

| Command | What it does |
|---|---|
| `athena chat` | Chat in the terminal, with the same tools and memory. |
| `athena doctor` | Check the model server, embeddings, database, MCP servers, Telegram and the dashboard. |
| `athena ingest PATH...` | Add `.md` or `.txt` files or folders to memory. Running it again on a file replaces it. |
| `athena reindex` | Re-embed everything with the configured embeddings model. |
| `athena embeddings test` / `use` / `switch` | See [Changing the embeddings model](memory.md#changing-the-embeddings-model). |
| `athena eval` | Compare models on tool calling. |
| `athena run` | Run Athena itself: the service does this. |
