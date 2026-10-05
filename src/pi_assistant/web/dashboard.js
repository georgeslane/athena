// Athena's dashboard. Plain JavaScript, no build step: it asks Athena for JSON (see dashboard.py)
// and builds the page from it. Everything shown comes from Athena, so it's added as text, never HTML.
"use strict";

// -- helpers ---------------------------------------------------------------------------------------

// h("div.card", {text: "hi", onclick: f}, ...children): an element, with classes, properties and children.
function h(tag, props, ...children) {
  const [name, ...classes] = tag.split(".");
  const el = document.createElement(name);
  if (classes.length) el.className = classes.join(" ");
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null) continue;
    if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "text") el.textContent = value;
    else if (key === "class") value && el.classList.add(...value.split(" "));
    else if (key === "style") Object.assign(el.style, value);
    else if (typeof value === "boolean") {
      if (key in el) el[key] = value;
      else if (value) el.setAttribute(key, "");
    } else if (typeof value === "number" && key in el) el[key] = value;
    else el.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === "") continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const ICONS = {
  chevron: "M9 6l6 6-6 6",
  lock: "M7 11V8a5 5 0 0 1 10 0v3M5 11h14v10H5z",
  info: "M12 8h.01M11 12h1v5h1M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z",
};

function icon(name, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  if (cls) svg.setAttribute("class", cls);
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

const number = (n) => Number(n || 0).toLocaleString("en-GB");

// A tool's name, which a narrow screen can break after an underscore rather than mid-word.
const breakable = (name) => name.split(/(?<=_)/).flatMap((part, i) => (i ? [h("wbr"), part] : [part]));
const plural = (n, word, many) => `${number(n)} ${n === 1 ? word : many || word + "s"}`;

function seconds(s) {
  if (!s) return "–";
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 90) return `${Math.round(s)} s`;
  return duration(s);
}

