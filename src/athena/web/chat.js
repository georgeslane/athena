// The Chat tab: talk to Athena in any of your sessions. Each session is something Athena does for you, and the
// conversation about it, which Telegram, Siri and the terminal share; the current one is the one used last, and
// sending in an earlier one carries it on, as the current session. Each can have tasks, which run in it and say
// what they found there. One message at a time: while Athena's answering you, wherever you asked, nothing more can
// be sent. Requests for your OK, from here or anywhere, can be answered here. Messages can come with photos and
// files, and each session can be named, muted and deleted.

import { ask, call, clock, h, icon, isoDay, now, plural, state, toast } from "./core.js";
import { markdown } from "./md.js";
import { canTurnOn, turnOnHere } from "./notifications.js";
import { fetchTasks, taskForm } from "./tasks.js";

const MAX_CHARS = 4000; // as chat.py takes
const MAX_SIDE = 2048; // a photo's longest side, as it's sent: larger ones are made smaller first
const MAX_FILE_BYTES = 20 * 1024 * 1024; // as chat.py takes
const TASK_RAN = /^\(Your task “(.+?)”\)\s*([\s\S]*)$/; // how a task's run starts (history.task_said)
const PHOTOS = /^\[(?:Photo|\d+ photos)\]\s*/; // how a message notes its photos (photos.label)
const FILE = /^\[File: ([^\]]+)\]\s*/; // how a message notes a file sent with it (files.receive)

const chat_ = {
  data: null, // GET /api/chat: the sessions, whether Athena's answering, what's waiting for your OK
  viewing: null, // the session shown: its id, or null until the current one's known
  sessions: new Map(), // id -> {messages, more, allowed, name, muted, tasks}: what's been loaded of each
  notes: new Map(), // id -> [{command, said}]: what commands said, here, until the page is reloaded
  once: new Set(), // tools allowed without asking for the next message only
  attachments: [], // photos and files to send with the next message: {name, kind, data, preview}
  reading: 0, // photos and files still being read, before the message can be sent
  sending: false,
  loading: null, // the load under way, so updates don't pile up
  again: false, // another update came while loading
  suggestion: -1, // the command highlighted in the suggestions
  menu: false, // the suggestions are every command, from the button for them
  timer: null, // counting down requests for your OK
  list: false, // on a phone: the list of sessions rather than a conversation
  scrollDown: false, // to the newest message, when it's next drawn
  last: null, // the newest message in any session, as the status said last
  seen: readSeen(), // id -> the newest message you've seen in each session, for what's new
  els: null, // the tab's parts, made once so what you're typing stays
};

const viewed = () => chat_.viewing;
const isCurrent = (id) => chat_.data && id === chat_.data.current;
const touch = () => window.matchMedia("(hover: none)").matches;
const summary = (id) => (chat_.data ? chat_.data.sessions.find((s) => s.id === id) : null);

// -- loading -----------------------------------------------------------------------------------------------

export async function loadChat() {
  route();
  if (chat_.loading) {
    chat_.again = true;
    return chat_.loading;
  }
  chat_.loading = (async () => {
    do {
      chat_.again = false;
      chat_.data = await call("GET", "/api/chat");
      if (chat_.viewing === null && !chat_.list) {
        // #chat is the current session, shown at its own address, so it stays on screen if another becomes current.
        chat_.viewing = chat_.data.current;
        history.replaceState(null, "", `#chat/${chat_.viewing}`);
      }
      const id = viewed();
      if (id !== null) await loadSession(id);
      render();
    } while (chat_.again);
  })().finally(() => (chat_.loading = null));
  return chat_.loading;
}

// What the address says to show: #chat for the current session, #chat/<id> for one, and #chat/all for the list
// of them, which a phone shows instead of a conversation.
function route() {
  const [, which] = location.hash.split("/");
  chat_.list = which === "all";
  if (which && /^\d+$/.test(which)) chat_.viewing = Number(which);
  else if (!chat_.list) chat_.viewing = null;
}

async function loadSession(id) {
  let page;
  try {
    page = await call("GET", `/api/chat/${id}`);
  } catch (e) {
    if (e.status !== 404) throw e;
    chat_.sessions.delete(id);
    chat_.viewing = chat_.data.current; // it's gone, as after deleting it or forgetting everything
    history.replaceState(null, "", `#chat/${chat_.viewing}`);
    return loadSession(chat_.viewing);
  }
  // The newest page, and what was loaded before it, if there's more than a page.
  const known = chat_.sessions.get(id);
  const first = page.messages.length ? page.messages[0].id : Infinity;
  const earlier = known && page.more ? known.messages.filter((m) => m.id < first) : [];
  chat_.sessions.set(id, {
    messages: [...earlier, ...page.messages],
    more: earlier.length ? known.more : page.more,
    allowed: page.allowed_tools,
    name: page.name,
    muted: page.muted,
    tasks: page.tasks,
  });
}

