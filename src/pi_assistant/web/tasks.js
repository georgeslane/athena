// The Tasks tab: what Athena does by itself.

import { ask, call, clock, day, h, icon, plural, state, toast } from "./core.js";

// -- tasks: what Athena does by itself ---------------------------------------------------------------

const tasks_ = { timer: null };
const TASK_STATES = { active: "Scheduled", paused: "Paused", done: "Done", running: "Working on it" };

export async function loadTasks() {
  try {
    state.tasks = await call("GET", "/api/tasks");
  } catch (e) {
    if (e.status !== 409) throw e;
    state.tasks = { off: e.message };
  }
  renderTasks();
}

function renderTasks() {
  const t = state.tasks;
  const panel = document.getElementById("tab-tasks");
  clearTimeout(tasks_.timer);
  if (t.off) {
    panel.replaceChildren(h("section.card", {}, h("p.note.setup", {}, icon("info"), h("span", { text: t.off }))));
    return;
  }
  const intro = h(
    "p.note",
    {},
    icon("info"),
    h("span", {
      text:
        `Athena works on these by itself when they're due, but never while you're chatting: it waits until you haven't ` +
        `messaged for ${plural(t.quiet_minutes, "minute")}, and stops for you while it works. When it finds something, it messages you in Telegram.`,
    })
  );
  const list = t.tasks.length
    ? h("div.task-list", {}, t.tasks.map(taskCard))
    : h("p.empty", { text: "No tasks yet. Add one here, or ask Athena in Telegram, such as “every weekday at 7:30, send me a briefing”." });
  panel.replaceChildren(
    h(
      "div.stack",
      {},
      h(
        "section.card",
        {},
        h("div.card-head", {}, h("h2", { text: "Tasks" }), h("span.aside", { text: "What Athena does by itself" }), h("div.actions", {}, h("button.button.small.primary", { type: "button", text: "New task", onclick: () => newTask() }))),
        intro,
        list
      ),
      watchingCard(t.watching)
    )
  );
  // While one is running or due, or Athena's checking what it's watching, follow how it's going.
  if (t.tasks.some((task) => task.status === "running" || task.waiting_for_you) || t.watching.checking || t.watching.asked) {
    tasks_.timer = setTimeout(() => location.hash === "#tasks" && loadTasks().catch(() => {}), 3000);
  }
}

function taskCard(task) {
  const status = task.waiting_for_you ? "Waiting for you" : TASK_STATES[task.status] || task.status;
  let next = "";
  if (task.status === "running") next = "Working on it now";
  else if (task.waiting_for_you) next = "Due: waiting until you've finished chatting";
  else if (task.next_run) next = `Next ${clock(task.next_run, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`;
  const actions = [];
  if (task.status === "running") actions.push(h("button.button.small", { type: "button", text: "Stop", onclick: (e) => taskAction(e, task, "stop") }));
  else actions.push(h("button.button.small", { type: "button", text: "Run now", onclick: (e) => taskAction(e, task, "run") }));
  if (task.status === "active") actions.push(h("button.button.small", { type: "button", text: "Pause", onclick: (e) => taskAction(e, task, "pause") }));
  else if (task.status !== "running") actions.push(h("button.button.small", { type: "button", text: "Resume", onclick: (e) => taskAction(e, task, "resume") }));
  actions.push(h("button.button.small.danger", { type: "button", text: "Delete", onclick: () => deleteTask(task) }));
  const last = task.last_run
    ? h(
        "details.task-last",
        {},
        h("summary", { text: `Last run ${clock(task.last_run, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}: ${task.last_outcome}` }),
        task.last_result ? h("p.task-said", { text: task.last_result }) : h("p.hint", { text: "It didn't say anything." })
      )
    : task.last_outcome
      ? h("p.hint", { text: `Last try: ${task.last_outcome}` })
      : null;
  return h(
    "article.task",
    { "data-status": task.waiting_for_you ? "waiting" : task.status },
    h("div.task-head", {}, h("h3", { text: task.title }), h("span.task-state", { text: status })),
    h("p.task-when", { text: [task.when.replace(/^./, (c) => c.toUpperCase()), next].filter(Boolean).join(" · ") }),
    h("p.task-instructions", { text: task.instructions }),
    task.allowed_tools.length
      ? h("div.task-tools", {}, h("span.hint", { text: "Uses without asking:" }), h("div.chips", {}, task.allowed_tools.map((name) => h("span.chip", { text: name }))))
      : null,
    task.created_by === "athena" ? h("p.hint", { text: "Suggested by Athena, and allowed by you." }) : null,
    task.event ? trigger(task.event) : null,
    last,
    h("div.tool-actions", {}, actions)
  );
}