function duration(s) {
  s = Math.max(0, Math.floor(s));
  const hours = Math.floor(s / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const secs = String(s % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${secs}` : `${minutes}:${secs}`;
}

function ago(s) {
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return plural(Math.floor(s / 86400), "day") + " ago";
}

// A time in Athena's time zone, which is the one you live in.
function clock(timestamp, options) {
  const date = new Date(timestamp * 1000);
  try {
    return new Intl.DateTimeFormat("en-GB", { timeZone: state.timezone, ...options }).format(date);
  } catch {
    return new Intl.DateTimeFormat("en-GB", options).format(date); // a time zone this browser doesn't know
  }
}

const sameDay = (a, b) => clock(a, { dateStyle: "short" }) === clock(b, { dateStyle: "short" });
const when = (t) => (sameDay(t, now()) ? clock(t, { hour: "2-digit", minute: "2-digit" }) : clock(t, { day: "numeric", month: "short" }));

// Athena's clock rather than this device's, for timing what it reports.
const now = () => Date.now() / 1000 + state.skew;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// -- talking to Athena --------------------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function call(method, path, body, signal) {
  const options = { method, credentials: "same-origin", cache: "no-store", signal, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "Can't reach Athena.");
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    // not JSON
  }
  if (response.status === 401 && path !== "/api/login") showSignIn();
  if (!response.ok) throw new ApiError(response.status, (data && data.error) || `Athena said ${response.status}.`);
  return data;
}

// -- state -------------------------------------------------------------------------------------------

const state = {
  overview: null,
  status: null, // what Athena is doing: /api/status
  tools: null,
  cards: new Map(), // the Tools tab's cards, by tool id
  timezone: undefined,
  skew: 0,
  offline: false,
  show: "session", // on a phone, this session's numbers or all time's
  watching: null, // the AbortController of the request waiting for the status to change
};

const appShown = () => !document.getElementById("app").hidden;

// -- signing in -------------------------------------------------------------------------------------------

function showSignIn() {
  if (state.watching) state.watching.abort();
  document.getElementById("app").hidden = true;
  document.getElementById("sign-in").hidden = false;
  document.getElementById("password").focus();
}

async function signIn(event) {
  event.preventDefault();
  const form = event.target;
  const error = document.getElementById("sign-in-error");
  const button = form.querySelector("button");
  error.textContent = "";
  button.disabled = true;
  try {
    await call("POST", "/api/login", { password: form.password.value });
    form.password.value = "";
    document.getElementById("sign-in").hidden = true;
    await showApp();
  } catch (e) {
    error.textContent = e.message;
  } finally {
    button.disabled = false;
  }
}

async function signOut() {
  await call("POST", "/api/logout", {}).catch(() => {});
  location.reload();
}

// -- the app ------------------------------------------------------------------------------------------

async function start() {
  document.getElementById("sign-in-form").addEventListener("submit", signIn);
  window.addEventListener("hashchange", showTab);
  document.addEventListener("visibilitychange", () => !document.hidden && appShown() && refresh());
  setInterval(tick, 1000);
  setInterval(() => !document.hidden && appShown() && refresh(), 30000);
  for (;;) {
    try {
      await showApp();
      return;
    } catch (e) {
      if (e.status === 401) return; // the sign-in form is showing
      document.getElementById("app").hidden = false;
      setOffline(true);
      await sleep(5000);
    }
  }
}

async function showApp() {
  await loadOverview();
  document.getElementById("app").hidden = false;
  showTab();
  if (!state.watching) watchStatus();
}

function refresh() {
  loadOverview().catch(() => {});
  if (location.hash === "#tools") loadTools().catch(() => {});
  if (!state.watching) watchStatus();
}

function showTab() {
  const tab = location.hash === "#tools" ? "tools" : "status";
  for (const link of document.querySelectorAll(".tab")) {
    if (link.dataset.tab === tab) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const panel of document.querySelectorAll(".panel")) panel.hidden = panel.dataset.panel !== tab;
  if (tab === "tools") loadTools().catch((e) => toast(e.message, true));
  window.scrollTo(0, 0);
}

function setOffline(offline) {
  if (state.offline === offline) return;
  state.offline = offline;
  document.getElementById("offline").hidden = !offline;
  renderStatus();
}

// Follow what Athena is doing: each request waits until something changes, or 25 seconds pass.
async function watchStatus() {
  const controller = new AbortController();
  state.watching = controller;
  let version = "";
  while (!controller.signal.aborted) {
    try {
      const wait = version ? "&wait=25" : "";
      const data = await call("GET", `/api/status?after=${encodeURIComponent(version)}${wait}`, undefined, controller.signal);
      const was = state.status && state.status.status.state;
      setStatus(data);
      setOffline(false);
      if (version && was !== "idle" && data.status.state === "idle") loadOverview().catch(() => {}); // new numbers
      version = data.version;
    } catch (e) {
      if (controller.signal.aborted || e.status === 401) break;
      setOffline(true);
      version = "";
      await sleep(5000);
    }
  }
  if (state.watching === controller) state.watching = null;
}

function setStatus(data) {
  state.status = data;
  state.skew = data.now - Date.now() / 1000;
  renderStatus();
}

async function loadOverview() {
  const overview = await call("GET", "/api/overview");
  state.overview = overview;
  state.timezone = overview.assistant.timezone;
  for (const el of document.querySelectorAll("[data-name]")) el.textContent = overview.assistant.name;
  document.title = overview.assistant.name;
  setStatus({ version: overview.version, now: overview.now, status: overview.status });
  setOffline(false);
  renderOverview();
  // Athena asks the model server how it is in the background: ask again for its answer.
  if (overview.model.reachable === null) setTimeout(() => loadOverview().catch(() => {}), 2500);
}

function tick() {
  if (document.hidden || !state.status) return;
  if (state.status.status.state !== "idle" || Math.floor(now()) % 30 === 0) renderStatus();
}

// -- the Status tab ---------------------------------------------------------------------------------------

const LABELS = { idle: "Idle", working: "Working", approval: "Needs you", offline: "Offline" };

function look() {
  return state.offline ? "offline" : state.status.status.state;
}

function shieldLook() {
  const s = state.status.status;
  return !state.offline && s.state === "idle" && s.last_error ? "failed" : look();
}

function renderStatus() {
  if (!state.status) return;
  for (const shield of document.querySelectorAll(".shield")) shield.dataset.state = shieldLook();
  const pill = document.getElementById("top-pill");
  pill.dataset.state = look();
  pill.querySelector(".pill-label").textContent = LABELS[look()];
  now_.update();
}

// The status board, larger: what Athena is doing now, or what it did last.
const now_ = {
  el: null,
  build() {
    this.parts = {
      pill: h("span.pill", {}, h("span.dot"), h("span.pill-label")),
      time: h("span.time"),
      headline: h("p.now-headline"),
      sub: h("p.now-sub"),
      label: h("span.label"),
      task: h("p.now-task"),
      footer: h("p.now-footer"),
    };
    const p = this.parts;
    this.el = h(
      "section.card.now",
      { "aria-live": "polite" },
      h(
        "div.shield",
        { "aria-hidden": "true" },
        h("img", { src: "athena.svg", alt: "", width: 120, height: 120 }),
        h("span.eye.left"),
        h("span.eye.right"),
        h("span.ring")
      ),
      h("div", {}, h("div.now-top", {}, p.pill, p.time), p.headline, p.sub),
      h("div.now-detail", {}, h("div.meander", { "aria-hidden": "true" }), p.label, p.task, p.footer)
    );
    this.update();
    return this.el;
  },
  update() {
    if (!this.el || !state.status) return;
    const s = state.status.status;
    const t = now();
    const p = this.parts;
    const state_ = look();
    let time = clock(t, { hour: "2-digit", minute: "2-digit" });
    let headline = "Ready";
    let sub = clock(t, { weekday: "long", day: "numeric", month: "long" });
    if (state_ === "working") {
      time = duration(t - s.started);
      headline = s.step || "Thinking";
      if (headline === "Thinking") headline += ".".repeat(Math.floor(t) % 4);
      sub = s.channel ? `From ${s.channel}` : "";
    } else if (state_ === "approval") {
      time = s.deadline ? `${duration(s.deadline - t)} left` : duration(t - s.started);
      headline = `Approve in ${s.channel || "the chat"}`;
      sub = s.tool;
    } else if (state_ === "offline") {
      headline = "Not answering";
      sub = "Is the pi-assistant service running?";
    }

    let label = "";
    let task = "";
    let footer = "";
    let footerClass = "";
    if (state_ === "working" || state_ === "approval") {
      label = "Task";
      task = s.task || "A request" + (s.channel ? ` from ${s.channel}` : "");
      footer = s.tools.join(" › ");
    } else if (state_ === "offline") {
      task = "Its log on the Pi says why: journalctl -u pi-assistant -f";
    } else if (!s.last_finished) {
      task = "Nothing yet. Send a message to get started.";
    } else {
      label = "Last task";
      task = s.last_task || "A request";
      footer = s.last_error ? `✗  ${s.last_error} · ${ago(t - s.last_finished)}` : `✓  Done · ${ago(t - s.last_finished)}`;
      footerClass = s.last_error ? "failed" : "done";
    }

    p.pill.dataset.state = state_;
    p.pill.querySelector(".pill-label").textContent = LABELS[state_];
    p.time.textContent = time;
    p.headline.textContent = headline;
    p.sub.textContent = sub;
    p.sub.className = "now-sub" + (state_ === "approval" ? " approval" : "");
    p.sub.hidden = !sub;
    p.label.textContent = label;
    p.label.hidden = !label;
    p.task.textContent = task;
    p.footer.textContent = footer;
    p.footer.className = "now-footer" + (footerClass ? ` ${footerClass}` : "");
    p.footer.hidden = !footer;
  },
};

function renderOverview() {
  const o = state.overview;
  const panel = document.getElementById("tab-status");
  const usage = h(
    "div.usage",
    { "data-show": state.show },
    h(
      "div.segmented",
      { role: "group", "aria-label": "Show the numbers for" },
      ["session", "total"].map((period) =>
        h("button", {
          type: "button",
          text: period === "session" ? "This session" : "All time",
          "aria-pressed": String(state.show === period),
          onclick: () => {
            state.show = period;
            renderOverview();
          },
        })
      )
    ),
    h("div.grid.two", {}, usageCard("session", o.session), usageCard("total", o.total))
  );
  panel.replaceChildren(
    h("div.stack", {}, now_.el || now_.build(), usage, h("div.grid.two", {}, memoryCard(o), connectionsCard(o))),
    h("button.button.quiet.small.sign-out", { type: "button", text: "Sign out", onclick: signOut })
  );
}

function usageCard(period, data) {
  const max = state.overview.model.context_window;
  const session = period === "session";
  let aside = plural(data.sessions || 0, "session");
  if (session) aside = `Session ${data.id} · since ${when(data.started)}`;
  else if (data.first) aside = `Since ${clock(data.first, { day: "numeric", month: "short", year: "numeric" })} · ${aside}`;
  return h(
    "section.card",
    { "data-period": period },
    h(
      "div.card-head",
      {},
      h("h2", { text: session ? "This session" : "All time" }),
      h("span.aside", { text: aside }),
      session && h("div.actions", {}, h("button.button.small", { type: "button", text: "New session", onclick: newSession }))
    ),
    h(
      "div.metrics",
      {},
      metric(number(data.queries), data.queries === 1 ? "Message" : "Messages"),
      metric(number(data.tool_calls), data.tool_calls === 1 ? "Tool call" : "Tool calls"),
      metric(seconds(data.average_seconds), "Average reply")
    ),
    data.failed ? h("p.hint", { style: { marginTop: "8px" }, text: `${plural(data.failed, "message")} didn't get an answer.` }) : null,
    session ? contextMeter(data.context, max) : largest(data.largest_prompt, max),
    h(
      "div.by-tool",
      {},
      h("span.label", { text: "Tool calls by tool" }),
      data.tools.length
        ? h("ul.tool-bars", {}, data.tools.map((t) => toolBar(t, data.tools[0].calls)))
        : h("p.empty", { text: session ? "None yet this session." : "None yet." })
    )
  );
}

function metric(value, name) {
  return h("div.metric", {}, h("span.value", { text: value }), h("span.name", { text: name }));
}

function contextMeter(context, max) {
  const tokens = context ? context.tokens : 0;
  const share = tokens && max ? Math.min(1, tokens / max) : 0;
  const width = tokens && max ? Math.max(share * 100, 1) : 0;
  return h(
    "div.context",
    {},
    h(
      "div.context-row",
      {},
      h("span.label", { text: "Context" }),
      h("span.value", { text: tokens ? `${number(tokens)} tokens` : "Not measured yet" }),
      max ? h("span.of", { text: `of ${number(max)}` }) : null,
      share ? h("span.percent", { text: share < 0.01 ? "<1%" : `${Math.round(share * 100)}%` }) : null
    ),
    h("div.bar", { class: share > 0.9 ? "full" : share > 0.7 ? "warn" : "" }, h("span", { style: { width: `${width}%` } })),
    h("p.hint", {
      text: tokens
        ? "What Athena reads before your next message: its instructions, its tools and this session's conversation." +
          (max || state.overview.model.reachable !== true ? "" : " The model server didn't say its limit: set llm.context_window to show it.")
        : "It's measured after Athena's next reply.",
    })
  );
}

function largest(tokens, max) {
  if (!tokens) return null;
  return h(
    "div.context",
    {},
    h(
      "div.context-row",
      {},
      h("span.label", { text: "Largest request" }),
      h("span.value", { text: `${number(tokens)} tokens` }),
      max ? h("span.of", { text: `of ${number(max)}` }) : null
    )
  );
}

function toolBar(tool, most) {
  const extra = [tool.failed && `${tool.failed} failed`, tool.declined && `${tool.declined} declined`].filter(Boolean).join(", ");
  return h(
    "li.tool-bar",
    {},
    h("span.name", { text: tool.name, title: tool.name }),
    h("span.count", {}, number(tool.calls), extra ? h("small", { text: extra }) : null),
    h("span.track", {}, h("span", { style: { width: `${(tool.calls / most) * 100}%` } }))
  );
}

function memoryCard(o) {
  const m = o.memory;
  return h(
    "section.card",
    {},
    h("div.card-head", {}, h("h2", { text: "Memory" })),
    h(
      "div.memory-counts",
      {},
      metric(number(m.facts), m.facts === 1 ? "Fact" : "Facts"),
      metric(number(m.documents), "From notes"),
      metric(number(m.conversations), m.conversations === 1 ? "Exchange" : "Exchanges")
    ),
    h("p.hint", {
      style: { marginTop: "10px" },
      text: "Exchanges are your messages and Athena's replies, kept so it can search what you've talked about in any session.",
    }),
    h(
      "div.danger-zone",
      {},
      h("p.hint", { text: "Forgetting everything deletes every memory and the conversation history." }),
      h("button.button.danger", { type: "button", text: "Forget everything…", onclick: forgetEverything })
    )
  );
}

function connectionsCard(o) {
  const model = o.model;
  let server = h("span.is", { text: "Checking…" });
  if (model.reachable && !model.error) server = h("span.is.good", { text: "Ready" });
  else if (model.reachable !== null) server = h("span.is.bad", { text: model.error || "Can't reach it" });
  const tools = o.tools;
  const servers = tools.servers ? `${tools.connected} of ${plural(tools.servers, "server")} connected` : "no MCP servers";
  return h(
    "section.card",
    {},
    h("div.card-head", {}, h("h2", { text: "Connections" })),
    h(
      "ul.facts",
      {},
      h("li", {}, h("span.what", { text: "Model" }), h("span.is", { text: model.name })),
      h("li", {}, h("span.what", { text: "Model server" }), server),
      h("li", {}, h("span.what", { text: "Context limit" }), h("span.is", { text: model.context_window ? `${number(model.context_window)} tokens` : "Unknown" })),
      h(
        "li",
        {},
        h("span.what", { text: "Tools" }),
        h("span.is", { text: tools.applying ? "Applying changes…" : `${plural(tools.count, "tool")}, ${servers}` }),
        h("a.button.small.end", { href: "#tools", text: "Manage" })
      )
    )
  );
}

async function newSession(event) {
  const button = event.target;
  button.disabled = true;
  try {
    state.overview = await call("POST", "/api/session", {});
    renderOverview();
    toast(`Started session ${state.overview.session.id}. The conversation is cleared from Athena's context.`);
  } catch (e) {
    toast(e.message, true);
    button.disabled = false;
  }
}

async function forgetEverything() {
  const m = state.overview.memory;
  const typed = h("input", { type: "text", autocomplete: "off", autocapitalize: "none", spellcheck: false, placeholder: "forget" });
  const ok = await ask({
    title: "Forget everything?",
    body: [
      h("p", { text: `${state.overview.assistant.name} will delete, for good:` }),
      h(
        "ul",
        {},
        m.facts ? h("li", { text: plural(m.facts, "saved fact") }) : null,
        m.documents ? h("li", { text: `${plural(m.documents, "chunk")} of your notes, which pi-assistant ingest can add again` }) : null,
        h("li", { text: m.conversations ? `${plural(m.conversations, "past exchange")}, and the conversation history` : "The conversation history" })
      ),
      h("p", { text: "It also starts a new session. Usage statistics are kept. Type forget to confirm." }),
      typed,
    ],
    confirm: "Delete everything",
    danger: true,
    ready: () => typed.value.trim().toLowerCase() === "forget",
    watch: typed,
  });
  if (!ok) return;
  try {
    state.overview = await call("POST", "/api/forget", { confirm: "forget" });
    renderOverview();
    toast("Every memory is deleted, and a new session has started.");
  } catch (e) {
    toast(e.message, true);
  }
}

// -- the Tools tab --------------------------------------------------------------------------------------

let polling = null;

async function loadTools() {
  showTools(await call("GET", "/api/tools"));
}

function showTools(data) {
  state.tools = data;
  const panel = document.getElementById("tab-tools");
  if (!panel.querySelector(".tools")) panel.replaceChildren(toolsPage());
  const more = data.tools.filter((t) => !t.builtin && !t.added);
  fillList("builtin", data.tools.filter((t) => t.builtin));
  fillList("servers", data.tools.filter((t) => !t.builtin && t.added));
  fillList("more", more);
  panel.querySelector("[data-group='more']").hidden = !more.length;
  const ids = new Set(data.tools.map((t) => t.id));
  for (const id of [...state.cards.keys()]) if (!ids.has(id)) state.cards.delete(id);

  const banner = panel.querySelector(".tools-banner");
  const problem = data.error || (!data.editable && "Athena was started without a config.toml, so these can't be changed here.");
  banner.hidden = !data.applying && !problem;
  banner.classList.toggle("info", !problem);
  banner.replaceChildren(problem || h("span", {}, h("span.spinner"), " Applying your changes. New messages wait until it's done."));
  for (const card of state.cards.values()) card.setEditable(data.editable && !data.applying);

  clearTimeout(polling);
  if (data.applying) polling = setTimeout(() => loadTools().catch(() => {}), 1200);
  else if (state.overview && state.overview.tools.applying) loadOverview().catch(() => {});
}

function toolsPage() {
  return h(
    "div.tools",
    {},
    h(
      "div.tools-intro",
      {},
      h("p", { text: "Switch tools on and off, and change their settings and keys. Changes apply straight away, once Athena has finished what it's doing." }),
      h("button.button.small", { type: "button", text: "Reconnect all", onclick: reconnect })
    ),
    h("div.banner.tools-banner", { role: "status", hidden: true }),
    h("h2.label.group-title", { text: "Built in" }),
    h("div.tool-list", { "data-list": "builtin" }),
    h("h2.label.group-title", { text: "MCP servers" }),
    h("div.tool-list", { "data-list": "servers" }),
    h("div", { "data-group": "more" }, h("h2.label.group-title", { text: "Recommended, not set up yet" }), h("div.tool-list", { "data-list": "more" })),
    h("h2.label.group-title", { text: "Another server" }),
    new AddServer().el
  );
}

function fillList(name, tools) {
  const list = document.querySelector(`[data-list='${name}']`);
  const cards = tools.map((data) => {
    let card = state.cards.get(data.id);
    if (card) card.update(data);
    else state.cards.set(data.id, (card = new ToolCard(data)));
    return card.el;
  });
  // Only move cards if the list has changed: moving one takes the focus from a field you're typing in.
  const same = cards.length === list.children.length && cards.every((el, i) => list.children[i] === el);
  if (!same) list.replaceChildren(...cards);
}

async function reconnect(event) {
  const button = event.target;
  button.disabled = true;
  try {
    showTools(await call("POST", "/api/reconnect", {}));
    toast("Reconnecting every server…");
  } catch (e) {
    toast(e.message, true);
  } finally {
    button.disabled = false;
  }
}

async function change(id, body) {
  showTools(await call("POST", `/api/tools/${encodeURIComponent(id)}`, body));
}

const isRef = (value) => typeof value === "string" && /^\$\{[A-Za-z_][A-Za-z0-9_]*\}$/.test(value);

// One tool: its name and state, a switch, and its settings when you open it.
class ToolCard {
  constructor(data) {
    this.data = data;
    this.open = false;
    this.editable = true;
    this.error = "";
    this.draft = null; // what you've changed and not saved
    this.toggle = h("input", { type: "checkbox", role: "switch", onchange: () => this.switched() });
    this.state = h("span.tool-state", {}, h("span.dot"), h("span.text"));
    this.title = h("span.title");
    this.tag = h("span.tag", { text: "Added", hidden: true });
    this.summary = h("span.tool-summary");
    this.opener = h(
      "button.tool-open",
      { type: "button", "aria-expanded": "false", onclick: () => this.setOpen(!this.open) },
      h("span.tool-title", {}, icon("chevron", "chevron"), this.title, this.tag),
      this.summary,
      this.state
    );
    this.body = h("div.tool-body", { hidden: true });
    this.el = h("article.card.tool", { "data-open": "false" }, h("div.tool-head", {}, this.opener, h("label.switch", {}, this.toggle, h("span"))), this.body);
    this.renderHead();
  }

  update(data) {
    this.data = data;
    this.renderHead();
    if (this.open && !this.isDirty() && !this.el.contains(document.activeElement)) this.renderBody();
  }

  setEditable(editable) {
    this.editable = editable;
    this.toggle.disabled = !editable;
    for (const el of this.body.querySelectorAll("[data-save]")) el.disabled = !editable;
  }

  renderHead() {
    const d = this.data;
    this.title.textContent = d.title;
    this.tag.hidden = !d.custom;
    this.summary.textContent = d.summary;
    this.summary.hidden = !d.summary;
    this.state.dataset.state = d.state;
    this.state.querySelector(".text").textContent = d.detail;
    this.toggle.checked = d.enabled;
    this.toggle.setAttribute("aria-label", `Use ${d.title}`);
  }

  setOpen(open) {
    this.open = open;
    this.el.dataset.open = String(open);
    this.opener.setAttribute("aria-expanded", String(open));
    this.body.hidden = !open;
    if (open) this.renderBody();
    else this.draft = null;
  }

  isDirty() {
    return !!this.draft && Object.keys(this.changes()).length > 0;
  }

  async switched() {
    const enabled = this.toggle.checked;
    this.error = "";
    this.toggle.disabled = true;
    try {
      await change(this.data.id, { enabled });
      toast(`${this.data.title} is ${enabled ? "on" : "off"}. Applying…`);
    } catch (e) {
      this.toggle.checked = !enabled;
      this.error = e.message;
      if (this.open) this.renderBody();
      else this.setOpen(true);
    } finally {
      this.toggle.disabled = !this.editable;
    }
  }

  // -- its settings ------------------------------------------------------------------------------------

  renderBody() {
    const d = this.data;
    this.draft = {
      fields: Object.fromEntries(d.fields.map((f) => [f.key, structuredClone(f.value)])),
      secrets: {},
      tools: d.tools && d.tools[0] && d.tools[0].asks !== null ? Object.fromEntries(d.tools.map((t) => [t.name, { on: t.on, asks: t.asks }])) : null,
    };
    const parts = [];
    if (d.asks) parts.push(h("p.note", {}, icon("lock"), h("span", { text: d.asks })));
    if (d.setup) parts.push(h("p.note.setup", {}, icon("info"), h("span", { text: d.setup })));
    // A server you added runs either as a command on the Pi or at a URL: show the settings for whichever it is.
    const byUrl = d.custom && d.fields.some((f) => f.key === "url" && f.value);
    const unused = d.custom ? (byUrl ? ["command", "args", "env"] : ["url", "headers"]) : [];
    for (const f of d.fields) if (!unused.includes(f.key)) parts.push(this.fieldEditor(f));
    if (!d.custom) for (const s of d.secrets) parts.push(this.secretEditor(s));
    if (d.tools) parts.push(this.toolsEditor());
    const error = h("p.form-error", { role: "alert", text: this.error });
    const save = h("button.button.primary", { type: "button", text: "Save", "data-save": true, disabled: !this.editable, onclick: () => this.save(save, error) });
    const cancel = h("button.button.quiet", {
      type: "button",
      text: "Cancel",
      onclick: () => {
        this.error = "";
        this.setOpen(false);
      },
    });
    const remove = d.custom ? h("button.button.danger.small", { type: "button", text: "Remove server", "data-save": true, disabled: !this.editable, onclick: () => this.remove() }) : null;
    this.body.replaceChildren(...parts, error, h("div.tool-actions", {}, save, cancel, h("span.grow"), remove));
  }

  fieldEditor(f) {
    const label = h("span.field-label", { text: f.label });
    const help = f.help ? h("span.field-help", { text: f.help }) : null;
    const set = (value) => (this.draft.fields[f.key] = value);
    if (f.kind === "map") return h("div.field", {}, label, help, this.mapEditor(f));
    if (f.kind === "lines") {
      const area = h("textarea", { rows: 3, spellcheck: false, autocapitalize: "none", oninput: () => set(area.value.split("\n")) });
      area.value = f.value.join("\n");
      return h("label.field", {}, label, help, area);
    }
    if (f.kind === "select") {
      const select = h("select", { onchange: () => set(select.value) }, f.options.map((o) => h("option", { value: o.value, text: o.label })));
      select.value = f.value;
      return h("label.field", {}, label, help, select);
    }
    const input = h("input", { type: "text", spellcheck: false, autocapitalize: "none", autocomplete: "off", oninput: () => set(input.value) });
    input.value = f.value;
    return h("label.field", {}, label, help, input);
  }

  // Names and values, like news feeds. A server you added can keep a value in .env instead: a secret.
  mapEditor(f) {
    const secrets = this.data.custom && (f.key === "env" || f.key === "headers");
    const rows = Object.entries(f.value).map(([name, value]) => ({ name, value, ref: isRef(value) ? value : null, secret: isRef(value), typed: "" }));
    const box = h("div.map");
    const sync = () => {
      const result = {};
      for (const row of rows) {
        if (!row.name.trim()) continue;
        if (row.ref) result[row.name.trim()] = row.typed ? { secret: row.typed } : row.ref;
        else result[row.name.trim()] = row.secret ? { secret: row.value } : row.value;
      }
      this.draft.fields[f.key] = result;
    };
    const draw = () => {
      const cls = secrets ? "with-secret" : "";
      const head = rows.length ? [h("div.map-row.map-head", { class: cls }, h("span", { text: f.columns[0] }), h("span", { text: f.columns[1] }))] : [];
      box.replaceChildren(
        ...head,
        ...rows.map((row, i) => {
          const name = h("input", { type: "text", value: row.name, placeholder: f.columns[0], "aria-label": f.columns[0], spellcheck: false, autocapitalize: "none", oninput: () => ((row.name = name.value), sync()) });
          const value = h("input", {
            type: row.secret ? "password" : "text",
            value: row.ref ? "" : row.value,
            placeholder: row.ref ? `In .env as ${row.ref.slice(2, -1)}. Type to replace` : f.columns[1],
            "aria-label": f.columns[1],
            spellcheck: false,
            autocapitalize: "none",
            autocomplete: "off",
            oninput: () => {
              if (row.ref) row.typed = value.value;
              else row.value = value.value;
              sync();
            },
          });
          let secret = null;
          if (secrets && row.ref) secret = h("span.check", { text: "Secret" });
          else if (secrets) {
            secret = h("label.check", {}, h("input", { type: "checkbox", checked: row.secret, onchange: (e) => ((row.secret = e.target.checked), sync(), draw()) }), "Secret");
          }
          const remove = h("button.button.small.remove", { type: "button", text: "×", "aria-label": `Remove ${row.name || "this row"}`, onclick: () => (rows.splice(i, 1), sync(), draw()) });
          return h("div.map-row", { class: cls }, name, value, secret, remove);
        }),
        h(
          "div",
          {},
          h("button.button.small", {
            type: "button",
            text: `Add ${f.item}`,
            onclick: () => {
              rows.push({ name: "", value: "", ref: null, secret: false, typed: "" });
              draw();
              box.querySelectorAll(".map-row:not(.map-head) input[type='text']")[rows.length - 1].focus();
            },
          })
        )
      );
    };
    draw();
    return box;
  }

  secretEditor(s) {
    const input = h("input", {
      type: "password",
      autocomplete: "off",
      spellcheck: false,
      autocapitalize: "none",
      "aria-label": s.label,
      placeholder: s.set ? "Set. Type a new one to replace it" : "Not set yet",
      oninput: () => (this.draft.secrets[s.name] = input.value),
    });
    const remove = s.set && !s.required ? h("button.button.small.quiet", { type: "button", text: "Remove", "data-save": true, onclick: () => this.removeSecret(s) }) : null;
    return h(
      "div.secret",
      {},
      h("div.secret-head", {}, h("span.field-label", { text: s.label }), h("span.badge", { class: s.set ? "set" : "unset", text: s.set ? "Set" : "Not set" })),
      s.help ? h("span.field-help", { text: s.help }) : null,
      h("div.secret-input", {}, input, remove)
    );
  }

  toolsEditor() {
    const d = this.data;
    if (!this.draft.tools) {
      return h("div.field", {}, h("span.field-label", { text: "Its tools" }), h("div.chips", {}, d.tools.map((t) => h("span.chip", { text: t.name }))));
    }
    const rows = d.tools.map((t) => {
      const choice = this.draft.tools[t.name];
      const row = h("div.server-tool", { class: choice.on ? "" : "off" });
      const asks = h("input", { type: "checkbox", checked: choice.asks, disabled: !choice.on, "aria-label": `${t.name} asks first`, onchange: () => (choice.asks = asks.checked) });
      const on = h("input", {
        type: "checkbox",
        role: "switch",
        checked: choice.on,
        "aria-label": `Use ${t.name}`,
        onchange: () => {
          choice.on = on.checked;
          if (on.checked) choice.asks = asks.checked = true; // a tool you've just switched on asks first until you say otherwise
          asks.disabled = !on.checked;
          row.classList.toggle("off", !on.checked);
        },
      });
      row.append(
        h("div", {}, h("span.name", {}, breakable(t.name)), t.description ? h("span.description", { text: t.description }) : null),
        h("label.switch.small", {}, on, h("span")),
        h("label.check", {}, asks)
      );
      return row;
    });
    return h(
      "div.field",
      {},
      h("span.field-label", { text: "Its tools" }),
      h("span.field-help", { text: "Only let a tool run without asking if it can't change anything, or send what's in the conversation anywhere." }),
      h("div.server-tools", {}, h("div.server-tools-head", {}, h("span", { text: "Tool" }), h("span", { text: "Use" }), h("span", { text: "Asks first" })), rows)
    );
  }

  changes() {
    const d = this.data;
    const body = {};
    const fields = {};
    for (const f of d.fields) {
      let value = this.draft.fields[f.key];
      if (f.kind === "lines") value = value.map((v) => v.trim()).filter(Boolean);
      if (JSON.stringify(value) !== JSON.stringify(f.value)) fields[f.key] = value;
    }
    if (Object.keys(fields).length) body.fields = fields;
    const secrets = Object.fromEntries(Object.entries(this.draft.secrets).filter(([, v]) => v.trim()));
    if (Object.keys(secrets).length) body.secrets = secrets;
    const tools = this.draft.tools;
    if (tools && d.tools.some((t) => t.on !== tools[t.name].on || (t.on && t.asks !== tools[t.name].asks))) body.tools = tools;
    return body;
  }

  async save(button, error) {
    const body = this.changes();
    if (!Object.keys(body).length) return this.setOpen(false);
    button.disabled = true;
    error.textContent = this.error = "";
    try {
      await change(this.data.id, body);
      this.setOpen(false);
      toast(`Saved ${this.data.title}. Applying…`);
    } catch (e) {
      error.textContent = this.error = e.message;
    } finally {
      button.disabled = !this.editable;
    }
  }

  async removeSecret(s) {
    if (!(await ask({ title: `Remove the ${s.label.toLowerCase()}?`, body: [h("p", { text: "It's deleted from .env on the Pi." })], confirm: "Remove", danger: true }))) return;
    try {
      await change(this.data.id, { secrets: { [s.name]: null } });
      toast("Removed. Applying…");
    } catch (e) {
      toast(e.message, true);
    }
  }

  async remove() {
    const d = this.data;
    if (!(await ask({ title: `Remove ${d.title}?`, body: [h("p", { text: "It's taken out of config.toml. Any keys it had stay in .env." })], confirm: "Remove", danger: true }))) return;
    try {
      showTools(await call("DELETE", `/api/tools/${encodeURIComponent(d.id)}`));
      toast(`Removed ${d.title}.`);
    } catch (e) {
      toast(e.message, true);
    }
  }
}

// A form for an MCP server that isn't one of the recommended ones.
class AddServer {
  constructor() {
    this.el = h("section.card");
    this.collapsed();
  }

  collapsed() {
    this.el.replaceChildren(
      h(
        "div.tools-intro",
        { style: { margin: "0" } },
        h(
          "p",
          {},
          "Most servers are in the ",
          h("a", { href: "https://registry.modelcontextprotocol.io", target: "_blank", rel: "noopener noreferrer", text: "MCP Registry" }),
          ". Choose carefully: a server sees what Athena sends it, and can act for you."
        ),
        h("button.button", { type: "button", text: "Add a server…", onclick: () => this.expanded() })
      )
    );
  }

  expanded() {
    const v = { name: "", runs: "command", command: "uvx", args: "", url: "", env: [], headers: [] };
    const error = h("p.form-error", { role: "alert" });
    const where = h("div.add-form");
    const name = h("input", { type: "text", placeholder: "weather", autocapitalize: "none", spellcheck: false, oninput: () => (v.name = name.value) });
    const rowsEditor = (list, columns, noun) => {
      const box = h("div.map");
      const draw = () =>
        box.replaceChildren(
          ...list.map((row, i) => {
            const key = h("input", { type: "text", value: row.key, placeholder: columns[0], "aria-label": columns[0], autocapitalize: "none", spellcheck: false, oninput: () => (row.key = key.value) });
            const value = h("input", { type: row.secret ? "password" : "text", value: row.value, placeholder: columns[1], "aria-label": columns[1], autocapitalize: "none", spellcheck: false, autocomplete: "off", oninput: () => (row.value = value.value) });
            return h(
              "div.map-row.with-secret",
              {},
              key,
              value,
              h("label.check", {}, h("input", { type: "checkbox", checked: row.secret, onchange: (e) => ((row.secret = e.target.checked), draw()) }), "Secret"),
              h("button.button.small.remove", { type: "button", text: "×", "aria-label": "Remove", onclick: () => (list.splice(i, 1), draw()) })
            );
          }),
          h("div", {}, h("button.button.small", { type: "button", text: `Add ${noun}`, onclick: () => (list.push({ key: "", value: "", secret: false }), draw()) }))
        );
      draw();
      return box;
    };
    const drawWhere = () => {
      if (v.runs === "command") {
        const command = h("input", { type: "text", value: v.command, autocapitalize: "none", spellcheck: false, oninput: () => (v.command = command.value) });
        const args = h("textarea", { rows: 3, placeholder: "some-weather-mcp==1.2.3", autocapitalize: "none", spellcheck: false, oninput: () => (v.args = args.value) });
        args.value = v.args;
        where.replaceChildren(
          h("label.field", {}, h("span.field-label", { text: "Command" }), h("span.field-help", { text: "uvx for Python servers from PyPI, or npx for npm ones, which need Node.js on the Pi." }), command),
          h("label.field", {}, h("span.field-label", { text: "Arguments" }), h("span.field-help", { text: "One per line. Pin a version, like name==1.2.3, so an update can't change what runs." }), args),
          h("div.field", {}, h("span.field-label", { text: "Environment" }), h("span.field-help", { text: "Settings it reads, like an API key. Tick Secret to keep a value in .env." }), rowsEditor(v.env, ["Name", "Value"], "a variable"))
        );
      } else {
        const url = h("input", { type: "url", value: v.url, placeholder: "https://example.com/mcp", autocapitalize: "none", spellcheck: false, oninput: () => (v.url = url.value) });
        where.replaceChildren(
          h("label.field", {}, h("span.field-label", { text: "URL" }), url),
          h("div.field", {}, h("span.field-label", { text: "Headers" }), h("span.field-help", { text: "Such as Authorization. Tick Secret to keep a value in .env." }), rowsEditor(v.headers, ["Header", "Value"], "a header"))
        );
      }
    };
    const runs = h(
      "div.choice",
      { role: "radiogroup", "aria-label": "Where it runs" },
      [
        ["command", "On the Pi"],
        ["url", "On the internet"],
      ].map(([value, label]) =>
        h("label", {}, h("input", { type: "radio", name: "runs", value, checked: v.runs === value, onchange: () => ((v.runs = value), drawWhere()) }), label)
      )
    );
    drawWhere();
    const add = h("button.button.primary", {
      type: "button",
      text: "Add server",
      onclick: async () => {
        const map = (rows) => Object.fromEntries(rows.filter((r) => r.key.trim()).map((r) => [r.key.trim(), r.secret ? { secret: r.value } : r.value]));
        const body = { name: v.name.trim() };
        if (v.runs === "command") Object.assign(body, { command: v.command.trim(), args: v.args.split("\n").map((a) => a.trim()).filter(Boolean), env: map(v.env) });
        else Object.assign(body, { url: v.url.trim(), headers: map(v.headers) });
        add.disabled = true;
        error.textContent = "";
        try {
          showTools(await call("POST", "/api/tools", body));
          toast(`Added ${body.name}. Connecting…`);
          this.collapsed();
        } catch (e) {
          error.textContent = e.message;
        } finally {
          add.disabled = false;
        }
      },
    });
    this.el.replaceChildren(
      h(
        "div.add-form",
        {},
        h("label.field", {}, h("span.field-label", { text: "Name" }), h("span.field-help", { text: "Lower-case letters, digits, - or _." }), name),
        h("div.field", {}, h("span.field-label", { text: "Where it runs" }), runs),
        where,
        h("p.note", {}, icon("lock"), h("span", { text: "Every tool it has asks first, until you choose otherwise once it's connected." })),
        error,
        h("div.tool-actions", {}, add, h("button.button.quiet", { type: "button", text: "Cancel", onclick: () => this.collapsed() }))
      )
    );
    name.focus();
  }
}

// -- dialogs and toasts ---------------------------------------------------------------------------------

function ask({ title, body, confirm, danger, ready, watch }) {
  const dialog = document.getElementById("dialog");
  return new Promise((resolve) => {
    const ok = h(`button.button.${danger ? "danger.solid" : "primary"}`, { type: "button", text: confirm, disabled: ready ? !ready() : false });
    const cancel = h("button.button", { type: "button", text: "Cancel" });
    const done = (answer) => {
      resolve(answer);
      if (dialog.open) dialog.close();
    };
    ok.addEventListener("click", () => done(true));
    cancel.addEventListener("click", () => done(false));
    if (watch) watch.addEventListener("input", () => (ok.disabled = !ready()));
    dialog.onclose = () => resolve(false);
    dialog.replaceChildren(h("h2", { text: title }), ...body, h("div.tool-actions", {}, cancel, ok));
    dialog.showModal();
    (watch || cancel).focus();
  });
}

let toastTimer = null;
function toast(message, error = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("error", error);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), error ? 6000 : 3500);
}

start();