async function showEarlier(event) {
  const id = viewed();
  const known = chat_.sessions.get(id);
  event.target.disabled = true;
  const log = chat_.els.log;
  const fromBottom = log.scrollHeight - log.scrollTop;
  try {
    const page = await call("GET", `/api/chat/${id}?before=${known.messages[0].id}`);
    known.messages = [...page.messages, ...known.messages];
    known.more = page.more;
    render();
    log.scrollTop = log.scrollHeight - fromBottom; // where you were
  } catch (e) {
    toast(e.message, true);
    event.target.disabled = false;
  }
}

// Athena's status has changed, or something in a session: the Chat tab's count, and the tab itself.
export function followChat(status) {
  // Athena's written, while you're on another tab.
  const last = status.chat && status.chat.last;
  if (last && chat_.last && last.id > chat_.last.id && last.role === "assistant" && !location.hash.startsWith("#chat")) {
    toast(`${state.overview ? state.overview.assistant.name : "Athena"}'s written to you. It's in Chat.`);
  }
  if (status.chat) chat_.last = last || { id: 0 };
  const count = status.chat ? status.chat.approvals : 0;
  const link = document.querySelector('.tab[data-tab="chat"]');
  let badge = link && link.querySelector(".tab-count");
  if (link && !count && badge) badge.remove();
  if (link && count) {
    if (!badge) link.append((badge = h("span.tab-count")));
    badge.textContent = String(count);
    badge.title = `${plural(count, "request")} for your OK`;
  }
  if (location.hash.startsWith("#chat")) loadChat().catch(() => {});
}

// What's new in each session: the newest message you've seen in each, kept on this device.
function readSeen() {
  try {
    const seen = JSON.parse(localStorage.getItem("athena.seen") || "null");
    return seen && typeof seen === "object" ? seen : null;
  } catch {
    return null;
  }
}

function markSeen(id) {
  if (!chat_.seen) chat_.seen = Object.fromEntries(chat_.data.sessions.map((s) => [s.id, s.last])); // at first, all of it
  const s = summary(id);
  if (s && (chat_.seen[id] || 0) < s.last) chat_.seen[id] = s.last;
  try {
    localStorage.setItem("athena.seen", JSON.stringify(chat_.seen));
  } catch {
    // private browsing: what's new is only known until the page is reloaded
  }
}

const unread = (s) => Boolean(chat_.seen) && s.last > (chat_.seen[s.id] || 0);

// -- drawing it -----------------------------------------------------------------------------------------

function build() {
  const e = (chat_.els = {});
  e.list = h("ol.session-list");
  e.newChat = h("button.button.small.primary", { type: "button", onclick: newChat }, icon("plus"), "New session");
  e.sidebar = h("aside.chat-sessions", { "aria-label": "Your sessions" }, h("div.chat-sessions-head", {}, h("h2", { text: "Sessions" }), e.newChat), e.list);
  e.back = h("a.button.quiet.small.chat-back", { href: "#chat/all", "aria-label": "Your sessions" }, icon("back"), h("span.wide-only", { text: "Sessions" }));
  e.title = h("h2.chat-title");
  e.sub = h("p.chat-sub");
  e.bell = h("button.button.small.bell", { type: "button", "data-session": "", onclick: toggleMuted });
  e.tools = h("button.button.small.chat-tools", { type: "button", onclick: chooseTools });
  e.more = h("button.button.small.chat-more", { type: "button", "aria-label": "About this session", title: "Name or delete this session", onclick: aboutSession }, icon("more"));
  e.head = h("header.chat-head", {}, e.back, h("div.chat-heading", {}, e.title, e.sub), e.bell, e.tools, e.more);
  e.banner = h("div.chat-banner", { hidden: true });
  e.log = h("ol.chat-log", { role: "log", "aria-live": "polite", "aria-label": "Conversation" });
  e.text = h("textarea", {
    rows: 1,
    maxlength: MAX_CHARS,
    placeholder: "Message Athena",
    "aria-label": "Message",
    enterkeyhint: "send",
    oninput: typed,
    onkeydown: keys,
    onpaste: pasted,
  });
  e.send = h("button.button.primary.chat-send", { type: "button", "aria-label": "Send", onclick: sendOrStop }, icon("send"));
  e.file = h("input", { type: "file", multiple: true, hidden: true, onchange: (event) => attach([...event.target.files]).finally(() => (event.target.value = "")) });
  e.attach = h("button.button.quiet.chat-icon", { type: "button", "aria-label": "Add photos or files", title: "Add photos or files", onclick: () => e.file.click() }, icon("clip"));
  e.commands = h("button.button.quiet.chat-icon", { type: "button", "aria-label": "Commands", title: "Commands", "aria-haspopup": "listbox", "aria-expanded": "false", onclick: commandMenu }, icon("slash"));
  e.suggest = h("ul.chat-suggest", { role: "listbox", "aria-label": "Commands", hidden: true });
  e.notice = h("p.chat-notice");
  e.allowed = h("div.chat-allowed");
  e.attached = h("ul.chat-attached", { "aria-label": "To send with your message" });
  e.form = h("form.composer", { onsubmit: send }, e.suggest, e.notice, e.allowed, e.attached, h("div.composer-row", {}, e.attach, e.commands, e.text, e.send), e.file);
  e.main = h("section.chat-main", { ondragover: dragging, ondrop: dropped }, e.head, e.banner, e.log, e.form);
  e.root = h("div.chat", {}, e.sidebar, e.main);
  document.getElementById("tab-chat").replaceChildren(e.root);
  document.addEventListener("athena-notifications", () => chat_.data && renderBanner());
  // The list of commands closes when you tap or click anywhere else.
  document.addEventListener("click", (event) => {
    if (chat_.menu && !e.suggest.contains(event.target) && !e.commands.contains(event.target)) closeMenu();
  });
}

