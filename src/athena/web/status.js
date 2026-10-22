// The Status tab: what Athena is doing, how much it's used, and its connections.

import { ago, ask, call, clock, day, duration, h, now, number, plural, seconds, state, toast, when } from "./core.js";
import { signOut } from "./dashboard.js";

// -- the Status tab ---------------------------------------------------------------------------------------

const LABELS = { idle: "Idle", working: "Working", approval: "Needs you", offline: "Offline" };

function look() {
  return state.offline ? "offline" : state.status.status.state;
}

function shieldLook() {
  const s = state.status.status;
  return !state.offline && s.state === "idle" && s.last_error ? "failed" : look();
}

export function renderStatus() {
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
      sub = "Is the athena service running?";
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
      task = "Its log on the Pi says why: journalctl -u athena -f";
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

export function renderOverview() {
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
    h("div.stack", {}, now_.el || now_.build(), usage, h("div.grid.two", {}, memoryCard(o), connectionsCard(o)))
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
        ? "What Athena reads with your next message: its instructions, its tools and this session's conversation." +
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
      metric(number(m.documents), m.documents === 1 ? "Document" : "Documents"),
      metric(number(m.conversations), m.conversations === 1 ? "Exchange" : "Exchanges")
    ),
    h("p.hint", {
      style: { marginTop: "10px" },
      text: "Exchanges are your messages and Athena's replies, kept so it can search past conversations.",
    }),
    h("div.tool-actions", { style: { marginTop: "10px" } }, h("a.button.small", { href: "#memory", text: "Search and documents" })),
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
      h("li", {}, h("span.what", { text: "Thinking" }), thinkingSwitch(model)),
      h("li", {}, h("span.what", { text: "Model server" }), server),
      h("li", {}, h("span.what", { text: "Context limit" }), h("span.is", { text: model.context_window ? `${number(model.context_window)} tokens` : "Unknown" })),
      h(
        "li",
        {},
        h("span.what", { text: "Tools" }),
        h("span.is", { text: tools.applying ? "Applying changes…" : `${plural(tools.count, "tool")}, ${servers}` }),
        h("a.button.small.end", { href: "#tools", text: "Manage" })
      ),
      h("li", {}, h("span.what", { text: "Dashboard" }), h("span.is", { text: "Signed in" }), h("button.button.small.end", { type: "button", text: "Sign out", onclick: signOut }))
    )
  );
}

const THINKING = {
  on: ["On", "Thinks things through before answering, for as long as it takes."],
  short: ["Short", "Thinks briefly before answering, for up to {tokens} tokens."],
  off: ["Off", "Answers straight away, without thinking first."],
};

function thinkingSwitch(model) {
  return h(
    "div.switcher.end",
    { role: "group", "aria-label": "Thinking before answering" },
    Object.entries(THINKING).map(([mode, [label, says]]) =>
      h("button", {
        type: "button",
        text: label,
        title: says.replace("{tokens}", number(model.short_thinking_tokens)),
        "aria-pressed": String(model.thinking === mode),
        onclick: (event) => setThinking(event, mode),
      })
    )
  );
}

async function setThinking(event, mode) {
  if (state.overview.model.thinking === mode) return;
  for (const button of event.target.parentElement.children) button.disabled = true;
  try {
    state.overview = await call("POST", "/api/thinking", { mode });
    renderOverview();
    const [label, says] = THINKING[mode];
    toast(`Thinking ${label.toLowerCase()}. ${state.overview.assistant.name} ${says.replace("{tokens}", number(state.overview.model.short_thinking_tokens)).replace(/^./, (c) => c.toLowerCase())}`);
  } catch (e) {
    toast(e.message, true);
    for (const button of event.target.parentElement.children) button.disabled = false;
  }
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
        m.documents ? h("li", { text: `What's indexed of ${plural(m.documents, "document")}, which indexing again can add back` }) : null,
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
