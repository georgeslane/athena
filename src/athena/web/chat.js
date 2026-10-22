// An agent's chat, on the Agents tab (agents.js): talk to it, and see what its DAG's tasks with alert on, and the
// summary of each run, said. Sending to an agent makes it the main one. One message at a time: while Athena's
// answering you, wherever you asked, nothing more can be sent. A message sent while its DAG runs is answered once
// the task that's running has finished. Requests for your OK, from here or anywhere, can be answered here.
// Messages can come with photos and files, and commands work as in Telegram.

import { call, clock, h, icon, isoDay, now, plural, state, toast } from "./core.js";
import { markdown } from "./md.js";
import { canTurnOn, turnOnHere } from "./notifications.js";

const MAX_CHARS = 4000; // as chat.py takes
const MAX_SIDE = 2048; // a photo's longest side, as it's sent: larger ones are made smaller first
const MAX_FILE_BYTES = 20 * 1024 * 1024; // as chat.py takes
const RUN_SAID = /^\(DAG run #(\d+): (.+)\)$/; // what a run said in the chat, on your side (dags/runner.py)
const TASK_RAN = /^\(Your task “(.+?)”\)\s*([\s\S]*)$/; // a task's run, from before DAGs
const PHOTOS = /^\[(?:Photo|\d+ photos)\]\s*/; // how a message notes its photos (photos.label)
const FILE = /^\[File: ([^\]]+)\]\s*/; // how a message notes a file sent with it (files.receive)

const chat_ = {
  notes: new Map(), // id -> [{command, said}]: what commands said, here, until the page is reloaded
  attachments: [], // photos and files to send with the next message: {name, kind, data, preview}
  reading: 0, // photos and files still being read, before the message can be sent
  sending: false,
  suggestion: -1, // the command highlighted in the suggestions
  menu: false, // the suggestions are every command, from the button for them
  timer: null, // counting down requests for your OK
  scrollDown: false, // to the newest message, when it's next drawn
  els: null, // the view's parts, made once so what you're typing stays
};

const agents = () => state.agents;
const data = () => state.agents.data;
const shown = () => state.agents.id;
const isMain = (id) => data() && id === data().current;
const touch = () => window.matchMedia("(hover: none)").matches;

// An agent's messages and settings, the newest page and what was loaded before it: false if there's no such agent.
export async function loadPage(id) {
  let page;
  try {
    page = await call("GET", `/api/agents/${id}`);
  } catch (e) {
    if (e.status !== 404) throw e;
    agents().pages.delete(id);
    return false;
  }
  const known = agents().pages.get(id);
  const first = page.messages.length ? page.messages[0].id : Infinity;
  const earlier = known && known.messages && page.more ? known.messages.filter((m) => m.id < first) : [];
  agents().pages.set(id, { ...page, messages: [...earlier, ...page.messages], more: earlier.length ? known.more : page.more });
  return true;
}

async function showEarlier(event) {
  const id = shown();
  const known = agents().pages.get(id);
  event.target.disabled = true;
  const log = chat_.els.log;
  const fromBottom = log.scrollHeight - log.scrollTop;
  try {
    const page = await call("GET", `/api/agents/${id}?before=${known.messages[0].id}`);
    known.messages = [...page.messages, ...known.messages];
    known.more = page.more;
    renderChat();
    log.scrollTop = log.scrollHeight - fromBottom; // where you were
  } catch (e) {
    toast(e.message, true);
    event.target.disabled = false;
  }
}

// -- drawing it -----------------------------------------------------------------------------------------

export function chatView() {
  if (chat_.els) return chat_.els.root;
  const e = (chat_.els = {});
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
  e.root = h("div.chat-view", { ondragover: dragging, ondrop: dropped }, e.banner, e.log, e.form);
  document.addEventListener("athena-notifications", () => data() && renderBanner());
  // The list of commands closes when you tap or click anywhere else.
  document.addEventListener("click", (event) => {
    if (chat_.menu && !e.suggest.contains(event.target) && !e.commands.contains(event.target)) closeMenu();
  });
  return e.root;
}