function render() {
  if (!chat_.data) return;
  if (!chat_.els) build();
  const e = chat_.els;
  const id = viewed();
  e.root.dataset.view = chat_.list ? "list" : "conversation";
  if (id !== null && !chat_.list) markSeen(id);
  renderSessions(id);
  renderHead(id);
  renderBanner();
  renderLog(id);
  renderComposer(id);
  countDown();
}

function renderSessions(id) {
  const asking = new Set(chat_.data.approvals.map((a) => a.session ?? chat_.data.current));
  const items = chat_.data.sessions.map((s) => {
    const meta = [];
    if (isCurrent(s.id)) meta.push(h("span.current", { text: "Current" }));
    meta.push(when(s.last_used));
    if (s.tasks) meta.push(plural(s.tasks, "task"));
    else if (s.messages) meta.push(plural(Math.ceil(s.messages / 2), "message"));
    return h(
      "li",
      {},
      h(
        "a.session",
        { href: `#chat/${s.id}`, "aria-current": s.id === id ? "true" : null, "data-unread": String(unread(s) && s.id !== id) },
        h(
          "span.session-line",
          {},
          h("span.session-title", { text: s.title || "New session" }),
          asking.has(s.id) ? h("span.session-flag.asking", { text: "Needs your OK" }) : null,
          s.muted ? h("span.session-flag", { title: "Muted: it doesn't notify you", "aria-label": "Muted" }, icon("bell_off")) : null,
          h("span.unread-dot", { title: "Something new" })
        ),
        h("span.session-meta", {}, meta.flatMap((part, i) => (i ? [" · ", part] : [part])))
      )
    );
  });
  e().list.replaceChildren(...items);
  e().newChat.disabled = chat_.data.busy;
}

function renderHead(id) {
  const s = summary(id);
  const known = chat_.sessions.get(id) || { tasks: [], muted: false, name: "" };
  e().title.textContent = (s && s.title) || known.name || "New session";
  const where = isCurrent(id) ? `Current session${s && s.messages ? ` · since ${when(s.started)}` : ""}` : `Earlier session · last used ${when(s ? s.last_used : 0)}`;
  const tasks = known.tasks.length;
  // Its tasks first, so a narrow screen cuts short the rest.
  e().sub.replaceChildren(h("button.linkish", { type: "button", text: tasks ? plural(tasks, "task") : "Add a task", onclick: () => sessionTasks(id) }), " · ", where);
  const allowed = allowedHere(id);
  const label = h("span.wide-only", { text: allowed.length ? `${allowed.length} allowed` : "Tools" });
  e().tools.replaceChildren(...[icon("shield"), label, allowed.length ? h("span.tools-count", { text: String(allowed.length) }) : null].filter(Boolean));
  e().tools.setAttribute("aria-label", allowed.length ? `Tools: ${allowed.length} allowed without asking` : "Tools");
  e().tools.title = "Tools that can run without asking you";
  const muted = Boolean(known.muted);
  e().bell.dataset.on = String(!muted);
  e().bell.replaceChildren(icon(muted ? "bell_off" : "bell"));
  e().bell.title = muted ? "This session doesn't notify you: turn its notifications on" : "This session notifies you: mute it";
  e().bell.setAttribute("aria-label", "Notifications for this session");
  e().bell.setAttribute("aria-pressed", String(!muted));
}

// An offer to notify this device, until it does, or you'd rather it didn't.
function renderBanner() {
  let dismissed = false;
  try {
    dismissed = localStorage.getItem("athena.notify-offer") === "no";
  } catch {
    // as if it weren't
  }
  const show = canTurnOn() && !dismissed;
  e().banner.hidden = !show;
  if (!show) return;
  const on = h("button.button.small.primary", { type: "button", text: "Turn on", onclick: () => turnOnHere(on) });
  const no = () => {
    try {
      localStorage.setItem("athena.notify-offer", "no");
    } catch {
      // it's offered again next time
    }
    renderBanner();
  };
  e().banner.replaceChildren(
    icon("bell"),
    h("span", { text: "Hear when Athena writes to you, on this device too." }),
    on,
    h("button.button.quiet.small.chat-icon", { type: "button", "aria-label": "Not on this device", title: "Not on this device", onclick: no }, icon("close"))
  );
}

