// The Chat tab: talk to Athena in any of your sessions. Each session is a conversation, which Telegram, Siri and
// the terminal share; the current one is the one used last, and sending in an earlier one carries it on, as the
// current session. One message at a time: while Athena's answering you, wherever you asked, nothing more can be
// sent. Requests for your OK, from here or anywhere, can be answered here.

import { call, clock, h, icon, isoDay, now, plural, state, toast } from "./core.js";
import { markdown } from "./md.js";
import { bell } from "./notifications.js";

const MAX_CHARS = 4000; // as chat.py takes
const chat_ = {
  data: null, // GET /api/chat: the sessions, whether Athena's answering, what's waiting for your OK
  viewing: null, // the session shown: its id, or null for the current one
  sessions: new Map(), // id -> {messages, more, allowed}: what's been loaded of each
  notes: new Map(), // id -> [{command, said}]: what commands said, here, until the page is reloaded
  once: new Set(), // tools allowed without asking for the next message only
  sending: false,
  loading: null, // the load under way, so updates don't pile up
  again: false, // another update came while loading
  suggestion: -1, // the command highlighted in the suggestions
  timer: null, // counting down requests for your OK
  list: false, // on a phone: the list of sessions rather than a conversation
  scrollDown: false, // to the newest message, when it's next drawn
  last: null, // the newest message in your conversation, as the status said last
  els: null, // the tab's parts, made once so what you're typing stays
};

const viewed = () => chat_.viewing ?? (chat_.data ? chat_.data.current : null);
const isCurrent = (id) => chat_.data && id === chat_.data.current;
const touch = () => window.matchMedia("(hover: none)").matches;

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
      const id = viewed();
      if (id !== null) await loadSession(id);
      render();
    } while (chat_.again);
  })().finally(() => (chat_.loading = null));
  return chat_.loading;
}

// What the address says to show: #chat for the current session, #chat/<id> for another, and #chat/all for the
// list of them, which a phone shows instead of a conversation.
function route() {
  const [, which] = location.hash.split("/");
  chat_.list = which === "all";
  chat_.viewing = which && /^\d+$/.test(which) ? Number(which) : null;
}