export function renderChat(fresh = false) {
  if (!chat_.els || !data()) return;
  const id = shown();
  if (fresh) chat_.scrollDown = true;
  renderBanner();
  renderLog(id);
  renderComposer(id);
  countDown();
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
  const known = agents().pages.get(id) || { messages: [], more: false };
  const d = data();
  const items = [];
  if (known.more) items.push(h("li.chat-earlier", {}, h("button.button.small", { type: "button", text: "Show earlier", onclick: showEarlier })));
  let day = "";
  for (const m of known.messages) {
    const at = Date.parse(m.at) / 1000;
    if (isoDay(at) !== day) {
      day = isoDay(at);
      items.push(h("li.day-sep", {}, h("span", { text: dayName(at) })));
    }
    items.push(message(m, id));
  }
  for (const note of chat_.notes.get(id) || []) items.push(h("li.msg.mine.command", {}, h("div.msg-text", { text: note.command })), h("li.msg.note", {}, h("div.msg-text.md", {}, markdown(note.said))));
  const failed = d.failed[String(id)];
  if (failed) items.push(failedMessage(failed));
  const replying = d.replying && d.replying.session === id ? d.replying : null;
  if (replying && !kept(known, replying)) items.push(h("li.msg.mine.pending", {}, ...yours(replying.text, replying.photos), h("span.msg-meta", { text: "Sent" })));
  const elsewhere = d.elsewhere && d.elsewhere.session === id ? d.elsewhere : null;
  if (elsewhere) items.push(h("li.msg-notice", {}, h("span", { text: `Answering your message from ${elsewhere.channel || "elsewhere"}${elsewhere.text ? `: “${elsewhere.text}”` : ""}` })));
  for (const a of d.approvals.filter((a) => a.session === id || (a.session === null && isMain(id)))) items.push(approvalCard(a));
  if (replying || elsewhere) items.push(h("li.msg.theirs.typing", { "aria-label": "Athena is answering" }, h("span.dots", {}, h("i"), h("i"), h("i")), h("span.typing-step", { text: d.step && d.step !== "Thinking" ? d.step : "Thinking" })));
  if (!items.length) {
    items.push(
      h(
        "li.chat-empty",
        {},
        h("p", { text: "Nothing said here yet." }),
        h("p.hint", { text: "Tell this agent what you're after, give it guidance, and a DAG to run by itself. Type / for commands." })
      )
    );
  }
  log.replaceChildren(...items);
  if (atBottom || chat_.scrollDown) log.scrollTop = log.scrollHeight;
  chat_.scrollDown = false;
}

// Whether a message being answered is in the conversation already, as it is just before the answer's done.
const kept = (known, replying) => known.messages.some((m) => m.role === "user" && m.text.endsWith(replying.text) && Date.parse(m.at) / 1000 >= replying.started - 2);

function message(m, id) {
  if (m.role === "user" && m.channel === "Athena") {
    return h("li.msg-notice", {}, h("span", { text: m.text.replace(/^\(([\s\S]*)\)$/, "$1") })); // "(Claude's projects changed.)"
  }
  const time = clock(Date.parse(m.at) / 1000, { hour: "2-digit", minute: "2-digit" });
  const run = m.role === "user" && m.channel === "Task" && m.text.match(RUN_SAID);
  if (run) {
    const what = run[2] === "summary" ? `Run #${run[1]}: how it went` : `Run #${run[1]}: ${run[2].replace(/^task /, "")}`;
    return h("li.msg-task", {}, h("a.msg-task-link", { href: `#agents/${id}/runs` }, icon("clock"), h("span", { text: what }), h("span.msg-meta", { text: time })));
  }
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
        h("button.button.small.primary", { type: "button", text: "Try again", disabled: data().busy, onclick: () => send(null, "/retry") })
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
  const d = data();
  const stopping = Boolean(d.replying);
  let notice = "";
  if (d.elsewhere) notice = `${state.overview ? state.overview.assistant.name : "Athena"}'s answering your message from ${d.elsewhere.channel || "elsewhere"}. You can send once it's finished.`;
  else if (d.busy && !isMain(id)) notice = "Athena's answering in your main agent. You can write to this one once it's finished.";
  else if (!isMain(id)) notice = "Sending a message makes this the main agent, which Telegram, Siri and the terminal talk to.";
  e().notice.textContent = notice;
  e().notice.hidden = !notice;
  const once = [...agents().once];
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
  if (data() && data().replying) stop();
  else e().form.requestSubmit();
}

async function send(event, text) {
  if (event) event.preventDefault();
  text = (text ?? e().text.value).trim();
  const id = shown();
  const command = text.startsWith("/");
  const attachments = command ? [] : chat_.attachments;
  if ((!text && !attachments.length) || chat_.sending || chat_.reading || id === null || (data().busy && !command)) return;
  if (command && event && chat_.attachments.length) return toast("Send a command by itself, without photos or files.", true);
  chat_.sending = true;
  closeMenu();
  renderComposer(id);
  try {
    const body = { text, allow: command ? [] : [...agents().once], attachments: attachments.map((a) => ({ name: a.name, data: a.data })) };
    const said = await call("POST", `/api/agents/${id}/messages`, body);
    agents().data = said;
    if (event) {
      e().text.value = "";
      typed();
    }
    if (said.said) chat_.notes.set(id, [...(chat_.notes.get(id) || []), { command: text, said: said.said }]);
    if (!command) {
      agents().once.clear();
      chat_.attachments = [];
    }
    if (/^\/(new|session|reset)\b/.test(text) && said.current !== id) location.hash = `#agents/${said.current}`; // the new one
    chat_.scrollDown = true;
    await loadPage(id);
    agents().render();
  } catch (err) {
    toast(err.status === 413 ? "That's more than Athena can take at once: send fewer, or smaller, files." : err.message, true);
  } finally {
    chat_.sending = false;
    renderComposer(shown());
  }
}