function renderLog(id) {
  const log = e().log;
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  const known = chat_.sessions.get(id) || { messages: [], more: false };
  const d = chat_.data;
  const items = [];
  if (known.more) items.push(h("li.chat-earlier", {}, h("button.button.small", { type: "button", text: "Show earlier", onclick: showEarlier })));
  let day = "";
  for (const m of known.messages) {
    const at = Date.parse(m.at) / 1000;
    if (isoDay(at) !== day) {
      day = isoDay(at);
      items.push(h("li.day-sep", {}, h("span", { text: dayName(at) })));
    }
    items.push(message(m));
  }
  for (const note of chat_.notes.get(id) || []) items.push(h("li.msg.mine.command", {}, h("div.msg-text", { text: note.command })), h("li.msg.note", {}, h("div.msg-text.md", {}, markdown(note.said))));
  const failed = d.failed[String(id)];
  if (failed) items.push(failedMessage(failed));
  const replying = d.replying && d.replying.session === id ? d.replying : null;
  if (replying && !kept(known, replying)) items.push(h("li.msg.mine.pending", {}, ...yours(replying.text, replying.photos), h("span.msg-meta", { text: "Sent" })));
  const elsewhere = d.elsewhere && d.elsewhere.session === id ? d.elsewhere : null;
  if (elsewhere) items.push(h("li.msg-notice", {}, h("span", { text: `Answering your message from ${elsewhere.channel || "elsewhere"}${elsewhere.text ? `: “${elsewhere.text}”` : ""}` })));
  for (const a of d.approvals.filter((a) => a.session === id || (a.session === null && isCurrent(id)))) items.push(approvalCard(a));
  if (replying || elsewhere) items.push(h("li.msg.theirs.typing", { "aria-label": "Athena is answering" }, h("span.dots", {}, h("i"), h("i"), h("i")), h("span.typing-step", { text: d.step && d.step !== "Thinking" ? d.step : "Thinking" })));
  if (!items.length) {
    items.push(
      h(
        "li.chat-empty",
        {},
        h("p", { text: "Nothing said here yet." }),
        h("p.hint", { text: "A session is something Athena does for you, such as finding you a job. Tell it what you're after, give it a task to do here by itself, or type / for commands." })
      )
    );
  }
  log.replaceChildren(...items);
  if (atBottom || chat_.scrollDown) log.scrollTop = log.scrollHeight;
  chat_.scrollDown = false;
}

// Whether a message being answered is in the conversation already, as it is just before the answer's done.
const kept = (known, replying) => known.messages.some((m) => m.role === "user" && m.text.endsWith(replying.text) && Date.parse(m.at) / 1000 >= replying.started - 2);

function message(m) {
  if (m.role === "user" && m.channel === "Athena") {
    return h("li.msg-notice", {}, h("span", { text: m.text.replace(/^\(([\s\S]*)\)$/, "$1") })); // "(Claude's projects changed.)"
  }
  const time = clock(Date.parse(m.at) / 1000, { hour: "2-digit", minute: "2-digit" });
  const ran = m.role === "user" && m.channel === "Task" && m.text.match(TASK_RAN);
  if (ran) {
    return h(
      "li.msg-task",
      {},
      h("details", {}, h("summary", {}, icon("clock"), h("span", { text: `Your task “${ran[1]}” ran` }), h("span.msg-meta", { text: time })), h("p", { text: ran[2] }))
    );
  }
  const mine = m.role === "user";
  const meta = [time];
  if (mine && m.channel && m.channel !== "Dashboard") meta.push(`via ${m.channel}`);
  if (!mine && m.tools.length) meta.push(`used ${[...new Set(m.tools)].join(", ")}`);
  return h(`li.msg.${mine ? "mine" : "theirs"}`, {}, ...(mine ? yours(m.text, m.photos) : [h("div.msg-text.md", {}, markdown(m.text))]), h("span.msg-meta", { text: meta.join(" · ") }));
}

// A message of yours: its photos (by name, or how many while it's being answered), the files that came with it,
// and what you wrote.
function yours(text, photos) {
  const parts = [];
  if (Array.isArray(photos) && photos.length) {
    parts.push(h("div.msg-photos", {}, photos.map((name) => h("a", { href: `/api/photos/${name}`, target: "_blank", rel: "noopener" }, h("img", { src: `/api/photos/${name}`, alt: "A photo you sent", loading: "lazy" })))));
  } else if (typeof photos === "number" && photos) {
    parts.push(h("div.msg-files", {}, h("span.chip.file-chip", {}, icon("clip"), plural(photos, "photo"))));
  }
  text = text.replace(PHOTOS, "");
  const files = [];
  for (let file = text.match(FILE); file; file = text.match(FILE)) {
    files.push(file[1]);
    text = text.slice(file[0].length);
  }
  if (files.length) parts.push(h("div.msg-files", {}, files.map((path) => h("span.chip.file-chip", { title: path }, icon("file"), path.split("/").pop()))));
  if (text.trim()) parts.push(h("div.msg-text", {}, lines(text)));
  return parts;
}

function failedMessage(failed) {
  return h(
    "li.msg.mine.failed",
    {},
    ...yours(failed.text, failed.photos),
    h(
      "div.msg-failed",
      {},
      h("p", { text: `Didn't get an answer: ${failed.error}` }),
      h(
        "div.msg-actions",
        {},
        failed.photos ? null : h("button.button.small", { type: "button", text: "Edit", onclick: () => edit(failed.text) }),
        h("button.button.small.primary", { type: "button", text: "Try again", disabled: chat_.data.busy, onclick: () => send(null, "/retry") })
      )
    )
  );
}

