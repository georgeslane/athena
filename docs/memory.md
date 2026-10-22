# Memory

Athena remembers three kinds of thing, each searched by meaning and by words:

- **Facts** it saves with `remember`, or you save with `/remember`, in the third person, such as "George's sister Anna's birthday is 14 March". The closest facts to each message (up to `recall_top_k`, within `recall_max_distance`) are added to it automatically, with the time.
- **Past conversations:** after each reply, your message and the reply are added to memory. `search_memory` finds them, so Athena can look up what you talked about in an earlier session. They're not added to messages automatically, so an old answer can't be mistaken for a current one.
- **Documents:** passages of your documents, indexed from your Mac when you ask (see [Documents](#documents)), and notes you add with `athena ingest`.

**Searching:** `search_memory`, `/recall` and the dashboard search by meaning and by words together, and merge the two. Meaning finds what's related in other words. Words find exact names, numbers and references that meaning can miss, such as a ticker or "Project Nimbus", matched without case, accents or endings. Words still work when the embeddings server is down. `/recall something` shows similarity scores: if unrelated memories keep appearing, lower `recall_max_distance`; if relevant ones are missed, raise it.

**From tasks:** what Athena wants to remember while it works by itself is a suggestion until you save it on the dashboard's Memory tab. Something it read on the web could have put the idea there. What you tell it in chat is saved straight away.

**Forgetting:** `/forget <id>` deletes one memory, and **Forget everything** on the dashboard deletes them all.

**Storage:** memories, conversations and usage statistics are all in `~/.athena/data/assistant.db`. Back up that file.

## Documents

Athena can search the documents on your Mac by meaning, and say which file has what you're after. Choose folders on the dashboard's **Memory** tab and press **Index now**: Athena reads them then, and never by itself. Setting up the Mac is in [Your Mac](mac.md).

- **Asking:** in Telegram, ask something like "find my council tax letter". Athena finds the file, gives you its path, and can summarise it or answer from it with `read_document`. That reads the text Athena keeps, so it works while the Mac sleeps.
- **What's read:** plain text, Markdown, PDF, Word, RTF, OpenDocument and HTML, up to `max_chars_per_file` (300,000 characters) of each. Scanned PDFs have no text, and Pages, Numbers and Keynote files aren't read.
- **Indexing again** reads only what's new or changed, and takes out what's gone. Stopping part way keeps what's done.
- **Removing a folder** takes its documents out of memory. They stay on your Mac.

What's kept on the Pi is each document's text, in passages, with its path. Embeddings are made on the Mac.

## Embeddings

Memories are searched by meaning with vectors from an embeddings model: [EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2), served by oMLX on the Mac with the model. With `base_url` and `api_key` empty under `[embeddings]`, it uses `[llm]`'s.

1. **Update oMLX** to 0.7.1 or later.
2. **Download the model:** in oMLX, add `mlx-community/embeddinggemma-2-bf16` (1.5 GB) beside your model.
3. **Check it:** `athena embeddings test`. If oMLX calls it something else, the error lists the names it has: choose it with `athena setup`.

## Changing the embeddings model

Vectors from different models can't be compared, so switching means re-embedding every memory, even for another quantisation of the same model. Each model also scores distances on its own scale, so the cut-offs change too. One command does it all:

```bash
athena embeddings switch embeddinggemma-2-8bit           # another model where embeddings come from now
athena embeddings switch embeddinggemma-2-bf16 --on-mac  # moving them to the model's server
```

For a model in Ollama on the Pi, it downloads it first. Then it stops Athena, and tests the new model against the current one on 40 questions about made-up memories: how often each finds the right memory, how fast, and the cut-offs that suit it. It says what it will change, and asks. If you agree, it sets the model and cut-offs in `config.toml`, keeping a copy in `config.toml.bak`, and re-embeds every memory. If that fails part way, nothing changes. Athena starts again either way.

To test models without switching: `athena embeddings test -m MODEL -m ANOTHER`. Avoid `float16` builds, which can return broken vectors: Athena refuses to save those.

## Skills

A skill is how to do something your way: how you like trip plans laid out, what goes in your morning briefing. Each has a name, when to use it, and how. Athena sees every skill's name and when to use it, and reads the rest with `read_skill` when it needs it, so a skill costs a line of the prompt until it's used.

- **You** write, change and delete them on the dashboard's Skills tab. Every version is kept, so you can go back.
- **Athena** writes one when you ask, such as "that's how I like trip plans: make it a skill", saved straight away since you're there.
- **Working by itself**, what it writes is a suggestion, shown line by line with why, to approve, change or discard. A skill is instructions Athena follows, so a web page mustn't be able to rewrite one.
- **After using a skill**, Athena looks back at how it went once you've finished chatting, and suggests a change if one would help next time.
- **Spotting habits:** the "Skills from habits" example task looks back over the week with `recent_work`, and suggests a skill for anything done the same way three or more times.
