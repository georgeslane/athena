// An agent's DAG, on the Agents tab, one part below another: when it runs and what starts it, with the address to
// start it from an iPhone Shortcut or a webhook; running it now, pausing it; changes its runs propose, to approve or
// reject; how it works, drawn (graph.js); and its file, in agentic-airflow's Python, which Athena reads but never
// runs, checked as you write it. How DAG files are written is a dialog of its own (reference()). "Full page" fills
// the window with the file and its drawing, side by side (one above the other on a phone), to edit it more easily.

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
const hook = (name) => `${location.origin}/hooks/${name}`;

export async function dagView(box, id, fresh) {
  if (fresh || dag_.id !== id || !dag_.els || !box.contains(dag_.els.root)) {
    full(false);
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
  e.drawing = h("div.dag-drawing");
  const enlarge = () => h("button.button.small.full-toggle", { type: "button", title: "Edit the file and see its drawing, filling the page", onclick: () => full(true) }, icon("expand"), h("span", { text: "Full page" }));
  e.graph = h("section.card.dag-graph", {}, h("div.card-head", {}, h("h2", { text: "How it works" }), h("span.aside", { text: "Each task after those it comes after" }), h("div.actions", {}, enlarge())), e.drawing);
  e.lines = h("pre.code-lines", { "aria-hidden": "true" });
  e.text = h("textarea.code-text", { spellcheck: false, autocapitalize: "off", autocomplete: "off", wrap: "off", "aria-label": "The DAG's file", oninput: edited, onkeydown: keys, onscroll: () => (e.lines.scrollTop = e.text.scrollTop) });
  e.problem = h("p.code-problem", { role: "status" });
  e.save = h("button.button.primary", { type: "button", text: "Save", onclick: save });
  e.revert = h("button.button", { type: "button", text: "Undo changes", onclick: revert });
  e.editor = h(
    "section.card.dag-editor",
    {},
    h("div.card-head", {}, h("h2", {}, "dag.py"), h("span.aside", { text: "agentic-airflow Python: read, never run" }), h("div.actions", {}, guide(), enlarge())),
    h("div.code", {}, e.lines, e.text),
    e.problem,
    h("div.tool-actions", {}, h("span.grow"), e.revert, e.save)
  );
  e.bar = h("div.dag-full-bar", {}, h("h2", { text: "Editing the DAG" }), h("span.aside", { text: "It's drawn again as you write it" }), h("button.button.small", { type: "button", text: "Done", onclick: () => full(false) }));
  e.root = h("div.dag-view", {}, e.bar, e.summary, e.proposals, e.graph, e.editor);
  box.replaceChildren(e.root);
}

// The file and its drawing filling the window, or back in the page.
function full(on) {
  if (!dag_.els) return;
  dag_.els.root.classList.toggle("full", on);
  document.body.classList.toggle("no-scroll", on);
  if (on) dag_.els.text.focus();
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && dag_.els && dag_.els.root.classList.contains("full") && !document.querySelector("dialog[open]")) full(false);
});
window.addEventListener("hashchange", () => full(false)); // somewhere else: the page is as it was

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
  const notes = [];
  if (!d.enabled) notes.push(h("p.note", {}, icon("info"), h("span", { text: "DAGs are switched off: [tasks] enabled = false in config.toml." })));
  const when = spec ? spec.when : "its file has a problem: fix it below";
  const parts = [d.written ? `Runs ${when}` : d.master ? "Not saved yet: write what the master agent keeps an eye on, and save it." : "Not saved yet: write what it should do, and save it."];
  if (d.paused) parts.push("paused");
  else if (d.next_run) parts.push(`next ${clock(d.next_run, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`);
  if (d.running) parts.push(`running now${d.running.doing ? `, on ${d.running.doing}` : ""}`);
  else if (d.queued.length) parts.push("waiting to run");
  const last = d.last ? h("a.dag-last", { href: `#agents/${dag_.id}/runs/${d.last.id}`, text: `Last run #${d.last.number}: ${d.last.state}${d.last.finished ? `, ${clock(d.last.finished, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}` : ""}` }) : null;
  const buttons = [];
  if (d.running) buttons.push(h("button.button.small", { type: "button", text: "Stop", onclick: (event) => act(event, "stop") }));
  else buttons.push(h("button.button.small.primary", { type: "button", text: "Run now", disabled: !d.written || !spec || d.queued.includes("manual"), onclick: (event) => act(event, "run") }));
  if (d.written) buttons.push(h("button.button.small", { type: "button", text: d.paused ? "Resume" : "Pause", onclick: (event) => act(event, d.paused ? "resume" : "pause") }));
  const opened = dag_.els.summary.querySelector("details.how-to-call[open]");
  dag_.els.summary.replaceChildren(
    ...[
      h("div.card-head", {}, h("h2", { text: "When it runs" }), h("div.actions", {}, buttons)),
      h("p.dag-when", { text: parts.join(" · ") }),
      last,
      ...notes,
      ...(spec ? starts(spec, d) : []),
    ].filter(Boolean) // replaceChildren would show a null as "null"
  );
  const how = dag_.els.summary.querySelector("details.how-to-call");
  if (how && opened) how.open = true;
}