function approvalCard(a) {
  return h(
    "li.approval",
    {},
    h("p.approval-title", {}, a.why ? `${a.why}: allow ` : "Allow ", h("code", { text: a.tool }), "?"),
    a.summary ? h("p.approval-summary", { text: a.summary }) : null,
    Object.keys(a.args).length ? h("pre.approval-args", { text: JSON.stringify(a.args, null, 2) }) : null,
    h(
      "div.approval-foot",
      {},
      h("span.hint.approval-time", { "data-deadline": a.deadline }),
      h(
        "div.msg-actions",
        {},
        h("button.button.small", { type: "button", text: "Deny", onclick: (event) => answer(event, a, false) }),
        h("button.button.small.primary", { type: "button", text: "Allow", onclick: (event) => answer(event, a, true) })
      )
    )
  );
}

// How long each request for your OK has left, every second while there are any.
function countDown() {
  clearInterval(chat_.timer);
  const update = () => {
    const left = [...document.querySelectorAll(".approval-time")];
    for (const el of left) {
      const s = Math.max(0, Math.round(Number(el.dataset.deadline) - now()));
      el.textContent = `Denied in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} without an answer`;
    }
    if (!left.length) clearInterval(chat_.timer);
  };
  update();
  chat_.timer = setInterval(update, 1000);
}

function renderComposer(id) {
  const d = chat_.data;
  const stopping = Boolean(d.replying);
  let notice = "";
  if (d.elsewhere) notice = `${state.overview ? state.overview.assistant.name : "Athena"}'s answering your message from ${d.elsewhere.channel || "elsewhere"}. You can send once it's finished.`;
  else if (d.busy && !isCurrent(id)) notice = "Athena's answering in your current session. You can carry this one on once it's finished.";
  else if (!isCurrent(id)) notice = "An earlier session: sending a message carries it on, as your current session.";
  e().notice.textContent = notice;
  e().notice.hidden = !notice;
  const once = [...chat_.once];
  e().allowed.replaceChildren(...(once.length ? [h("span.hint", { text: "Next message only:" }), ...once.map((t) => h("span.chip", { text: t }))] : []));
  e().allowed.hidden = !once.length;
  e().attached.replaceChildren(
    ...chat_.attachments.map((a, i) =>
      h(
        "li.attached",
        {},
        a.kind === "photo" ? h("img", { src: a.preview, alt: "" }) : icon("file"),
        h("span.attached-name", { text: a.name }),
        h("button.attached-remove", { type: "button", "aria-label": `Don't send ${a.name}`, title: "Don't send it", onclick: () => unattach(i) }, icon("close"))
      )
    )
  );
  e().attached.hidden = !chat_.attachments.length;
  e().attach.disabled = !d.attach.photos && !d.attach.files;
  e().file.accept = d.attach.files ? "image/*,.pdf,.md,.markdown,.txt,.text,.csv,.tsv,.json,.html,.xml,.ics,.log,.py" : "image/*";
  // While Athena answers a message sent here, the button stops it.
  e().send.replaceChildren(icon(stopping ? "stop" : "send"));
  e().send.setAttribute("aria-label", stopping ? "Stop" : "Send");
  e().send.classList.toggle("stop", stopping);
  const something = e().text.value.trim() || chat_.attachments.length;
  e().send.disabled = chat_.sending || (!stopping && (d.busy || chat_.reading > 0 || !something));
}

const e = () => chat_.els;

// -- what you do --------------------------------------------------------------------------------------------

// The button sends, or while Athena's answering a message sent here, stops it. Enter only ever sends.
function sendOrStop() {
  if (chat_.data && chat_.data.replying) stop();
  else e().form.requestSubmit();
}

async function send(event, text) {
  if (event) event.preventDefault();
  text = (text ?? e().text.value).trim();
  const id = viewed();
  const command = text.startsWith("/");
  const attachments = command ? [] : chat_.attachments;
  if ((!text && !attachments.length) || chat_.sending || chat_.reading || id === null || (chat_.data.busy && !command)) return;
  if (command && event && chat_.attachments.length) return toast("Send a command by itself, without photos or files.", true);
  chat_.sending = true;
  closeMenu();
  renderComposer(id);
  try {
    const body = { text, allow: command ? [] : [...chat_.once], attachments: attachments.map((a) => ({ name: a.name, data: a.data })) };
    const said = await call("POST", `/api/chat/${id}`, body);
    chat_.data = said;
    if (event) {
      e().text.value = "";
      typed();
    }
    if (said.said) chat_.notes.set(id, [...(chat_.notes.get(id) || []), { command: text, said: said.said }]);
    if (!command) {
      chat_.once.clear();
      chat_.attachments = [];
    }
    if (/^\/(session|reset)\b/.test(text) && said.current !== id) location.hash = `#chat/${said.current}`; // the new one
    chat_.scrollDown = true;
    await loadChat();
  } catch (err) {
    toast(err.status === 413 ? "That's more than Athena can take at once: send fewer, or smaller, files." : err.message, true);
  } finally {
    chat_.sending = false;
    renderComposer(viewed());
  }
}

