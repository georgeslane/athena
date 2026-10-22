// An agent's DAG, on the Agents tab: when it runs, running it now, pausing it; changes its runs propose, to approve
// or reject; its file, in Airflow-style Python that Athena reads but never runs, checked as you write it; and how
// it works, drawn (graph.js).

import { ask, call, clock, h, icon, state, toast } from "./core.js";
import { graph } from "./graph.js";

const CHECK_MS = 300; // after you stop typing, the file's checked
const dag_ = {
  id: null, // the agent shown
  data: null, // GET /api/agents/<id>/dag
  shown: "", // what was drawn last, so it's drawn again only when something's changed
  checked: null, // what the server said of the file as it's written now: {dag, error, unknown}
  timer: null,
  els: null,
};

const drafts = () => state.agents.drafts;
const draftKey = () => `${dag_.id}/dag`;

export async function dagView(box, id, fresh) {
  if (fresh || dag_.id !== id || !dag_.els || !box.contains(dag_.els.root)) {
    dag_.id = id;
    dag_.data = null;
    dag_.checked = null;
    dag_.shown = "";
    build(box);
  }
  let data;
  try {
    data = await call("GET", `/api/agents/${id}/dag`);
  } catch (err) {
    toast(err.message, true);
    return;
  }
  if (dag_.id !== id) return; // another agent's been chosen meanwhile
  const now_ = JSON.stringify(data);
  if (now_ === dag_.shown && !fresh) return; // as it was: nothing to draw again
  dag_.shown = now_;
  dag_.data = data;
  render();
}

function build(box) {
  const e = (dag_.els = {});
  e.summary = h("section.card.dag-summary");
  e.proposals = h("div.proposals");
  e.lines = h("pre.code-lines", { "aria-hidden": "true" });
  e.text = h("textarea.code-text", { spellcheck: false, autocapitalize: "off", autocomplete: "off", wrap: "off", "aria-label": "The DAG's file", oninput: edited, onkeydown: keys, onscroll: () => (e.lines.scrollTop = e.text.scrollTop) });
  e.problem = h("p.code-problem", { role: "status" });
  e.save = h("button.button.primary", { type: "button", text: "Save", onclick: save });
  e.revert = h("button.button", { type: "button", text: "Undo changes", onclick: revert });
  e.editor = h(
    "section.card.dag-editor",
    {},
    h("div.card-head", {}, h("h2", {}, "dag.py"), h("span.aside", { text: "Airflow-style Python: read, never run" })),
    h("div.code", {}, e.lines, e.text),
    e.problem,
    h("div.tool-actions", {}, help(), h("span.grow"), e.revert, e.save)
  );
  e.drawing = h("div.dag-drawing");
  e.graph = h("section.card.dag-graph", {}, h("div.card-head", {}, h("h2", { text: "How it works" }), h("span.aside", { text: "Each task after those it comes after" })), e.drawing);
  e.root = h("div.dag-view", {}, e.summary, e.proposals, h("div.dag-layout", {}, e.editor, e.graph));
  box.replaceChildren(e.root);
}

function render() {
  const d = dag_.data;
  const e = dag_.els;
  // What you've written stays, until you save it or undo it; otherwise the file's as it's saved.
  const draft = drafts().get(draftKey());
  if (draft === undefined && e.text.value !== d.source && document.activeElement !== e.text) {
    e.text.value = d.source;
    dag_.checked = null;
  } else if (draft !== undefined && e.text.value !== draft) {
    e.text.value = draft;
  }
  renderSummary();
  renderProposals();
  numbered();
  if (dag_.checked === null) check();
  renderChecked();
}

