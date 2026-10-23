// The Agents tab. Each agent is something Athena keeps an eye on and does for you, such as finding you jobs: a
// conversation, and a DAG of tasks it runs by itself. The master agent is always first: Telegram, Siri and the
// terminal talk to it, it hears the others' news, and its DAG keeps an eye on things for them to start from. Each
// agent has views: Chat, its DAG (dag.js), the History of its runs (runs.js), the Context its last run kept for the
// next, and its Guidance, with files.
//
// The address says what's shown: #agents for the master agent, #agents/<id> for one's chat, #agents/<id>/<view>
// for another view, #agents/<id>/runs/<run> for a run, and #agents/all for the list, which a phone shows on its own.

import { ask, base64, bytes, call, clock, h, icon, isoDay, now, plural, state, toast } from "./core.js";
import { chatView, chooseTools, loadPage, renderChat } from "./chat.js";
import { dagView } from "./dag.js";
import { runsView } from "./runs.js";

const VIEWS = [
  ["chat", "Chat"],
  ["dag", "DAG"],
  ["runs", "History"],
  ["context", "Context"],
  ["guidance", "Guidance"],
];

// What the tab knows, which chat.js shares (as state.agents).
const agents_ = (state.agents = {
  data: null, // GET /api/agents: the agents, whether Athena's answering you, and what's waiting for your OK
  id: null, // the agent shown, or null until the main one's known
  view: "chat",
  run: null, // the run shown on its History view
  list: false, // on a phone: the list of agents rather than one
  pages: new Map(), // id -> GET /api/agents/<id>: its messages and settings, as loaded
  once: new Set(), // tools allowed without asking for your next message only (chat.js)
  render: () => render(),
  drafts: new Map(), // "<id>/<view>" -> context or guidance being written, until it's saved
  seen: readSeen(), // id -> the newest message you've seen of each, for what's new
  last: null, // the newest message in any agent, as the status said last
  loading: null,
  again: false,
  els: null,
});

const summary = (id) => (agents_.data ? agents_.data.sessions.find((s) => s.id === id) : null);
const isMaster = (id) => agents_.data && id === agents_.data.master;

// -- loading -------------------------------------------------------------------------------------------------

export async function loadAgents() {
  route();
  if (agents_.loading) {
    agents_.again = true;
    return agents_.loading;
  }
  agents_.loading = (async () => {
    do {
      agents_.again = false;
      agents_.data = await call("GET", "/api/agents");
      if (agents_.id === null && !agents_.list) {
        agents_.id = agents_.data.master; // #agents is the master agent, shown at its own address
        history.replaceState(null, "", address(agents_.id, agents_.view));
      }
      if (agents_.id !== null && !(await loadPage(agents_.id))) {
        agents_.id = agents_.data.master; // it's gone, as after deleting it
        history.replaceState(null, "", address(agents_.id, "chat"));
        agents_.view = "chat";
        await loadPage(agents_.id);
      }
      render();
    } while (agents_.again);
  })().finally(() => (agents_.loading = null));
  return agents_.loading;
}

function route() {
  const [, which, view, run] = location.hash.split("/");
  agents_.list = which === "all";
  if (which && /^\d+$/.test(which)) agents_.id = Number(which);
  else if (!agents_.list) agents_.id = null;
  agents_.view = VIEWS.some(([v]) => v === view) ? view : "chat";
  agents_.run = view === "runs" && run && /^run_[0-9A-Za-z_]+$/.test(run) ? run : null;
}

export const address = (id, view = "chat", run = null) => `#agents/${id}${view !== "chat" ? `/${view}` : ""}${run ? `/${run}` : ""}`;