async function stop() {
  try {
    await call("POST", "/api/chat/stop", {});
    await loadChat();
  } catch (err) {
    toast(err.message, true);
  }
}

async function answer(event, approval, allow) {
  for (const button of event.target.parentElement.querySelectorAll("button")) button.disabled = true;
  try {
    chat_.data = await call("POST", `/api/approvals/${approval.id}`, { allow });
    render();
  } catch (err) {
    toast(err.message, true);
    loadChat().catch(() => {});
  }
}

async function newChat() {
  try {
    chat_.data = await call("POST", "/api/chat", {});
    chat_.scrollDown = true;
    const to = `#chat/${chat_.data.session}`;
    if (location.hash !== to) location.hash = to;
    else await loadChat();
    e().text.focus();
  } catch (err) {
    toast(err.message, true);
  }
}

function edit(text) {
  e().text.value = text;
  typed();
  e().text.focus();
}

async function toggleMuted() {
  const id = viewed();
  const known = chat_.sessions.get(id);
  if (!known) return;
  try {
    const page = await call("POST", `/api/chat/${id}/mute`, { muted: !known.muted });
    known.muted = page.muted;
    const s = summary(id);
    if (s) s.muted = page.muted;
    render();
    toast(page.muted ? "This session won't notify you. Requests for your OK still do." : "This session notifies you again.");
  } catch (err) {
    toast(err.message, true);
  }
}

// Name the session, or delete it.
function aboutSession() {
  const id = viewed();
  const s = summary(id);
  const known = chat_.sessions.get(id) || { name: "", tasks: [] };
  const dialog = document.getElementById("dialog");
  const name = h("input", { type: "text", maxLength: 80, value: known.name || "", placeholder: (s && s.title) || "Finding me a job", "aria-label": "Name" });
  const error = h("p.form-error", { role: "alert" });
  const save = h("button.button.primary", { type: "submit", text: "Save" });
  const form = h(
    "form.task-form",
    {
      onsubmit: async (event) => {
        event.preventDefault();
        save.disabled = true;
        try {
          const page = await call("POST", `/api/chat/${id}/name`, { name: name.value });
          known.name = page.name;
          dialog.close();
          await loadChat();
        } catch (err) {
          error.textContent = err.message;
          save.disabled = false;
        }
      },
    },
    h("h2", { text: "This session" }),
    h("label.field", {}, h("span.field-label", { text: "Name" }), h("span.field-help", { text: "What Athena's doing for you here. Without a name, it's called after your first message." }), name),
    error,
    h("div.tool-actions", {}, h("button.button.danger", { type: "button", text: "Delete session", onclick: () => deleteSession(id) }), h("span.grow"), h("button.button", { type: "button", text: "Cancel", onclick: () => dialog.close() }), save)
  );
  dialog.onclose = null;
  dialog.replaceChildren(form);
  if (!dialog.open) dialog.showModal();
  name.focus();
}

async function deleteSession(id) {
  const s = summary(id);
  const tasks = (chat_.sessions.get(id) || { tasks: [] }).tasks.length;
  const ok = await ask({
    title: `Delete “${(s && s.title) || "this session"}”?`,
    body: [
      h("p", { text: `Everything said in it is deleted, here and from Athena's memory${tasks ? `, and so ${tasks === 1 ? "is its task" : `are its ${tasks} tasks`}` : ""}. This can't be undone.` }),
      h("p.hint", { text: "Activity keeps its record of what Athena did for up to 30 days." }),
    ],
    confirm: "Delete",
    danger: true,
  });
  if (!ok) return;
  try {
    chat_.data = await call("DELETE", `/api/chat/${id}`);
    chat_.sessions.delete(id);
    chat_.notes.delete(id);
    toast("Deleted.");
    location.hash = `#chat/${chat_.data.current}`;
  } catch (err) {
    toast(err.message, true);
  }
}