// What starts it, besides its cron schedule and you: each webhook, with the address to send it to; each asset, with
// the tasks that update it; and each DAG it follows. Then the assets its own tasks update, and who waits for them.
function starts(spec, d) {
  const events = d.events;
  const rows = spec.schedule.flatMap((entry) => {
    if (entry.kind === "webhook") {
      const where = events.hooks
        ? [h("span.hook-method", { text: "POST" }), h("code.hook-url", {}, ...hook(entry.name).split(/(?<=\/)/).flatMap((part, i) => (i ? [h("wbr"), part] : [part]))), copyButton(hook(entry.name))]
        : [h("span.hint", { text: "From Shortcuts and webhooks once TRIGGERS_TOKEN is set in .env." })];
      return [h("li.dag-trigger", {}, icon("bolt"), h("div.dag-trigger-body", {}, h("div", {}, h("code", { text: entry.name }), h("span.hint", { text: " a webhook" })), h("div.hook", {}, ...where)))];
    }
    if (entry.kind === "asset") {
      const from = (events.sources[entry.name] || []).filter((s) => s.agent !== dag_.id);
      const said = [];
      if (entry.name === events.claude) said.push("when Claude finishes a project you handed it");
      for (const s of from) said.push(h("span", {}, "from ", h("a", { href: `#agents/${s.agent}/dag`, text: s.name }), `'s task ${s.task}`));
      if (!said.length) said.push("no agent's task updates it yet");
      return [h("li.dag-trigger", {}, icon("bolt"), h("div.dag-trigger-body", {}, h("div", {}, h("code", { text: entry.name }), h("span.hint", {}, " when it's updated, ", ...said.flatMap((s, i) => (i ? [" and ", s] : [s]))))))];
    }
    if (entry.kind === "dag") {
      return [h("li.dag-trigger", {}, icon("bolt"), h("div.dag-trigger-body", {}, h("span", {}, "When the DAG ", h("code", { text: entry.dag_id }), ` finishes (${entry.on.join(" or ")})`)))];
    }
    return [];
  });
  const outlets = spec.tasks.flatMap((t) =>
    t.outlets.map((name) => {
      const waiting = events.assets[name] || [];
      const said = waiting.length ? `: the DAG of ${waiting.join(" and ")} starts on it.` : ": no agent's DAG starts on it yet.";
      return h("li.dag-trigger.outlet", {}, icon("send"), h("div.dag-trigger-body", {}, h("span", {}, "Its task ", h("code", { text: t.id }), " updates ", h("code", { text: name }), said)));
    })
  );
  if (!rows.length && !outlets.length) return [];
  const webhook = spec.schedule.find((entry) => entry.kind === "webhook");
  return [h("ul.dag-triggers", { "aria-label": "What starts it" }, ...rows, ...outlets), events.hooks && webhook ? howToCall(webhook.name) : null];
}

function copyButton(text) {
  return h(
    "button.button.quiet.small.copy",
    {
      type: "button",
      "aria-label": "Copy the address",
      title: "Copy the address",
      onclick: async () => {
        try {
          await navigator.clipboard.writeText(text);
          toast("Copied.");
        } catch {
          toast("Couldn't copy it: select it and copy it instead.", true);
        }
      },
    },
    icon("copy")
  );
}

function howToCall(event) {
  return h(
    "details.how-to-call",
    {},
    h("summary", { text: "Start it from an iPhone Shortcut or a webhook" }),
    h("ol.help-list", {}, [
      h("li", { text: "In the Shortcuts app, under Automation, make one such as “When I arrive home”, set to run immediately." }),
      h("li", {}, "Give it one action, ", h("b", { text: "Get Contents of URL" }), ", with the address above, Method POST, and a header ", h("code", { text: "Authorization" }), " of ", h("code", { text: "Bearer " }), " then your TRIGGERS_TOKEN."),
      h("li", {}, "To pass something on, set its Request Body to JSON, with ", h("code", { text: "text" }), ", and ", h("code", { text: "items" }), ": what comes after ", h("code", { text: "Task(\"items\", fn=trigger_items, fan_out=True)" }), " runs once for each."),
    ]),
    h("p.hint", { text: "Anything else on your tailnet can do the same, such as:" }),
    h("pre.code-sample", { text: `curl -X POST ${hook(event)} \\\n  -H "Authorization: Bearer $TRIGGERS_TOKEN" \\\n  -H "Content-Type: application/json" \\\n  -d '{"text": "Home at 18:02", "items": ["milk", "bread"]}'` })
  );
}

