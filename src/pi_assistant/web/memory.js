// The Memory tab: suggested memories, search, private words and your documents.

import { ask, call, day, h, icon, number, plural, state, toast, when } from "./core.js";
import { setReviewCount } from "./dashboard.js";

// -- the Memory tab --------------------------------------------------------------------------------------

// A path on your Mac, with iCloud Drive by name, which a narrow screen can break after a slash.
const breakablePath = (path) =>
  path
    .replace(/^~\/Library\/Mobile Documents\/com~apple~CloudDocs(?=\/|$)/, "iCloud Drive")
    .split(/(?<=\/)/)
    .flatMap((part, i) => (i ? [h("wbr"), part] : [part]));
const fileName = (path) => path.split("/").filter(Boolean).pop() || path;
const PHASES = { connecting: "Connecting to your Mac…", listing: "Listing your documents…" };
// A passage as it reads best on a screen: no PDF page markers (Athena uses them), and no blank lines.
const passage = (text) => text.replace(/^--- page \d+ ---$/gm, "").replace(/\n\s*\n/g, "\n").trim();

const memory_ = {
  search: null, // built once, so what you've typed survives the rest changing
  results: null,
  timer: null,
  suggested: new Map(), // each suggested memory, by id, built once so your edits survive the rest changing
  privacy: null, // built once, so what you've typed survives the rest changing
};

export async function loadMemory() {
  state.memory = await call("GET", "/api/memory");
  renderMemory();
}

function renderMemory() {
  const m = state.memory;
  if (!memory_.search) memory_.search = searchCard();
  setReviewCount("memories", m.suggestions.length);
  const suggested = m.suggestions.length ? suggestedMemories(m) : null;
  if (!memory_.privacy) memory_.privacy = privacyCard();
  memory_.privacy.show(m.privacy);
  document.getElementById("tab-memory").replaceChildren(h("div.stack", {}, suggested, memory_.search, documentsCard(m), memory_.privacy.card));
  clearTimeout(memory_.timer);
  // While Athena indexes, follow how it's going.
  if (m.documents.running) memory_.timer = setTimeout(() => location.hash === "#memory" && loadMemory().catch(() => {}), 1000);
}

// Facts Athena wanted to remember while it worked by itself, which it keeps only if you say so.
function suggestedMemories(m) {
  const ids = new Set(m.suggestions.map((s) => s.id));
  for (const id of memory_.suggested.keys()) if (!ids.has(id)) memory_.suggested.delete(id);
  const items = m.suggestions.map((s) => {
    if (!memory_.suggested.has(s.id)) memory_.suggested.set(s.id, suggestedMemory(s));
    return memory_.suggested.get(s.id);
  });
  return h(
    "section.card.review",
    {},
    h("div.card-head", {}, h("h2", { text: "Suggested memories" }), h("span.aside", { text: plural(items.length, "to review", "to review") })),
    h("p.note", {}, icon("info"), h("span", { text: "Athena wanted to remember these while it worked by itself. It keeps only those you save, as you've edited them." })),
    h("div.review-list", {}, items)
  );
}

function suggestedMemory(s) {
  const text = h("textarea.review-text", { rows: 2, maxLength: 2000, "aria-label": "What to remember" });
  text.value = s.text;
  const fit = () => {
    text.style.height = "auto";
    text.style.height = `${text.scrollHeight + 2}px`;
  };
  text.addEventListener("input", fit);
  requestAnimationFrame(fit);
  const decide = async (event, action) => {
    event.target.disabled = true;
    try {
      state.memory = await call("POST", `/api/memory/suggestions/${s.id}`, action === "approve" ? { action, text: text.value } : { action });
      renderMemory();
      toast(action === "approve" ? "Saved to memory." : "Discarded.");
    } catch (e) {
      toast(e.message, true);
      event.target.disabled = false;
    }
  };
  return h(
    "article.review-item",
    {},
    text,
    h("p.hint", { text: `From ${s.source}, ${day(s.created_at)}` }),
    h(
      "div.tool-actions",
      {},
      h("span.grow"),
      h("button.button.small", { type: "button", text: "Discard", onclick: (e) => decide(e, "discard") }),
      h("button.button.small.primary", { type: "button", text: "Save", onclick: (e) => decide(e, "approve") })
    )
  );
}

