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
      )
    )
  );
  // While one is running or due, follow how it's going.
  if (t.tasks.some((task) => task.status === "running" || task.waiting_for_you)) {
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
    last,
    h("div.tool-actions", {}, actions)
  );
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