// Athena's status has changed, or something in an agent: the tab's count, and the tab itself if it's showing.
export function followAgents(status) {
  const last = status.chat && status.chat.last;
  if (last && agents_.last && last.id > agents_.last.id && last.role === "assistant" && !location.hash.startsWith("#agents")) {
    toast(`${state.overview ? state.overview.assistant.name : "Athena"}'s written to you. It's in Agents.`);
  }
  if (status.chat) agents_.last = last || { id: 0 };
  const count = status.chat ? status.chat.approvals : 0;
  const link = document.querySelector('.tab[data-tab="agents"]');
  let badge = link && link.querySelector(".tab-count");
  if (link && !count && badge) badge.remove();
  if (link && count) {
    if (!badge) link.append((badge = h("span.tab-count")));
    badge.textContent = String(count);
    badge.title = `${plural(count, "request")} for your OK`;
  }
  if (location.hash.startsWith("#agents")) loadAgents().catch(() => {});
}

// What's new in each agent: the newest message you've seen in each, kept on this device.
function readSeen() {
  try {
    const seen = JSON.parse(localStorage.getItem("athena.seen") || "null");
    return seen && typeof seen === "object" ? seen : null;
  } catch {
    return null;
  }
}

function markSeen(id) {
  if (!agents_.seen) agents_.seen = Object.fromEntries(agents_.data.sessions.map((s) => [s.id, s.last])); // at first, all of it
  const s = summary(id);
  if (s && (agents_.seen[id] || 0) < s.last) agents_.seen[id] = s.last;
  try {
    localStorage.setItem("athena.seen", JSON.stringify(agents_.seen));
  } catch {
    // private browsing: what's new is only known until the page is reloaded
  }
}

const unread = (s) => Boolean(agents_.seen) && s.last > (agents_.seen[s.id] || 0);

// -- drawing it ----------------------------------------------------------------------------------------------

function build() {
  const e = (agents_.els = {});
  e.list = h("ol.session-list");
  e.newAgent = h("button.button.small.primary", { type: "button", onclick: newAgent }, icon("plus"), "New agent");
  e.sidebar = h("aside.chat-sessions", { "aria-label": "Your agents" }, h("div.chat-sessions-head", {}, h("h2", { text: "Agents" }), e.newAgent), e.list);
  e.back = h("a.button.quiet.small.chat-back", { href: "#agents/all", "aria-label": "Your agents" }, icon("back"), h("span.wide-only", { text: "Agents" }));
  e.title = h("h2.chat-title");
  e.sub = h("p.chat-sub");
  e.bell = h("button.button.small.bell", { type: "button", "data-session": "", onclick: toggleMuted });
  e.tools = h("button.button.small.chat-tools", { type: "button", onclick: () => chooseTools(agents_.id) });
  e.more = h("button.button.small.chat-more", { type: "button", "aria-label": "About this agent", title: "Name, model, or delete this agent", onclick: aboutAgent }, icon("more"));
  e.head = h("header.chat-head", {}, e.back, h("div.chat-heading", {}, e.title, e.sub), e.bell, e.tools, e.more);
  e.views = h("nav.agent-views", { "aria-label": "Views" });
  e.view = h("div.agent-view");
  e.main = h("section.chat-main", {}, e.head, e.views, e.view);
  e.root = h("div.chat.agents", {}, e.sidebar, e.main);
  document.getElementById("tab-agents").replaceChildren(e.root);
}

function render() {
  if (!agents_.data) return;
  if (!agents_.els) build();
  const e = agents_.els;
  const id = agents_.id;
  e.root.dataset.view = agents_.list ? "list" : "conversation";
  e.root.dataset.agentView = agents_.view;
  if (id !== null && !agents_.list && agents_.view === "chat") markSeen(id);
  renderList(id);
  if (id === null) return;
  renderHead(id);
  renderViews(id);
  mountView(id);
}