function searchCard() {
  const input = h("input", { type: "search", placeholder: "What are you looking for?", autocomplete: "off", enterkeyhint: "search", "aria-label": "Search for" });
  const kind = h(
    "select",
    { "aria-label": "Search in" },
    h("option", { value: "", text: "Everything" }),
    h("option", { value: "document", text: "Documents" }),
    h("option", { value: "fact", text: "Remembered" }),
    h("option", { value: "conversation", text: "Conversations" })
  );
  const button = h("button.button.primary", { type: "submit", text: "Search" });
  memory_.results = h("div.results", { "aria-live": "polite" });
  const form = h(
    "form.search",
    {
      onsubmit: async (event) => {
        event.preventDefault();
        const query = input.value.trim();
        if (!query) return input.focus();
        button.disabled = true;
        memory_.results.replaceChildren(h("p.hint", { text: "Searching…" }));
        try {
          const found = await call("POST", "/api/memory/search", { query, kind: kind.value || null });
          memory_.results.replaceChildren(...(found.results.length ? found.results.map(result) : [h("p.hint", { text: "Nothing found." })]));
        } catch (e) {
          memory_.results.replaceChildren(h("p.form-error", { role: "alert", text: e.message }));
        } finally {
          button.disabled = false;
        }
      },
    },
    input,
    kind,
    button
  );
  return h("section.card", {}, h("div.card-head", {}, h("h2", { text: "Search" }), h("span.aside", { text: "What Athena remembers, and your documents" })), form, memory_.results);
}

function result(r) {
  if (r.kind === "document") {
    return h(
      "article.result",
      {},
      h("div.result-head", {}, h("span.result-title", { text: fileName(r.path) }), h("span.result-date", { text: r.date && `Changed ${day(r.date)}` })),
      h("div.result-path", {}, breakablePath(r.path)),
      h("p.result-text", { text: passage(r.text) })
    );
  }
  return h(
    "article.result",
    {},
    h("div.result-head", {}, h("span.result-kind", { text: r.kind === "fact" ? "Remembered" : "Conversation" }), h("span.result-date", { text: day(r.date) })),
    h("p.result-text", { text: r.text })
  );
}

// Words that identify you, which tools can't send where nothing about you should go, such as Claude's projects.
function privacyCard() {
  const terms = h("textarea.review-text.terms", { rows: 4, maxLength: 20000, "aria-label": "Private words, one per line", placeholder: "Your full name\nYour address\nYour family's names" });
  let shown = ""; // as saved: what you've typed is kept until you save it
  const automatic = h("p.hint");
  const blocked = h("span");
  const save = h("button.button.primary", { type: "submit", text: "Save" });
  const form = h(
    "form.privacy",
    {
      onsubmit: async (event) => {
        event.preventDefault();
        save.disabled = true;
        try {
          state.memory.privacy = await call("POST", "/api/privacy", { terms: terms.value.split("\n") });
          shown = "";
          view.show(state.memory.privacy);
          toast("Saved your private words.");
        } catch (e) {
          toast(e.message, true);
        } finally {
          save.disabled = false;
        }
      },
    },
    terms,
    automatic,
    h("div.tool-actions", {}, h("span.grow"), save)
  );
  const text = h("input", { type: "search", placeholder: "Try some text", autocomplete: "off", enterkeyhint: "go", "aria-label": "Text to check" });
  const check = h("button.button", { type: "submit", text: "Check" });
  const found = h("div.results", { "aria-live": "polite" });
  const tryIt = h(
    "form.search.try",
    {
      onsubmit: async (event) => {
        event.preventDefault();
        if (!text.value.trim()) return text.focus();
        check.disabled = true;
        try {
          const r = await call("POST", "/api/privacy/check", { text: text.value });
          found.replaceChildren(
            r.findings.length
              ? h("ul.findings", {}, r.findings.map((f) => h("li", {}, h("span", { text: f.text ? `${f.kind}: ` : f.kind }), f.text ? h("strong", { text: f.text }) : null)))
              : h("p.hint", { text: "Nothing personal found." })
          );
        } catch (e) {
          found.replaceChildren(h("p.form-error", { role: "alert", text: e.message }));
        } finally {
          check.disabled = false;
        }
      },
    },
    text,
    check
  );
  const card = h(
    "section.card",
    {},
    h("div.card-head", {}, h("h2", { text: "Private words" }), h("span.aside", { text: "Never sent to Claude" })),
    h(
      "p.note",
      {},
      icon("lock"),
      h("span", {}, "Words that identify you, one per line, such as your full name, family's names and employer. ", blocked, " Tools that ask first warn you when they'd send one. Email addresses, phone numbers, postcodes and card numbers are caught without being listed.")
    ),
    form,
    h("div.try-head", {}, h("span.field-label", { text: "Try it" }), h("span.field-help", { text: "See what would be caught in some text." })),
    tryIt,
    found
  );
  const view = {
    card,
    show(p) {
      const saved = p.terms.join("\n");
      if (terms.value === shown) terms.value = saved; // unless you've changed it and not saved yet
      shown = saved;
      requestAnimationFrame(() => {
        terms.style.height = "auto";
        terms.style.height = `${terms.scrollHeight + 2}px`;
      });
      automatic.textContent = p.automatic.length ? `And your name, from config.toml: ${p.automatic.join(", ")}.` : "";
      const claude = ["hand_to_claude", "answer_claude", "extend_claude_project"];
      blocked.textContent = !p.block.length
        ? "No tool is stopped by them: [privacy] block in config.toml is empty."
        : p.block.length === claude.length && claude.every((t) => p.block.includes(t))
          ? "Athena won't send any of them to Claude."
          : `These tools won't send any of them: ${p.block.join(", ")}.`;
    },
  };
  terms.addEventListener("input", () => {
    terms.style.height = "auto";
    terms.style.height = `${terms.scrollHeight + 2}px`;
  });
  return view;
}