// The session's tasks: what each does and when, to run, change or pause, and adding another.
async function sessionTasks(id) {
  const dialog = document.getElementById("dialog");
  const t = await fetchTasks().catch((err) => ({ off: err.message }));
  if (t.off) return toast(t.off, true);
  const reload = () => loadChat().catch(() => {});
  const draw = () => {
    const tasks = state.tasks.tasks.filter((task) => task.session === id);
    const act = async (event, task, action) => {
      event.target.disabled = true;
      try {
        state.tasks = await call("POST", `/api/tasks/${task.id}`, { action });
        toast({ run: `“${task.title}” will run as soon as you're not chatting.`, pause: "Paused.", resume: "Resumed." }[action]);
        draw();
        reload();
      } catch (err) {
        toast(err.message, true);
        event.target.disabled = false;
      }
    };
    const rows = tasks.map((task) => {
      let next = task.status === "paused" ? "Paused" : task.status === "done" ? "Done" : "";
      if (task.status === "running") next = "Working on it now";
      else if (task.next_run) next = `Next ${clock(task.next_run, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`;
      return h(
        "li.session-task",
        {},
        h("div.allow-name", {}, h("strong", { text: task.title }), h("span.hint", { text: [task.when.replace(/^./, (c) => c.toUpperCase()), next].filter(Boolean).join(" · ") })),
        h(
          "div.msg-actions",
          {},
          task.status === "running" ? null : h("button.button.small", { type: "button", text: "Run now", onclick: (event) => act(event, task, "run") }),
          task.status === "active" ? h("button.button.small", { type: "button", text: "Pause", onclick: (event) => act(event, task, "pause") }) : null,
          task.status === "paused" || task.status === "done" ? h("button.button.small", { type: "button", text: "Resume", onclick: (event) => act(event, task, "resume") }) : null,
          task.status === "running" ? null : h("button.button.small", { type: "button", text: "Edit", onclick: () => taskForm({ task, onSaved: reload }) })
        )
      );
    });
    dialog.replaceChildren(
      h("h2", { text: "Tasks in this session" }),
      h("p.hint", { text: "Each works by itself when it's due, with everything said here to go on, and says what it found here, for you to reply to." }),
      rows.length ? h("ul.allow-tools", {}, rows) : h("p.empty", { text: "None yet. Add one, or ask Athena here: “every morning, look for new job postings”." }),
      h("div.tool-actions", {}, h("button.button", { type: "button", text: "Close", onclick: () => dialog.close() }), h("button.button.primary", { type: "button", text: "New task", onclick: () => taskForm({ session: id, onSaved: reload }) }))
    );
  };
  draw();
  dialog.onclose = null;
  if (!dialog.open) dialog.showModal();
}

// The tools that ask first, and whether each may run without asking: for the next message, or the session.
function allowedHere(id) {
  const known = chat_.sessions.get(id);
  return [...new Set([...(known ? known.allowed : []), ...chat_.once])];
}

function chooseTools() {
  const id = viewed();
  const dialog = document.getElementById("dialog");
  const draw = () => {
    const known = chat_.sessions.get(id) || { allowed: [] };
    const askable = chat_.data.askable;
    const rows = askable.map((tool) => {
      const now_ = known.allowed.includes(tool.name) ? "session" : chat_.once.has(tool.name) ? "once" : "ask";
      return h(
        "li.allow-tool",
        {},
        h("div.allow-name", {}, h("code", { text: tool.name }), h("span.hint", { text: tool.description })),
        h(
          "div.switcher",
          { role: "group", "aria-label": `When ${tool.name} may run without asking` },
          [["ask", "Ask"], ["once", "Next message"], ["session", "Session"]].map(([choice, label]) =>
            h("button", { type: "button", text: label, "aria-pressed": String(now_ === choice), onclick: () => choose(tool.name, choice) })
          )
        )
      );
    });
    dialog.replaceChildren(
      h("h2", { text: "Tools that ask first" }),
      h("p.hint", { text: "Let one run without asking you: for your next message only, or for the rest of this session, wherever you're talking to Athena in it. Its tasks choose their own. Some, such as scheduling tasks, always ask." }),
      askable.length ? h("ul.allow-tools", {}, rows) : h("p.empty", { text: "No tools ask first." }),
      h("div.tool-actions", {}, h("button.button.primary", { type: "button", text: "Done", onclick: () => dialog.close() }))
    );
  };
  const choose = async (name, choice) => {
    const known = chat_.sessions.get(id) || { allowed: [] };
    const session = new Set(known.allowed);
    chat_.once.delete(name);
    session.delete(name);
    if (choice === "once") chat_.once.add(name);
    if (choice === "session") session.add(name);
    if (session.size !== known.allowed.length || choice === "session") {
      try {
        const page = await call("POST", `/api/chat/${id}/tools`, { allowed: [...session] });
        known.allowed = page.allowed_tools;
      } catch (err) {
        toast(err.message, true);
      }
    }
    draw();
    render();
  };
  draw();
  dialog.onclose = null;
  dialog.showModal();
}

// -- photos and files ---------------------------------------------------------------------------------------

async function attach(list) {
  chat_.reading += 1;
  renderComposer(viewed());
  try {
    await attachEach(list);
  } finally {
    chat_.reading -= 1;
    renderComposer(viewed());
  }
}

async function attachEach(list) {
  const limits = chat_.data.attach;
  for (const file of list) {
    const photo = file.type.startsWith("image/");
    const have = chat_.attachments.filter((a) => a.kind === (photo ? "photo" : "file")).length;
    let problem = "";
    if (photo && have >= limits.photos) problem = `Send up to ${limits.photos} photos with a message.`;
    else if (!photo && !limits.files) problem = "Files are switched off, so Athena can't read them: switch them on in the Tools tab.";
    else if (!photo && have >= limits.files) problem = `Send up to ${limits.files} files with a message.`;
    else if (!photo && file.size > MAX_FILE_BYTES) problem = `${file.name} is over 20 MB, more than Athena can take.`;
    if (problem) {
      toast(problem, true);
      continue;
    }
    try {
      chat_.attachments.push(photo ? await asPhoto(file) : { name: file.name, kind: "file", data: await base64(file) });
    } catch {
      toast(`Couldn't read ${file.name}${photo ? ": send it as a JPEG or PNG" : ""}.`, true);
    }
  }
}