function renderList(id) {
  const asking = new Set(agents_.data.approvals.map((a) => a.session ?? agents_.data.master));
  const items = agents_.data.sessions.map((s) => {
    const meta = [];
    if (s.master) meta.push(h("span.current", { text: "Telegram" }));
    meta.push(when(s.last_used));
    if (s.dag) meta.push(s.dag.running ? h("span.running", { text: "Running" }) : s.dag.paused ? "Paused" : s.dag.when);
    return h(
      "li",
      {},
      h(
        "a.session",
        { href: address(s.id), "aria-current": s.id === id ? "true" : null, "data-master": s.master ? "" : null, "data-unread": String(unread(s) && !(s.id === id && agents_.view === "chat")) },
        h(
          "span.session-line",
          {},
          s.master ? h("span.master-mark", { title: "The master agent" }, icon("crown")) : null,
          h("span.session-title", { text: s.title || "New agent" }),
          asking.has(s.id) ? h("span.session-flag.asking", { text: "Needs your OK" }) : null,
          s.proposals ? h("span.session-flag.asking", { text: plural(s.proposals, "proposal"), title: "Changes its runs proposed, for you to approve" }) : null,
          s.muted ? h("span.session-flag", { title: "Muted: it doesn't notify you", "aria-label": "Muted" }, icon("bell_off")) : null,
          h("span.unread-dot", { title: "Something new" })
        ),
        h("span.session-meta", {}, meta.flatMap((part, i) => (i ? [" · ", part] : [part])))
      )
    );
  });
  agents_.els.list.replaceChildren(...items);
  agents_.els.newAgent.disabled = agents_.data.busy;
}

function renderHead(id) {
  const e = agents_.els;
  const s = summary(id);
  const page = agents_.pages.get(id) || { muted: false, name: "" };
  e.title.textContent = (s && s.title) || page.name || "New agent";
  const parts = [isMaster(id) ? "Master agent: Telegram, Siri and the terminal talk to it" : `Last active ${when(s ? s.last_used : 0)}`];
  if (s && s.dag) parts.push(s.dag.running ? "its DAG is running" : s.dag.paused ? "its DAG is paused" : `runs ${s.dag.when}`);
  else parts.push("no DAG yet");
  e.sub.textContent = parts.join(" · ");
  const allowed = new Set([...(page.allowed_tools || []), ...agents_.once]);
  const label = h("span.wide-only", { text: allowed.size ? `${allowed.size} allowed` : "Tools" });
  e.tools.replaceChildren(...[icon("shield"), label, allowed.size ? h("span.tools-count", { text: String(allowed.size) }) : null].filter(Boolean));
  e.tools.setAttribute("aria-label", allowed.size ? `Tools: ${allowed.size} allowed without asking` : "Tools");
  e.tools.title = "Tools that can run without asking you, when you talk to it";
  const muted = Boolean(page.muted);
  e.bell.dataset.on = String(!muted);
  e.bell.replaceChildren(icon(muted ? "bell_off" : "bell"));
  e.bell.title = muted ? "This agent doesn't notify you: turn its notifications on" : "This agent notifies you: mute it";
  e.bell.setAttribute("aria-label", "Notifications for this agent");
  e.bell.setAttribute("aria-pressed", String(!muted));
}

function renderViews(id) {
  const s = summary(id);
  agents_.els.views.replaceChildren(
    ...VIEWS.map(([view, label]) => {
      const badge = view === "dag" && s && s.proposals ? h("span.view-count", { text: String(s.proposals), title: "Proposals to approve" }) : null;
      const live = view === "runs" && s && s.dag && s.dag.running ? h("span.view-live", { title: "Running now" }) : null;
      return h("a.agent-view-link", { href: address(id, view), "aria-current": agents_.view === view ? "page" : null }, label, badge, live);
    })
  );
}

function mountView(id) {
  const box = agents_.els.view;
  const key = `${agents_.view}/${id}`;
  const fresh = box.dataset.key !== key;
  box.dataset.key = key;
  if (agents_.view === "chat") {
    const el = chatView();
    if (box.firstChild !== el) box.replaceChildren(el);
    renderChat(fresh);
  } else if (agents_.view === "dag") {
    dagView(box, id, fresh);
  } else if (agents_.view === "runs") {
    runsView(box, id, agents_.run, fresh);
  } else {
    textView(box, id, agents_.view, fresh);
  }
}