// How to start a task that runs on an event: the address to call, and with what.
function trigger(event) {
  const on = state.tasks.triggers.on;
  return h(
    "details.task-last",
    {},
    h("summary", { text: on ? `Started by POST ${location.origin}/hooks/${event}` : "Triggers are off: set TRIGGERS_TOKEN in .env on the Pi" }),
    h("p.hint", {
      text:
        `In an iPhone Shortcuts automation, such as “When I arrive home”, add “Get contents of URL”: ${location.origin}/hooks/${event}, ` +
        "method POST, with a header Authorization: Bearer and your TRIGGERS_TOKEN. Add a JSON body with “text” to pass on anything that helps. " +
        "Set the automation to run immediately. See the README, “Triggers”.",
    })
  );
}

// Things Athena keeps an eye on for you, checked together now and then.
function watchingCard(w) {
  const day_ = (ts) => clock(ts, { weekday: "short", day: "numeric", month: "short" });
  const time_ = (ts) => clock(ts, { hour: "2-digit", minute: "2-digit" });
  let about;
  if (!w.every_minutes) about = "Checking is off: [tasks] heartbeat_minutes is 0.";
  else if (w.checking) about = "Checking now.";
  else if (w.asked) about = "Athena will check as soon as it's free.";
  else {
    const parts = [`Athena checks on all of these every ${plural(w.every_minutes, "minute")} when you're not chatting, and messages you only when something needs your attention.`];
    if (w.last) parts.push(`Last checked at ${time_(w.last.at)}: ${w.last.outcome}.`);
    if (w.next) parts.push(`Next at about ${time_(Math.max(w.next, Date.now() / 1000))}.`);
    about = parts.join(" ");
  }
  const items = w.items.length
    ? h(
        "div.task-list",
        {},
        w.items.map((item) =>
          h(
            "article.task",
            { "data-status": "active" },
            h("p.task-instructions", { text: item.what }),
            h("p.hint", { text: `Since ${day_(item.created)}, until ${day_(item.until)} · asked for by ${item.by === "you" ? "you" : "Athena, when you asked"}` }),
            item.note ? h("p.task-said", { text: `Athena's note: ${item.note}` }) : null,
            h("div.tool-actions", {}, h("button.button.small.danger", { type: "button", text: "Stop watching", onclick: (e) => stopWatching(e, item) }))
          )
        )
      )
    : h("p.empty", { text: "Nothing yet. Ask Athena, such as “let me know when Claude finishes the bank project”, or add something here." });
  return h(
    "section.card",
    {},
    h(
      "div.card-head",
      {},
      h("h2", { text: "Watching" }),
      h("span.aside", { text: "What Athena keeps an eye on" }),
      h(
        "div.actions",
        {},
        h("button.button.small", { type: "button", text: "Check now", disabled: !w.items.length || w.checking || !w.every_minutes, onclick: checkNow }),
        h("button.button.small.primary", { type: "button", text: "Watch for…", onclick: newWatch })
      )
    ),
    h("p.note", {}, icon("info"), h("span", { text: about })),
    items
  );
}

async function checkNow(event) {
  event.target.disabled = true;
  try {
    state.tasks = await call("POST", "/api/watches/check", {});
    renderTasks();
    toast("Athena will check as soon as it's free.");
  } catch (e) {
    toast(e.message, true);
    event.target.disabled = false;
  }
}

async function stopWatching(event, item) {
  event.target.disabled = true;
  try {
    state.tasks = await call("DELETE", `/api/watches/${item.id}`);
    renderTasks();
    toast("Stopped watching it.");
  } catch (e) {
    toast(e.message, true);
    event.target.disabled = false;
  }
}