function documentsCard(m) {
  const d = m.documents;
  const parts = [];
  if (!d.folders.length) {
    parts.push(
      h(
        "p.note.setup",
        {},
        icon("info"),
        h("span", { text: "Choose folders on your Mac to index, to search here or ask Athena about. Your Mac needs setting up first: see the README, “Documents”." })
      )
    );
  } else {
    parts.push(h("ul.folders", {}, d.folders.map((f) => folderRow(f, m.editable && !d.running))));
  }
  if (d.running && d.run) parts.push(progress(d.run));
  else if (d.run) parts.push(...outcome(d.run));
  else if (d.indexed) parts.push(h("p.hint", { text: `Indexed ${when(Date.parse(d.indexed) / 1000)}.` }));
  if (d.files) {
    const extra = d.ingested ? ` · ${plural(d.ingested, "note")} from pi-assistant ingest` : "";
    parts.push(h("p.hint", { text: `In memory: ${plural(d.files, "document")}, in ${plural(d.passages, "passage")}${extra}.` }));
  }
  const index = d.running
    ? h("button.button", { type: "button", text: "Stop", onclick: () => indexing("stop") })
    : h("button.button.primary", { type: "button", text: "Index now", disabled: !d.folders.length, onclick: () => indexing("index") });
  parts.push(h("div.tool-actions", {}, h("button.button", { type: "button", text: "Add a folder…", disabled: !m.editable, onclick: addFolder }), h("span.grow"), index));
  return h("section.card.documents", {}, h("div.card-head", {}, h("h2", { text: "Documents" }), h("span.aside", { text: d.mac ? `From ${d.mac}` : "From your Mac" })), ...parts);
}

function folderRow(f, editable) {
  let count = f.files ? plural(f.files, "document") : "Not indexed yet";
  if (f.no_text) count += ` · ${number(f.no_text)} without text`;
  return h(
    "li.folder",
    {},
    h("div.folder-name", {}, breakablePath(f.folder)),
    h("span.folder-count", { text: count }),
    h("button.button.quiet.small", { type: "button", text: "Remove", disabled: !editable, "aria-label": `Remove ${f.folder}`, onclick: () => removeFolder(f.folder) })
  );
}

function progress(run) {
  const reading = run.state === "reading";
  const label = reading ? `Reading ${number(run.read)} of ${plural(run.to_read, "new or changed document")}…` : PHASES[run.state] || "Indexing…";
  const share = reading && run.to_read ? Math.min(1, run.read / run.to_read) : 0;
  return h("div.indexing", { role: "status" }, h("p", { text: label }), h("div.bar", {}, h("span", { style: { width: `${(share * 100).toFixed(1)}%` } })));
}

function outcome(run) {
  const out = [];
  const finished = run.finished ? when(run.finished) : "";
  if (run.state === "failed") out.push(h("p.form-error", { role: "alert", text: run.error }));
  else if (run.state === "stopped") out.push(h("p.hint", { text: `Stopped ${finished}. What was done is kept: index again to carry on.` }));
  else {
    const changes = [["added", run.added], ["updated", run.updated], ["removed", run.removed]].filter(([, n]) => n).map(([what, n]) => `${number(n)} ${what}`);
    const unchanged = run.unchanged ? `, ${number(run.unchanged)} unchanged` : "";
    out.push(h("p.hint", { text: `Indexed ${finished}: ${changes.join(", ") || "nothing new"}${unchanged}.` }));
  }
  const notes = Object.entries(run.folder_errors || {}).map(([folder, why]) => `${folder}: ${why}`);
  const skipped = run.skipped || {};
  if (skipped.cloud) notes.push(`${plural(skipped.cloud, "document is", "documents are")} only in iCloud. To index them, download them on your Mac: in Finder, Download Now.`);
  if (skipped.too_big) notes.push(`${plural(skipped.too_big, "document is", "documents are")} too big to read.`);
  if (skipped.unreadable) notes.push(`${plural(skipped.unreadable, "folder")} inside couldn't be opened.`);
  if (run.truncated) notes.push("Your Mac listed the first 50,000 documents only: choose smaller folders.");
  if (notes.length) out.push(h("ul.run-notes", {}, notes.map((note) => h("li", { text: note }))));
  if (run.failures) {
    const more = run.failures > run.failed.length ? h("li.hint", { text: `…and ${number(run.failures - run.failed.length)} more` }) : null;
    out.push(
      h(
        "details.failures",
        {},
        h("summary", { text: `Couldn't read ${plural(run.failures, "document")}` }),
        h("ul", {}, run.failed.map((f) => h("li", {}, h("span.folder-name", {}, breakablePath(f.file)), h("span.hint", { text: ` ${f.why}` }))), more)
      )
    );
  }
  return out;
}