async function loadSession(id) {
  let page;
  try {
    page = await call("GET", `/api/chat/${id}`);
  } catch (e) {
    if (e.status !== 404) throw e;
    chat_.viewing = null; // it's gone, as after forgetting everything
    history.replaceState(null, "", "#chat");
    return loadSession(chat_.data.current);
  }
  // The newest page, and what was loaded before it, if there's more than a page.
  const known = chat_.sessions.get(id);
  const first = page.messages.length ? page.messages[0].id : Infinity;
  const earlier = known && page.more ? known.messages.filter((m) => m.id < first) : [];
  chat_.sessions.set(id, {
    messages: [...earlier, ...page.messages],
    more: earlier.length ? known.more : page.more,
    allowed: page.allowed_tools,
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

// Athena's status has changed, or something in your conversation: the Chat tab's count, and the tab itself.
export function followChat(status) {
  // Athena's written in your conversation, while you're on another tab.
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

// -- drawing it -----------------------------------------------------------------------------------------

function build() {
  const e = (chat_.els = {});
  e.list = h("ol.session-list");
  e.newChat = h("button.button.small.primary", { type: "button", onclick: newChat }, icon("plus"), "New chat");
  e.sidebar = h("aside.chat-sessions", { "aria-label": "Your sessions" }, h("div.chat-sessions-head", {}, h("h2", { text: "Chats" }), e.newChat), e.list);
  e.back = h("a.button.quiet.small.chat-back", { href: "#chat/all", "aria-label": "Your chats" }, icon("back"), h("span.wide-only", { text: "Chats" }));
  e.title = h("h2.chat-title");
  e.sub = h("p.chat-sub");
  e.tools = h("button.button.small.chat-tools", { type: "button", onclick: chooseTools });
  e.head = h("header.chat-head", {}, e.back, h("div.chat-heading", {}, e.title, e.sub), bell(), e.tools);
  e.log = h("ol.chat-log", { role: "log", "aria-live": "polite", "aria-label": "Conversation" });
  e.text = h("textarea", {
    rows: 1,
    maxlength: MAX_CHARS,
    placeholder: "Message Athena",
    "aria-label": "Message",
    enterkeyhint: "send",
    oninput: typed,
    onkeydown: keys,
  });
  e.send = h("button.button.primary.chat-send", { type: "button", "aria-label": "Send", onclick: sendOrStop }, icon("send"));
  e.suggest = h("ul.chat-suggest", { role: "listbox", "aria-label": "Commands", hidden: true });
  e.notice = h("p.chat-notice");
  e.allowed = h("div.chat-allowed");
  e.form = h("form.composer", { onsubmit: send }, e.suggest, e.notice, e.allowed, h("div.composer-row", {}, e.text, e.send));
  e.main = h("section.chat-main", {}, e.head, e.log, e.form);
  e.root = h("div.chat", {}, e.sidebar, e.main);
  document.getElementById("tab-chat").replaceChildren(e.root);
}

function render() {
  if (!chat_.data) return;
  if (!chat_.els) build();
  const e = chat_.els;
  const id = viewed();
  e.root.dataset.view = chat_.list ? "list" : "conversation";
  renderSessions(id);
  renderHead(id);
  renderLog(id);
  renderComposer(id);
  countDown();
}

function renderSessions(id) {
  const items = chat_.data.sessions.map((s) =>
    h(
      "li",
      {},
      h(
        "a.session",
        { href: isCurrent(s.id) ? "#chat" : `#chat/${s.id}`, "aria-current": s.id === id ? "true" : null },
        h("span.session-title", { text: s.title || "New conversation" }),
        h("span.session-meta", {}, isCurrent(s.id) ? h("span.current", { text: "Current" }) : null, isCurrent(s.id) ? " · " : "", when(s.last_used), s.messages ? ` · ${plural(Math.ceil(s.messages / 2), "message")}` : "")
      )
    )
  );
  e().list.replaceChildren(...items);
  e().newChat.disabled = chat_.data.busy;
}

function renderHead(id) {
  const session = chat_.data.sessions.find((s) => s.id === id);
  e().title.textContent = (session && session.title) || "New conversation";
  e().sub.textContent = isCurrent(id)
    ? `Current session${session && session.messages ? ` · since ${when(session.started)}` : ""}`
    : `Earlier session · last used ${when(session ? session.last_used : 0)}`;
  const allowed = allowedHere(id);
  e().tools.replaceChildren(icon("shield"), h("span.wide-only", { text: allowed.length ? `${allowed.length} allowed` : "Tools" }), allowed.length ? h("span.tools-count", { text: String(allowed.length) }) : null);
  e().tools.setAttribute("aria-label", allowed.length ? `Tools: ${allowed.length} allowed without asking` : "Tools");
  e().tools.title = "Tools that can run without asking you";
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
  if (replying && !kept(known, replying)) items.push(h("li.msg.mine.pending", {}, h("div.msg-text", {}, lines(replying.text)), h("span.msg-meta", { text: "Sent" })));
  const elsewhere = d.elsewhere && d.elsewhere.session === id ? d.elsewhere : null;
  if (elsewhere) items.push(h("li.msg-notice", {}, h("span", { text: `Answering your message from ${elsewhere.channel || "elsewhere"}${elsewhere.text ? `: “${elsewhere.text}”` : ""}` })));
  for (const a of d.approvals.filter((a) => a.session === id || (a.session === null && isCurrent(id)))) items.push(approvalCard(a));
  if (replying || elsewhere) items.push(h("li.msg.theirs.typing", { "aria-label": "Athena is answering" }, h("span.dots", {}, h("i"), h("i"), h("i")), h("span.typing-step", { text: d.step && d.step !== "Thinking" ? d.step : "Thinking" })));
  if (!items.length) items.push(h("li.chat-empty", {}, h("p", { text: "Nothing said here yet." }), h("p.hint", { text: "What you say here and in Telegram goes into your current session. Type / for commands." })));
  log.replaceChildren(...items);
  if (atBottom || chat_.scrollDown) log.scrollTop = log.scrollHeight;
  chat_.scrollDown = false;
}

// Whether a message being answered is in the conversation already, as it is just before the answer's done.
const kept = (known, replying) => known.messages.some((m) => m.role === "user" && m.text === replying.text && Date.parse(m.at) / 1000 >= replying.started - 2);

function message(m) {
  if (m.role === "user" && m.channel === "Athena") {
    return h("li.msg-notice", {}, h("span", { text: m.text.replace(/^\(([\s\S]*)\)$/, "$1") })); // "(Your task “Briefing” ran.)"
  }
  const mine = m.role === "user";
  const meta = [clock(Date.parse(m.at) / 1000, { hour: "2-digit", minute: "2-digit" })];
  if (mine && m.channel && m.channel !== "Dashboard") meta.push(`via ${m.channel}`);
  if (!mine && m.tools.length) meta.push(`used ${[...new Set(m.tools)].join(", ")}`);
  return h(
    `li.msg.${mine ? "mine" : "theirs"}`,
    {},
    h(mine ? "div.msg-text" : "div.msg-text.md", {}, mine ? lines(m.text) : markdown(m.text)),
    h("span.msg-meta", { text: meta.join(" · ") })
  );
}

function failedMessage(failed) {
  return h(
    "li.msg.mine.failed",
    {},
    h("div.msg-text", {}, lines(failed.text)),
    h(
      "div.msg-failed",
      {},
      h("p", { text: `Didn't get an answer: ${failed.error}` }),
      h(
        "div.msg-actions",
        {},
        h("button.button.small", { type: "button", text: "Edit", onclick: () => edit(failed.text) }),
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
  // While Athena answers a message sent here, the button stops it.
  e().send.replaceChildren(icon(stopping ? "stop" : "send"));
  e().send.setAttribute("aria-label", stopping ? "Stop" : "Send");
  e().send.classList.toggle("stop", stopping);
  e().send.disabled = chat_.sending || (!stopping && (d.busy || !e().text.value.trim()));
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
  if (!text || chat_.sending || id === null || (chat_.data.busy && !text.startsWith("/"))) return;
  chat_.sending = true;
  renderComposer(id);
  try {
    const said = await call("POST", `/api/chat/${id}`, { text, allow: text.startsWith("/") ? [] : [...chat_.once] });
    chat_.data = said;
    if (event) {
      e().text.value = "";
      typed();
    }
    if (said.said) chat_.notes.set(id, [...(chat_.notes.get(id) || []), { command: text, said: said.said }]);
    if (!text.startsWith("/")) chat_.once.clear();
    if (/^\/(session|reset)\b/.test(text) && said.current !== id) location.hash = "#chat"; // the new one
    chat_.scrollDown = true;
    await loadChat();
  } catch (err) {
    toast(err.message, true);
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
    if (location.hash !== "#chat") location.hash = "#chat";
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
      h("p.hint", { text: "Let one run without asking you: for your next message only, or for the rest of this session, wherever you're talking to Athena in it. Some, such as scheduling tasks, always ask." }),
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
      complete(items[chat_.suggestion].dataset.name);
      return;
    }
    if (event.key === "Escape") {
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
  const list = e().suggest;
  const typed_ = e().text.value;
  const m = typed_.match(/^\/(\w*)$/);
  const matches = m && chat_.data ? chat_.data.commands.filter((c) => c.name.startsWith(m[1].toLowerCase())) : [];
  list.hidden = !matches.length;
  chat_.suggestion = matches.length ? 0 : -1;
  list.replaceChildren(
    ...matches.map((c) =>
      h("li", { role: "option", "data-name": c.name, onmousedown: (event) => (event.preventDefault(), complete(c.name)) }, h("code", { text: `/${c.name}` }), h("span", { text: c.description }))
    )
  );
  highlight([...list.children]);
}

function highlight(items) {
  items.forEach((item, i) => item.setAttribute("aria-selected", String(i === chat_.suggestion)));
}

function complete(name) {
  e().text.value = `/${name} `;
  e().suggest.hidden = true;
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