function renderProposals() {
  const list = dag_.data.proposals;
  dag_.els.proposals.replaceChildren(...list.map((p) => proposalCard(p, decided)));
  dag_.els.proposals.hidden = !list.length;
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
      : h("p.hint", { text: { approved: "Approved.", rejected: "Rejected.", stale: "Out of date: the DAG changed since.", withdrawn: "Withdrawn: a newer one took its place." }[p.state] || p.state });
  const when = p.created ? `, ${clock(p.created, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}` : "";
  // Tools that ask you first, which it would let run without asking: said plainly, not only in the diff.
  const unasked = p.unasked && p.unasked.length ? h("p.proposal-unasked", { text: `It would let these run without asking you: ${p.unasked.join(", ")}.` }) : null;
  return h(
    "section.card.proposal",
    { "data-state": p.state },
    h("div.card-head", {}, h("h2", { text: title }), h("span.aside", { text: `Proposed by ${p.by || "a run"}${when}` })),
    p.why ? h("p", { text: p.why }) : null,
    unasked,
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
    toast({ run: "It'll run when it's its turn.", pause: "Paused: it won't run by itself.", resume: "It runs by itself again.", stop: "Stopped." }[action]);
  } catch (err) {
    toast(err.message, true);
    event.target.disabled = false;
  }
}

// -- how DAG files are written ----------------------------------------------------------------------------------

function guide() {
  return h("button.button.small.quiet", { type: "button", onclick: reference }, icon("info"), "How to write it");
}

const EXAMPLE = `from agentic_airflow import AgenticBranch, AgenticTask, Asset, Dag, TaskGroup, Webhook
from athena.dags import alert

with Dag(
    "emails",
    prompt="Keep on top of my emails.",
    schedule=["0 8 * * *", Webhook("new-email")],  # cron, in your time zone; and a webhook
    hidden_tools=["news_*"],                         # found only when needed
):
    emails = AgenticTask("emails", prompt="Which of my emails need a reply? One item for each.", return_type="list")
    with TaskGroup("each_email", allowed_tools=["read_email", "draft_email"]) as each_email:
        urgent = AgenticBranch("urgent", prompt="Is this one urgent?", allow_none=True)
        draft = AgenticTask("draft", prompt="Draft a reply to it.", follow_mapping=True, on_complete=alert)
        urgent >> draft
    report = AgenticTask("report", prompt="Say what was drafted.", outlets=[Asset("drafted")])
    emails >> each_email >> report`;