async function indexing(action) {
  try {
    state.memory = await call("POST", `/api/documents/${action}`, {});
    renderMemory();
  } catch (e) {
    toast(e.message, true);
  }
}

async function removeFolder(folder) {
  const ok = await ask({ title: `Stop indexing ${folder}?`, body: [h("p", { text: "Its documents are taken out of Athena's memory. They stay on your Mac." })], confirm: "Remove" });
  if (!ok) return;
  try {
    state.memory = await call("POST", "/api/documents/folders", { remove: folder });
    renderMemory();
    toast(`Removed ${folder}.`);
  } catch (e) {
    toast(e.message, true);
  }
}

// A dialog for choosing a folder on your Mac: it asks the Mac what's in each folder you open.
function addFolder() {
  const dialog = document.getElementById("dialog");
  const path = h("input", { type: "text", spellcheck: false, autocapitalize: "none", autocomplete: "off", placeholder: "~/Documents" });
  const where = h("div.crumbs");
  const list = h("div.browse", { "aria-live": "polite" });
  const error = h("p.form-error", { role: "alert" });
  const add = h("button.button.primary", { type: "button", text: "Add this folder" });
  const cancel = h("button.button", { type: "button", text: "Cancel", onclick: () => dialog.close() });
  let asked = 0;
  async function go(folder) {
    const mine = ++asked; // only the latest answer counts
    error.textContent = "";
    list.replaceChildren(h("p.hint", { text: "Asking your Mac…" }));
    try {
      const data = await call("GET", `/api/documents/browse?path=${encodeURIComponent(folder)}`);
      if (mine !== asked) return;
      if (data.path) path.value = data.path;
      where.replaceChildren(...crumbs(data.path, go));
      const items = data.folders.map((f) => h("button.browse-item", { type: "button", onclick: () => go(f.path) }, icon("folder"), h("span", { text: f.name })));
      let note = "";
      if (data.path) note = data.documents ? `${plural(data.documents, "document")} directly in here.` : items.length ? "" : "Nothing to index in here.";
      list.replaceChildren(...items, ...(note ? [h("p.hint", { text: note })] : [])); // (replaceChildren would show a null as "null")
    } catch (e) {
      if (mine !== asked) return;
      list.replaceChildren();
      error.textContent = `${e.message}\nYou can still type a folder's path.`;
    }
  }
  add.addEventListener("click", async () => {
    add.disabled = true;
    try {
      state.memory = await call("POST", "/api/documents/folders", { add: path.value });
      dialog.close();
      renderMemory();
      toast(`Added ${path.value.trim()}. Press Index now to index it.`);
    } catch (e) {
      error.textContent = e.message;
    } finally {
      add.disabled = false;
    }
  });
  dialog.onclose = null;
  dialog.replaceChildren(
    h("h2", { text: "Add a folder" }),
    h("p", { text: "Choose a folder on your Mac. Athena indexes the documents in it, and in the folders inside it." }),
    where,
    list,
    h("label.field", {}, h("span.field-label", { text: "Folder" }), path),
    error,
    h("div.tool-actions", {}, cancel, add)
  );
  dialog.showModal();
  go("");
}

function crumbs(path, go) {
  const top = h(path ? "button.crumb" : "span", { type: path ? "button" : undefined, text: "Your Mac", onclick: path ? () => go("") : undefined });
  if (!path) return [top];
  const parts = path.split("/");
  return [
    top,
    ...parts.flatMap((part, i) => {
      const name = part === "~" ? "Home" : part || "/";
      const last = i === parts.length - 1;
      const target = parts.slice(0, i + 1).join("/") || "/";
      return [h("span.sep", { text: "›" }), last ? h("span", { text: name }) : h("button.crumb", { type: "button", text: name, onclick: () => go(target) })];
    }),
  ];
}