function unattach(i) {
  chat_.attachments.splice(i, 1);
  renderComposer(viewed());
  e().text.focus();
}

// A photo as it's sent: as it is, if the model can take it and it's not too large; otherwise a JPEG of it, no
// more than MAX_SIDE across, as Telegram sends photos.
async function asPhoto(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const takes = ["image/jpeg", "image/png", "image/webp", "image/gif"].includes(file.type);
  if (takes && scale === 1 && file.size <= 4 * 1024 * 1024) {
    bitmap.close();
    const data = await base64(file);
    return { name: file.name, kind: "photo", data, preview: `data:${file.type};base64,${data}` };
  }
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const url = canvas.toDataURL("image/jpeg", 0.85);
  return { name: `${file.name.replace(/\.[^.]*$/, "") || "photo"}.jpg`, kind: "photo", data: url.slice(url.indexOf(",") + 1), preview: url };
}

function base64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).slice(String(reader.result).indexOf(",") + 1));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function pasted(event) {
  const files = [...((event.clipboardData && event.clipboardData.files) || [])];
  if (!files.length) return;
  event.preventDefault();
  attach(files);
}

function dragging(event) {
  if (event.dataTransfer && [...event.dataTransfer.types].includes("Files")) event.preventDefault();
}

function dropped(event) {
  const files = [...((event.dataTransfer && event.dataTransfer.files) || [])];
  if (!files.length) return;
  event.preventDefault();
  attach(files);
}

// -- the composer ---------------------------------------------------------------------------------------

function typed() {
  const text = e().text;
  text.style.height = "auto";
  text.style.height = `${Math.min(text.scrollHeight + 2, 200)}px`;
  suggest();
  renderComposer(viewed());
}

function keys(event) {
  const list = e().suggest;
  if (!list.hidden) {
    const items = [...list.children];
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      chat_.suggestion = (chat_.suggestion + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      highlight(items);
      return;
    }
    if ((event.key === "Tab" || event.key === "Enter") && chat_.suggestion >= 0) {
      event.preventDefault();
      pick(items[chat_.suggestion].dataset.name);
      return;
    }
    if (event.key === "Escape") {
      closeMenu();
      list.hidden = true;
      return;
    }
  }
  // Enter sends, and Shift+Enter starts a new line; on a phone, the keyboard's return key is for new lines.
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !touch()) {
    event.preventDefault();
    e().form.requestSubmit();
  }
}

// Commands that start with what's typed, while it's only "/" and a word.
function suggest() {
  closeMenu();
  const m = e().text.value.match(/^\/(\w*)$/);
  showCommands(m && chat_.data ? chat_.data.commands.filter((c) => c.name.startsWith(m[1].toLowerCase())) : []);
}

// Every command, from the button for them: one that needs nothing more runs as soon as it's chosen.
function commandMenu() {
  if (chat_.menu) return closeMenu();
  chat_.menu = true;
  e().commands.setAttribute("aria-expanded", "true");
  showCommands(chat_.data.commands);
  chat_.suggestion = -1;
  highlight([...e().suggest.children]);
}

function closeMenu() {
  if (!chat_.menu) return;
  chat_.menu = false;
  e().commands.setAttribute("aria-expanded", "false");
  e().suggest.hidden = true;
}

function showCommands(commands) {
  const list = e().suggest;
  list.hidden = !commands.length;
  chat_.suggestion = commands.length ? 0 : -1;
  list.replaceChildren(
    ...commands.map((c) =>
      h("li", { role: "option", "data-name": c.name, onmousedown: (event) => event.preventDefault(), onclick: () => pick(c.name) }, h("code", { text: `/${c.name}` }), h("span", { text: c.description }))
    )
  );
  highlight([...list.children]);
}

function highlight(items) {
  items.forEach((item, i) => item.setAttribute("aria-selected", String(i === chat_.suggestion)));
}

function pick(name) {
  const command = chat_.data.commands.find((c) => c.name === name);
  const fromMenu = chat_.menu;
  closeMenu();
  e().suggest.hidden = true;
  if (fromMenu && command && !command.takes) {
    send(null, `/${name}`);
    return;
  }
  e().text.value = `/${name} `;
  e().text.focus();
  renderComposer(viewed());
}

// -- small things --------------------------------------------------------------------------------------------

function lines(text) {
  return text.split("\n").flatMap((part, i) => (i ? [h("br"), part] : [part]));
}

function dayName(at) {
  if (isoDay(at) === isoDay(now())) return "Today";
  if (isoDay(at) === isoDay(now() - 86400)) return "Yesterday";
  return clock(at, { weekday: "long", day: "numeric", month: "long" });
}

function when(at) {
  if (!at) return "";
  if (isoDay(at) === isoDay(now())) return clock(at, { hour: "2-digit", minute: "2-digit" });
  if (isoDay(at) === isoDay(now() - 86400)) return "yesterday";
  return clock(at, { day: "numeric", month: "short" });
}