function newWatch() {
  const what = h("textarea", { required: true, rows: 3, maxLength: 300, placeholder: "When the parcel from Amazon has been delivered" });
  const days = h("input", { type: "number", min: 1, max: 90, value: 14, required: true });
  const error = h("p.form-error", { role: "alert" });
  const dialog = document.getElementById("dialog");
  const save = h("button.button.primary", { type: "submit", text: "Watch" });
  const form = h(
    "form.task-form",
    {
      onsubmit: async (event) => {
        event.preventDefault();
        save.disabled = true;
        error.textContent = "";
        try {
          state.tasks = await call("POST", "/api/watches", { what: what.value, days: Number(days.value) });
          dialog.close();
          renderTasks();
          toast("Watching it.");
        } catch (e) {
          error.textContent = e.message;
          save.disabled = false;
        }
      },
    },
    h("h2", { text: "Watch for something" }),
    h("label.field", {}, h("span.field-label", { text: "What to watch for" }), h("span.field-help", { text: "And when to tell you. Athena checks with the tools it has, such as your email, Claude's board or market prices." }), what),
    h("label.field", {}, h("span.field-label", { text: "For how many days" }), days),
    error,
    h("div.tool-actions", {}, h("button.button", { type: "button", text: "Cancel", onclick: () => dialog.close() }), save)
  );
  dialog.onclose = null;
  dialog.replaceChildren(form);
  if (!dialog.open) dialog.showModal();
  what.focus();
}

async function taskAction(event, task, action) {
  event.target.disabled = true;
  try {
    state.tasks = await call("POST", `/api/tasks/${task.id}`, { action });
    renderTasks();
    toast({ run: `“${task.title}” will run now.`, pause: "Paused.", resume: "Resumed.", stop: "Stopped." }[action]);
  } catch (e) {
    toast(e.message, true);
    event.target.disabled = false;
  }
}

async function deleteTask(task) {
  const ok = await ask({ title: `Delete “${task.title}”?`, body: [h("p", { text: "Athena won't do it any more. Anything it said is still in your chat." })], confirm: "Delete", danger: true });
  if (!ok) return;
  try {
    state.tasks = await call("DELETE", `/api/tasks/${task.id}`);
    renderTasks();
  } catch (e) {
    toast(e.message, true);
  }
}

function newTask(example) {
  const t = state.tasks;
  const title = h("input", { type: "text", maxLength: 120, required: true, value: example ? example.title : "" });
  const instructions = h("textarea", { required: true, rows: 5, value: example ? example.instructions : "" });
  instructions.value = example ? example.instructions : "";
  const schedule = h("input", { type: "text", required: true, value: example ? example.schedule : "", placeholder: "weekdays 07:30", autocapitalize: "none", spellcheck: false });
  const tools = t.tools.map((name) => h("label", {}, h("input", { type: "checkbox", value: name }), h("span", { text: name })));
  const error = h("p.form-error", { role: "alert" });
  const dialog = document.getElementById("dialog");
  const save = h("button.button.primary", { type: "submit", text: "Add task" });
  const form = h(
    "form.task-form",
    {
      onsubmit: async (event) => {
        event.preventDefault();
        save.disabled = true;
        error.textContent = "";
        try {
          state.tasks = await call("POST", "/api/tasks", {
            title: title.value,
            instructions: instructions.value,
            schedule: schedule.value,
            tools: tools.map((label) => label.querySelector("input")).filter((box) => box.checked).map((box) => box.value),
          });
          dialog.close();
          renderTasks();
          toast(`Added “${title.value.trim()}”.`);
        } catch (e) {
          error.textContent = e.message;
          save.disabled = false;
        }
      },
    },
    h("h2", { text: "New task" }),
    example
      ? null
      : h(
          "div.task-examples",
          {},
          h("span.hint", { text: "Start from:" }),
          t.examples.map((ex) => h("button.button.small", { type: "button", text: ex.title, onclick: () => newTask(ex) }))
        ),
    h("label.field", {}, h("span.field-label", { text: "Title" }), title),
    h("label.field", {}, h("span.field-label", { text: "What to do" }), h("span.field-help", { text: "Instructions Athena follows on its own, and what to tell you." }), instructions),
    h("label.field", {}, h("span.field-label", { text: "When" }), h("span.field-help", { text: `Such as ${t.schedules}.` }), schedule),
    tools.length
      ? h("fieldset.field", {}, h("legend.field-label", { text: "May use without asking" }), h("span.field-help", { text: "These normally ask you first. Anything not ticked still asks, in Telegram." }), h("div.choice", {}, tools))
      : null,
    error,
    h("div.tool-actions", {}, h("button.button", { type: "button", text: "Cancel", onclick: () => dialog.close() }), save)
  );
  dialog.onclose = null;
  dialog.replaceChildren(form);
  if (!dialog.open) dialog.showModal();
  title.focus();
}