function renderSummary() {
  const d = dag_.data;
  const spec = d.dag;
  const lines = [];
  if (!d.enabled) lines.push(h("p.note", {}, icon("info"), h("span", { text: "DAGs are switched off: [tasks] enabled = false in config.toml." })));
  const when = spec ? spec.when : "Its file has a problem: fix it below.";
  const parts = [d.written ? `Runs ${when}` : "Not saved yet: write what it should do, and save it."];
  if (d.paused) parts.push("paused");
  else if (d.next_run) parts.push(`next ${clock(d.next_run, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`);
  if (d.running) parts.push(`running now${d.running.doing ? `, on ${d.running.doing}` : ""}`);
  else if (d.queued.length) parts.push("waiting to run");
  const last = d.last ? h("a.dag-last", { href: `#agents/${dag_.id}/runs/${d.last.id}`, text: `Last run #${d.last.number}: ${d.last.state}${d.last.finished ? `, ${clock(d.last.finished, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}` : ""}` }) : null;
  const buttons = [];
  if (d.running) buttons.push(h("button.button.small", { type: "button", text: "Stop", onclick: (event) => act(event, "stop") }));
  else buttons.push(h("button.button.small.primary", { type: "button", text: "Run now", disabled: !d.written || !spec || d.queued.includes("manual"), onclick: (event) => act(event, "run") }));
  if (d.written) buttons.push(h("button.button.small", { type: "button", text: d.paused ? "Resume" : "Pause", onclick: (event) => act(event, d.paused ? "resume" : "pause") }));
  dag_.els.summary.replaceChildren(
    h("div.card-head", {}, h("h2", { text: "When it runs" }), h("div.actions", {}, buttons)),
    h("p.dag-when", { text: parts.join(" · ") }),
    last,
    ...lines
  );
}

function renderProposals() {
  const list = dag_.data.proposals;
  dag_.els.proposals.replaceChildren(...list.map((p) => proposalCard(p, decided)));
}

// A change a run proposed, to approve or reject: here, and on the run's History.
export function proposalCard(p, then) {
  const title = p.kind === "workflow" ? "A change to its tasks" : "Its tools";
  const body = p.kind === "workflow" ? diffView(p.diff || "") : h("p.proposal-tools", { text: p.summary || "" });
  const actions =
    p.state === "pending"
      ? h(
          "div.tool-actions",
          {},
          h("button.button.small", { type: "button", text: "Reject", onclick: (event) => decide(event, p, false, then) }),
          h("button.button.small.primary", { type: "button", text: "Approve", onclick: (event) => decide(event, p, true, then) })
        )
      : h("p.hint", { text: { approved: "Approved.", rejected: "Rejected.", stale: "Out of date: the DAG changed since." }[p.state] || p.state });
  return h(
    "section.card.proposal",
    { "data-state": p.state },
    h("div.card-head", {}, h("h2", { text: title }), h("span.aside", { text: p.run ? `Proposed by a run, ${clock(p.created, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}` : "" })),
    p.why ? h("p", { text: p.why }) : null,
    body,
    actions
  );
}

function diffView(diff) {
  const lines = diff.split("\n").filter((line) => !line.startsWith("---") && !line.startsWith("+++"));
  return h(
    "pre.diff",
    {},
    lines.map((line) => h(`span.diff-line${line.startsWith("+") ? ".added" : line.startsWith("-") ? ".removed" : line.startsWith("@@") ? ".where" : ""}`, { text: `${line}\n` }))
  );
}

async function decide(event, proposal, approve, then) {
  for (const b of event.target.parentElement.querySelectorAll("button")) b.disabled = true;
  try {
    const data = await call("POST", `/api/proposals/${proposal.id}`, { approve });
    toast(approve ? "Approved: the DAG's changed." : "Rejected.");
    then(data);
  } catch (err) {
    toast(err.message, true);
    then(null);
  }
}

function decided(data) {
  if (data) {
    if (drafts().get(draftKey()) === undefined) dag_.checked = null; // the file's changed: check it again
    dag_.data = data;
    render();
  } else {
    dagView(dag_.els.root.parentElement, dag_.id, false);
  }
}

// -- the file ------------------------------------------------------------------------------------------------

function numbered() {
  const e = dag_.els;
  const count = e.text.value.split("\n").length;
  const bad = dag_.checked && dag_.checked.error ? dag_.checked.error.line : 0;
  e.lines.replaceChildren(...Array.from({ length: count }, (_, i) => h(`span${i + 1 === bad ? ".bad" : ""}`, { text: `${i + 1}\n` })));
  e.lines.scrollTop = e.text.scrollTop;
}

function edited() {
  const e = dag_.els;
  if (e.text.value === dag_.data.source && dag_.data.written) drafts().delete(draftKey());
  else drafts().set(draftKey(), e.text.value);
  numbered();
  renderButtons();
  clearTimeout(dag_.timer);
  dag_.timer = setTimeout(check, CHECK_MS);
}

async function check() {
  const id = dag_.id;
  const source = dag_.els.text.value;
  try {
    const checked = await call("POST", `/api/agents/${id}/dag/check`, { source });
    if (dag_.id !== id || dag_.els.text.value !== source) return; // it's changed since
    dag_.checked = checked;
  } catch (err) {
    toast(err.message, true);
    return;
  }
  numbered();
  renderChecked();
}

function renderChecked() {
  const e = dag_.els;
  const c = dag_.checked;
  if (c && c.error) {
    e.problem.textContent = c.error.line ? `Line ${c.error.line}: ${c.error.message}` : c.error.message;
    e.problem.dataset.kind = "error";
  } else if (c && c.unknown.length) {
    e.problem.textContent = `No tool matches ${c.unknown.join(", ")} now.`;
    e.problem.dataset.kind = "warning";
  } else {
    e.problem.textContent = "";
    delete e.problem.dataset.kind;
  }
  const spec = (c && c.dag) || (!c && dag_.data.dag) || null;
  if (spec) e.drawing.replaceChildren(graph({ tasks: spec.tasks, end: spec.end }));
  e.drawing.classList.toggle("stale", Boolean(c && c.error));
  if (!spec && !e.drawing.firstChild) e.drawing.replaceChildren(h("p.empty", { text: "Once the file reads, its tasks are drawn here." }));
  renderButtons();
}

function renderButtons() {
  const e = dag_.els;
  const dirty = drafts().get(draftKey()) !== undefined;
  e.save.disabled = !dirty || Boolean(dag_.checked && dag_.checked.error);
  e.revert.hidden = !dirty || !dag_.data.written;
}

async function save() {
  const e = dag_.els;
  e.save.disabled = true;
  try {
    dag_.data = await call("POST", `/api/agents/${dag_.id}/dag`, { source: e.text.value });
    drafts().delete(draftKey());
    dag_.checked = null;
    render();
    toast("Saved.");
  } catch (err) {
    toast(err.message, true);
    renderButtons();
  }
}

async function revert() {
  const ok = await ask({ title: "Undo your changes?", body: [h("p", { text: "The file goes back to how it's saved." })], confirm: "Undo them" });
  if (!ok) return;
  drafts().delete(draftKey());
  dag_.els.text.value = dag_.data.source;
  dag_.checked = null;
  render();
}

// Tab indents, and a new line keeps the line's indent, and adds one after a colon.
function keys(event) {
  const text = event.target;
  const { selectionStart: start, selectionEnd: end, value } = text;
  if (event.key === "Tab" && !event.shiftKey) {
    event.preventDefault();
    text.setRangeText("    ", start, end, "end");
    edited();
  } else if (event.key === "Enter" && !event.isComposing) {
    const line = value.slice(value.lastIndexOf("\n", start - 1) + 1, start);
    const indent = line.match(/^\s*/)[0] + (/[:([{]\s*$/.test(line) ? "    " : "");
    if (!indent) return;
    event.preventDefault();
    text.setRangeText(`\n${indent}`, start, end, "end");
    edited();
  } else if ((event.metaKey || event.ctrlKey) && event.key === "s") {
    event.preventDefault();
    if (!dag_.els.save.disabled) save();
  }
}

async function act(event, action) {
  event.target.disabled = true;
  try {
    dag_.data = await call("POST", `/api/agents/${dag_.id}/dag/${action}`, {});
    render();
    toast({ run: "It'll run as soon as Athena's free.", pause: "Paused: it won't run by itself.", resume: "It runs by itself again.", stop: "Stopped." }[action]);
  } catch (err) {
    toast(err.message, true);
    event.target.disabled = false;
  }
}

// How DAG files work, and what this one can use.
function help() {
  const d = () => dag_.data || { triggers: { events: [], watches: [], hooks: false }, tools: [] };
  const open = () => {
    const t = d().triggers;
    const dialog = document.getElementById("dialog");
    const code = (text) => h("pre.code-sample", { text });
    dialog.replaceChildren(
      h("h2", { text: "How a DAG file works" }),
      h("p", { text: "It's Python in the style of Apache Airflow, which Athena reads but never runs: one with DAG(...) block, tasks assigned to names, and >> between them for what comes after what." }),
      code(`from athena import DAG, Task

with DAG(
    schedule="0 9 * * 1-5",        # cron, or None
    triggers=["watch:job-emails"],  # events that start it too
    hidden_tools=["news_*"],        # found only when needed
    allow=["fetch"],                # may run without asking
):
    find = Task(
        task_id="find_jobs",
        prompt="Find new jobs that fit my CV. Trigger apply for each.",
    )
    apply = Task(
        task_id="apply",
        prompt="Apply to this job: {input}",
        triggered=True,  # once for each time find triggers it
        alert=True,      # what it says goes to the chat, and to you
    )
    find >> apply`),
      h("ul.help-list", {}, [
        h("li", { text: "A task runs after all those before it have finished, with what they found to go on. One is skipped if one before it failed, or all were skipped." }),
        h("li", { text: "triggered=True runs it once for each time a task before it calls trigger_task, with what that gives it as {input}, and not otherwise." }),
        h("li", { text: "alert=True puts what it says in this agent's chat, and tells you. The summary at the end always goes there, and tells you when there's news." }),
        h("li", { text: "Every DAG ends with five tasks of its own: reviewing the workflow, the tools and the skills it used, keeping what the next run should know, and the summary. Their proposals wait for you to approve them." }),
      ]),
      h("h3", { text: "What can start it" }),
      h("ul.help-list", {}, [
        h("li", { text: "A schedule, in cron: minute, hour, day of the month, month and day of the week, such as \"30 7 * * 1-5\" for 07:30 on weekdays. At most every 15 minutes." }),
        h("li", { text: `Events: ${t.events.join(", ")}${t.hooks ? ", and any you send to /hooks/<event> on the dashboard's address" : ". Set TRIGGERS_TOKEN in .env for events from iPhone Shortcuts (see docs/agents.md)"}.` }),
        h("li", { text: t.watches.length ? `What's watched, when it happens: ${t.watches.map((w) => `watch:${w.key}`).join(", ")}.` : "What's watched, when it happens: add something on the Watching tab, then use watch:<its key>." }),
      ]),
      h("details", {}, h("summary", { text: `The tools it can name (${d().tools.length})` }), h("p.hint.tool-names", { text: d().tools.join(", ") })),
      h("div.tool-actions", {}, h("button.button.primary", { type: "button", text: "Close", onclick: () => dialog.close() }))
    );
    dialog.onclose = null;
    if (!dialog.open) dialog.showModal();
  };
  return h("button.button.small.quiet", { type: "button", onclick: open }, icon("info"), "How it works");
}