// -- its context and guidance ----------------------------------------------------------------------------------

const TEXTS = {
  context: {
    title: "What its last run kept",
    help: "What its runs last told you, which the next run starts with, with what you've said in its chat since. A run with nothing new leaves it as it is. You can change it here, or tell the agent in its chat.",
    empty: "Nothing yet: its first run keeps what's worth keeping.",
    placeholder: "The CV, what's been applied for already…",
  },
  guidance: {
    title: "Guidance",
    help: "How Athena acts in this agent, at a high level: what it's for, what matters and what to avoid. It's at the start of this agent's chat and each run of its DAG.",
    empty: "",
    placeholder: "You find me data engineering jobs in London or remote. I'd rather not work in finance. Keep messages short.",
  },
};
const FILE_TYPES = ".pdf,.md,.markdown,.txt,.text,.csv,.tsv,.json,.html,.xml,.ics,.log,.py"; // what read_file reads
const MAX_FILE_BYTES = 20 * 1024 * 1024; // as the server takes

function textView(box, id, view, fresh) {
  const page = agents_.pages.get(id) || {};
  const saved = page[view] || "";
  const draftKey = `${id}/${view}`;
  if (fresh || !box.querySelector("textarea")) {
    const text = TEXTS[view];
    const area = h("textarea.text-view", { rows: 14, placeholder: text.placeholder, "aria-label": text.title, maxlength: view === "guidance" ? 8000 : 12000 });
    area.value = agents_.drafts.has(draftKey) ? agents_.drafts.get(draftKey) : saved;
    const save = h("button.button.primary", { type: "submit", text: "Save" });
    const undo = h("button.button", { type: "button", text: "Undo changes" });
    const status = h("span.hint.text-status");
    const changed = () => {
      const dirty = area.value !== (agents_.pages.get(id) || {})[view];
      if (dirty) agents_.drafts.set(draftKey, area.value);
      else agents_.drafts.delete(draftKey);
      save.disabled = !dirty;
      undo.hidden = !dirty;
    };
    area.addEventListener("input", changed);
    undo.addEventListener("click", () => {
      agents_.drafts.delete(draftKey);
      area.value = (agents_.pages.get(id) || {})[view] || "";
      changed();
    });
    const form = h(
      "form.card.text-card",
      {
        onsubmit: async (event) => {
          event.preventDefault();
          save.disabled = true;
          try {
            const page = await call("POST", `/api/agents/${id}`, { [view]: area.value });
            agents_.pages.set(id, { ...(agents_.pages.get(id) || {}), ...page });
            agents_.drafts.delete(draftKey);
            area.value = page[view];
            changed();
            toast("Saved.");
          } catch (err) {
            toast(err.message, true);
            save.disabled = false;
          }
        },
      },
      h("div.card-head", {}, h("h2", { text: text.title }), status),
      h("p.hint", { text: text.help }),
      area,
      h("div.tool-actions", {}, undo, save)
    );
    box.replaceChildren(view === "guidance" ? h("div.stack", {}, form, filesCard(id)) : form);
    changed();
  }
  const area = box.querySelector("textarea");
  if (!agents_.drafts.has(draftKey) && area.value !== saved && document.activeElement !== area) area.value = saved;
  const status = box.querySelector(".text-status");
  if (view === "context") status.textContent = page.context_at ? `Kept ${when(page.context_at)}` : TEXTS.context.empty;
}