// The reference, as docs/dags.md has it at length: every argument, and what each does.
function reference() {
  const d = dag_.data || { events: { assets: {}, webhooks: {}, sources: {}, claude: "claude-done", hooks: false }, tools: [] };
  const dialog = document.getElementById("dialog");
  const code = (text) => h("pre.code-sample", { text });
  const rows = (items) => h("dl.reference", {}, items.flatMap(([name, said]) => [h("dt", {}, h("code", { text: name })), h("dd", { text: said })]));
  const assets = [...new Set([d.events.claude, ...Object.keys(d.events.assets), ...Object.keys(d.events.sources)])].sort();
  dialog.replaceChildren(
    h(
      "div.reference-dialog",
      {},
      h("h2", { text: "Writing a DAG" }),
      h("p", { text: "A DAG says what an agent does by itself. It's Python for agentic-airflow, in the style of Apache Airflow, which Athena reads but never runs: imports, one with Dag(...) block, tasks, task groups, and >> between them for what comes after what. Values are written out as they are: strings, numbers, True, False, None, lists and dicts of them. Anything else, such as a loop, a variable or another call, is refused, with the line it's on." }),
      code(EXAMPLE),
      h("h3", { text: "The DAG" }),
      rows([
        ["id, prompt", 'Dag("emails", prompt="..."): its id, unique among your agents\' DAGs, and what it\'s for, which every task is told.'],
        ["schedule", 'What starts it: cron, in your time zone ("30 7 * * 1-5" for 07:30 on weekdays, "@hourly", "@daily"; at most every 15 minutes); Webhook("arrive-home"), from an iPhone Shortcut or a webhook; Asset("job-emails"), when another DAG\'s task updates it; DagTrigger("other_dag"), when that DAG\'s run finishes. Asset("claude-done") is when Claude finishes a project you handed it.'],
        ["allowed_tools", "All the tools its tasks may use without asking: patterns such as news_* work. Unset, they may use any tool, asking you first for those that ask. Naming a tool that asks first lets it run without asking; one that isn't named waits for your OK. A task group or a task can have its own, which replaces the DAG's."],
        ["hidden_tools", "Tools its tasks find when they need them, rather than in every prompt, which keeps prompts short."],
        ["model, thinking, skills", "The model its tasks use (the agent's, unless it says), thinking \"on\" or \"off\", and skills to read into every prompt."],
      ]),
      h("h3", { text: "Tasks" }),
      rows([
        ['AgenticTask("id", prompt="...")', "A task for the model: its prompt, with what the tasks before it found. return_type: \"string\", the default; \"list\", a JSON list, whose items what comes after runs once for each (fan_out=False keeps it one list); or \"json\"."],
        ['AgenticBranch("id", prompt="...")', "The model picks which of the tasks after it run; the others are skipped. allow_none=True lets it pick none, so a branch with one task after it is a yes or no. multi_select=True lets it pick several."],
        ['AgenticEvaluator("id", prompt="...")', "Checks what the task before it did, and has it done again, with why, up to max_retries times. The tasks that use it come after the evaluator."],
        ['Task("items", fn=trigger_items, fan_out=True)', "The items that came with what started the run: what comes after runs once for each."],
        ["follow_mapping=True", "A task after one that ran for each item runs once for each too, with that item's; otherwise it runs once, given every item's."],
        ["on_complete=alert", "What it finds is told to you, in Telegram and on your devices, and in the agent's chat and the master agent's."],
        ["outlets", 'Assets it updates with what it finds, such as [Asset("job-emails")]: the DAGs with that asset in their schedule run with it. A list that\'s empty starts nothing.'],
        ["context", "The tasks whose outputs it's given: unset, those right before it."],
      ]),
      h("h3", { text: "Task groups, and what comes after what" }),
      h("ul.help-list", {}, [
        h("li", { text: 'with TaskGroup("each", allowed_tools=[...]) as each: its tasks share its settings; in >>, a group stands for its first tasks, after something, and its last, before something.' }),
        h("li", { text: "a >> b runs b after a; a << b runs a after b; [a, b] >> c runs c after both; chains such as a >> b >> c work too." }),
        h("li", { text: "A task runs once those before it have: it's skipped if one of them failed, or all of them were skipped, or a branch before it didn't pick it." }),
      ]),
      h("h3", { text: "What starts it" }),
      h("ul.help-list", {}, [
        h("li", { text: "Its schedule, Run now above, and run_dag in a chat. Runs take turns, one at a time on each model, and your messages go between their tasks." }),
        h("li", { text: d.events.hooks ? `A webhook or an iPhone Shortcut: POST ${hook("<name>")}, with your TRIGGERS_TOKEN, and {"text": ..., "items": [...]} if there's something to pass on.` : "A webhook or an iPhone Shortcut, once TRIGGERS_TOKEN is set in .env: POST /hooks/<name> on the dashboard's address." }),
        h("li", { text: 'Another agent\'s task: say, the master agent\'s DAG checks your emails every hour, and its task with outlets=[Asset("job-emails")] starts the DAG of an agent with Asset("job-emails") in its schedule, which applies for each job it found.' }),
        h("li", { text: `Assets DAGs know of now: ${assets.join(", ")}.` }),
      ]),
      h("h3", { text: "The end of each run" }),
      h("p", { text: "Every DAG ends with five tasks of its own: reviewing the workflow, the tools and the skills it used, which can propose changes for you to approve; keeping what the next run should know; and a summary, which tells you when there's news. A run that finds nothing, with every list its tasks returned empty and every branch picking none, ends without them, so checking for something often costs little." }),
      h("details", {}, h("summary", { text: `The tools it can name (${d.tools.length})` }), h("p.hint.tool-names", { text: d.tools.join(", ") })),
      h("p.hint", { text: "There's more in docs/dags.md." }),
      h("div.tool-actions", {}, h("button.button.primary", { type: "button", text: "Close", onclick: () => dialog.close() }))
    )
  );
  dialog.onclose = null;
  if (!dialog.open) dialog.showModal();
}