async function stop() {
  try {
    await call("POST", "/api/agents/stop", {});
    agents().data = await call("GET", "/api/agents");
    agents().render();
  } catch (err) {
    toast(err.message, true);
  }
}

async function answer(event, approval, allow) {
  for (const button of event.target.parentElement.querySelectorAll("button")) button.disabled = true;
  try {
    agents().data = await call("POST", `/api/approvals/${approval.id}`, { allow });
    agents().render();
  } catch (err) {
    toast(err.message, true);
  }
}

function edit(text) {
  e().text.value = text;
  typed();
  e().text.focus();
}

// The tools that ask first, and whether each may run without asking: for the next message, or for when you talk to
// this agent from now on.
export function chooseTools(id) {
  const dialog = document.getElementById("dialog");
  const draw = () => {
    const known = agents().pages.get(id) || { allowed_tools: [] };
    const askable = data().askable;
    const rows = askable.map((tool) => {
      const now_ = known.allowed_tools.includes(tool.name) ? "session" : agents().once.has(tool.name) ? "once" : "ask";
      return h(
        "li.allow-tool",
        {},
        h("div.allow-name", {}, h("code", { text: tool.name }), h("span.hint", { text: tool.description })),
        h(
          "div.switcher",
          { role: "group", "aria-label": `When ${tool.name} may run without asking` },
          [["ask", "Ask"], ["once", "Next message"], ["session", "Always here"]].map(([choice, label]) =>
            h("button", { type: "button", text: label, "aria-pressed": String(now_ === choice), onclick: () => choose(tool.name, choice) })
          )
        )
      );
    });
    dialog.replaceChildren(
      h("h2", { text: "Tools that ask first" }),
      h("p.hint", { text: "Let one run without asking you when you talk to this agent: for your next message only, or from now on, wherever you're talking to it. Its DAG says what its tasks may do. Some, such as changing a DAG, always ask." }),
      askable.length ? h("ul.allow-tools", {}, rows) : h("p.empty", { text: "No tools ask first." }),
      h("div.tool-actions", {}, h("button.button.primary", { type: "button", text: "Done", onclick: () => dialog.close() }))
    );
  };
  const choose = async (name, choice) => {
    const known = agents().pages.get(id) || { allowed_tools: [] };
    const always = new Set(known.allowed_tools);
    agents().once.delete(name);
    always.delete(name);
    if (choice === "once") agents().once.add(name);
    if (choice === "session") always.add(name);
    if (always.size !== known.allowed_tools.length || choice === "session") {
      try {
        const page = await call("POST", `/api/agents/${id}/tools`, { allowed: [...always] });
        known.allowed_tools = page.allowed_tools;
      } catch (err) {
        toast(err.message, true);
      }
    }
    draw();
    agents().render();
  };
  draw();
  dialog.onclose = null;
  dialog.showModal();
}

// -- photos and files ---------------------------------------------------------------------------------------

async function attach(list) {
  chat_.reading += 1;
  renderComposer(shown());
  try {
    await attachEach(list);
  } finally {
    chat_.reading -= 1;
    renderComposer(shown());
  }
}

async function attachEach(list) {
  const limits = data().attach;
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
  renderComposer(shown());
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
  renderComposer(shown());
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
  showCommands(m && data() ? data().commands.filter((c) => c.name.startsWith(m[1].toLowerCase())) : []);
}

// Every command, from the button for them: one that needs nothing more runs as soon as it's chosen.
function commandMenu() {
  if (chat_.menu) return closeMenu();
  chat_.menu = true;
  e().commands.setAttribute("aria-expanded", "true");
  showCommands(data().commands);
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
  const command = data().commands.find((c) => c.name === name);
  const fromMenu = chat_.menu;
  closeMenu();
  e().suggest.hidden = true;
  if (fromMenu && command && !command.takes) {
    send(null, `/${name}`);
    return;
  }
  e().text.value = `/${name} `;
  e().text.focus();
  renderComposer(shown());
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