// The files that come with its guidance, such as your CV: its chat and each run of its DAG are told they're there,
// and read them with read_file when they'd help.
function filesCard(id) {
  const list = h("ul.file-list", { "aria-label": "Its files" });
  const hint = h("p.hint", { text: "Your CV, notes, or anything else it should know: PDFs and text files. Its chat and each run of its DAG are told they're here, and read them when they'd help." });
  const input = h("input", { type: "file", multiple: true, hidden: true, accept: FILE_TYPES, onchange: (event) => upload([...event.target.files]).finally(() => (event.target.value = "")) });
  const add = h("button.button.small", { type: "button", onclick: () => input.click() }, icon("clip"), "Add files");
  const card = h(
    "section.card.files-card",
    {
      ondragover: (event) => {
        event.preventDefault();
        card.classList.add("dropping");
      },
      ondragleave: () => card.classList.remove("dropping"),
      ondrop: (event) => {
        event.preventDefault();
        card.classList.remove("dropping");
        upload([...((event.dataTransfer && event.dataTransfer.files) || [])]);
      },
    },
    h("div.card-head", {}, h("h2", { text: "Files" }), h("div.actions", {}, add)),
    hint,
    list,
    input
  );
  const show = (data) => {
    if (!data.on) {
      hint.textContent = "Files are switched off: switch them on in the Tools tab to give an agent files.";
      add.disabled = true;
    }
    list.replaceChildren(
      ...data.files.map((f) =>
        h(
          "li.file-row",
          {},
          icon("file"),
          h("a.file-name", { href: `/api/agents/${id}/files/${encodeURIComponent(f.name)}`, download: f.name, title: `Download ${f.name}`, text: f.name }),
          h("span.hint", { text: bytes(f.size) }),
          h("button.button.quiet.small.file-remove", { type: "button", "aria-label": `Delete ${f.name}`, title: "Delete it", onclick: (event) => remove(event, f.name) }, icon("close"))
        )
      )
    );
    list.hidden = !data.files.length;
  };
  const upload = async (files) => {
    if (!files.length) return;
    const tooBig = files.find((f) => f.size > MAX_FILE_BYTES);
    if (tooBig) return toast(`${tooBig.name} is over 20 MB, more than Athena can take.`, true);
    add.disabled = true;
    try {
      const sent = await Promise.all(files.map(async (f) => ({ name: f.name, data: await base64(f) })));
      show({ on: true, ...(await call("POST", `/api/agents/${id}/files`, { files: sent })) });
      toast(files.length === 1 ? `Added ${files[0].name}.` : `Added ${plural(files.length, "file")}.`);
    } catch (err) {
      toast(err.status === 413 ? "That's more than Athena can take at once: add fewer, or smaller, files." : err.message, true);
    } finally {
      add.disabled = false;
    }
  };
  const remove = async (event, name) => {
    event.currentTarget.disabled = true;
    try {
      show({ on: true, ...(await call("DELETE", `/api/agents/${id}/files/${encodeURIComponent(name)}`)) });
    } catch (err) {
      toast(err.message, true);
    }
  };
  call("GET", `/api/agents/${id}/files`).then(show, (err) => toast(err.message, true));
  return card;
}

// -- what you do -----------------------------------------------------------------------------------------------

async function newAgent() {
  const name = h("input", { type: "text", maxLength: 80, placeholder: "Job hunting", "aria-label": "Its name" });
  const ok = await ask({
    title: "A new agent",
    body: [
      h("p", { text: "Something you'd like Athena to do for you. Give it guidance, and a DAG to run by itself, once it's made. To start from what the master agent finds, give its DAG the same trigger as an outlet of the master's." }),
      h("label.field", {}, h("span.field-label", { text: "Name" }), name),
    ],
    confirm: "Make it",
  });
  if (!ok) return;
  try {
    agents_.data = await call("POST", "/api/agents", { name: name.value });
    location.hash = address(agents_.data.agent);
  } catch (err) {
    toast(err.message, true);
  }
}

async function toggleMuted() {
  const id = agents_.id;
  const page = agents_.pages.get(id);
  if (!page) return;
  try {
    const changed = await call("POST", `/api/agents/${id}`, { muted: !page.muted });
    agents_.pages.set(id, { ...page, ...changed });
    const s = summary(id);
    if (s) s.muted = changed.muted;
    render();
    toast(changed.muted ? "This agent won't notify you. Requests for your OK still do." : "This agent notifies you again.");
  } catch (err) {
    toast(err.message, true);
  }
}

// Name it, choose its model, or delete it.
async function aboutAgent() {
  const id = agents_.id;
  const s = summary(id);
  const page = agents_.pages.get(id) || { name: "", model: "" };
  const dialog = document.getElementById("dialog");
  const name = h("input", { type: "text", maxLength: 80, value: page.name || "", placeholder: (s && s.title) || "Job hunting", "aria-label": "Name" });
  const model = h("select", { "aria-label": "Model" }, h("option", { value: "", text: "The default" }));
  const error = h("p.form-error", { role: "alert" });
  const save = h("button.button.primary", { type: "submit", text: "Save" });
  const form = h(
    "form.task-form",
    {
      onsubmit: async (event) => {
        event.preventDefault();
        save.disabled = true;
        try {
          const changed = await call("POST", `/api/agents/${id}`, { name: name.value, model: model.value });
          agents_.pages.set(id, { ...page, ...changed });
          dialog.close();
          await loadAgents();
        } catch (err) {
          error.textContent = err.message;
          save.disabled = false;
        }
      },
    },
    h("h2", { text: isMaster(id) ? "The master agent" : "This agent" }),
    h("label.field", {}, h("span.field-label", { text: "Name" }), h("span.field-help", { text: isMaster(id) ? "What you call it. It's always here: Telegram, Siri and the terminal talk to it." : "What it does for you. Without a name, it's called after your first message." }), name),
    h("label.field", {}, h("span.field-label", { text: "Model" }), h("span.field-help", { text: "Which of the model server's models it uses, for its chat and its DAG." }), model),
    error,
    h(
      "div.tool-actions",
      {},
      isMaster(id) ? null : h("button.button.danger", { type: "button", text: "Delete agent", onclick: () => deleteAgent(id) }),
      h("span.grow"),
      h("button.button", { type: "button", text: "Cancel", onclick: () => dialog.close() }),
      save
    )
  );
  dialog.onclose = null;
  dialog.replaceChildren(form);
  if (!dialog.open) dialog.showModal();
  name.focus();
  try {
    const listed = await call("GET", "/api/models");
    model.firstChild.textContent = `The default (${listed.default})`;
    for (const m of listed.models) if (m !== listed.default) model.append(h("option", { value: m, text: m }));
    if (page.model && !listed.models.includes(page.model)) model.append(h("option", { value: page.model, text: `${page.model} (not on the server now)` }));
    model.value = page.model || "";
  } catch {
    // only the default, then
  }
}

async function deleteAgent(id) {
  const s = summary(id);
  const ok = await ask({
    title: `Delete “${(s && s.title) || "this agent"}”?`,
    body: [
      h("p", { text: "Everything said to it is deleted, here and from Athena's memory, and so are its DAG, the history of its runs and its files. This can't be undone." }),
    ],
    confirm: "Delete",
    danger: true,
  });
  if (!ok) return;
  try {
    agents_.data = await call("DELETE", `/api/agents/${id}`);
    agents_.pages.delete(id);
    toast("Deleted.");
    location.hash = address(agents_.data.master);
  } catch (err) {
    toast(err.message, true);
  }
}

// -- small things ------------------------------------------------------------------------------------------------

export function when(at) {
  if (!at) return "";
  if (isoDay(at) === isoDay(now())) return clock(at, { hour: "2-digit", minute: "2-digit" });
  if (isoDay(at) === isoDay(now() - 86400)) return "yesterday";
  return clock(at, { day: "numeric", month: "short" });
}
