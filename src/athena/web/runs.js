// The History of an agent's DAG, on the Agents tab: its last 10 runs, and for each, its DAG as it was, drawn with how
// each task went, then what each task was given, said and did, for each item it ran for, and what the run proposed.

import { call, clock, duration, h, plural, toast } from "./core.js";
import { proposalCard } from "./dag.js";
import { graph } from "./graph.js";
import { markdown } from "./md.js";
import { stepList } from "./steps.js";

const STATES = { running: "Running", success: "Done", failed: "Failed", skipped: "Skipped", upstream_failed: "Not run: a task before it failed", stopped: "Stopped" };
const SHORT = { running: "running", success: "done", failed: "failed", skipped: "skipped", upstream_failed: "not run", stopped: "stopped" };
const runs_ = { id: null, runs: [], run: null, selected: null, shown: "", els: null };

export async function runsView(box, id, runId, fresh) {
  if (fresh || runs_.id !== id || !runs_.els || !box.contains(runs_.els.root)) {
    runs_.id = id;
    runs_.run = null;
    runs_.selected = null;
    build(box);
  }
  try {
    runs_.runs = (await call("GET", `/api/agents/${id}/runs`)).runs;
    const which = runId ?? (runs_.runs[0] && runs_.runs[0].id);
    runs_.run = which ? await call("GET", `/api/runs/${which}`) : null;
  } catch (err) {
    if (err.status !== 404) toast(err.message, true);
    runs_.run = null;
  }
  if (runs_.id !== id) return;
  render();
}

function build(box) {
  runs_.shown = "";
  const e = (runs_.els = {});
  e.list = h("ol.run-list", { "aria-label": "Its last runs" });
  e.detail = h("div.run-detail");
  e.root = h("div.runs-view", {}, h("section.card.runs-card", {}, h("div.card-head", {}, h("h2", { text: "Runs" }), h("span.aside", { text: "The last 10" })), e.list), e.detail);
  box.replaceChildren(e.root);
}

// Drawn again only when something's changed, as it's asked for again whenever Athena's status changes, keeping
// what you've opened open.
function render(always = false) {
  const e = runs_.els;
  const now_ = JSON.stringify([runs_.runs, runs_.run, runs_.selected]);
  if (!always && now_ === runs_.shown) return;
  runs_.shown = now_;
  const opened = new Set([...e.detail.querySelectorAll("details[data-keep][open]")].map((d) => d.dataset.keep));
  const shown = runs_.run && runs_.run.id;
  e.list.replaceChildren(
    ...(runs_.runs.length
      ? runs_.runs.map((r) =>
          h(
            "li",
            {},
            h(
              "a.run-row",
              { href: `#agents/${runs_.id}/runs/${r.id}`, "aria-current": r.id === shown ? "true" : null, "data-state": r.state },
              h("span.run-number", { text: `#${r.number}` }),
              h("span.run-when", { text: clock(r.started, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) }),
              h("span.run-state", { text: STATES[r.state] || r.state }),
              h("span.run-meta", { text: [trigger(r.trigger), took(r)].filter(Boolean).join(" · ") })
            )
          )
        )
      : [h("li.empty", { text: "It hasn't run yet. Run it from its DAG view, or give it a schedule." })])
  );
  e.detail.replaceChildren(...(runs_.run ? detail(runs_.run) : []));
  for (const d of e.detail.querySelectorAll("details[data-keep]")) if (opened.has(d.dataset.keep)) d.open = true;
}

function detail(run) {
  // How each task went: running while any of its runs is, done if any of them was, or as its first went; and for a
  // task that ran for each item, how many went each way.
  const states = {};
  for (const t of run.tasks) (states[t.task] ||= { all: [] }).all.push(t.state);
  for (const s of Object.values(states)) {
    s.count = s.all.length;
    s.state = s.all.includes("running") ? "running" : s.all.includes("success") ? "success" : s.all.includes("failed") ? "failed" : s.all[0];
    if (s.count > 1) {
      const counted = {};
      for (const state of s.all) counted[state] = (counted[state] || 0) + 1;
      s.states = Object.entries(counted).map(([state, n]) => `${n} ${SHORT[state] || state}`).join(", ");
    }
  }
  const select = (taskId) => {
    runs_.selected = taskId;
    render();
    const card = runs_.els.detail.querySelector(`[data-task="${CSS.escape(taskId)}"]`);
    if (card) {
      card.open = true;
      card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  };
  const head = h(
    "section.card.run-head",
    { "data-state": run.state },
    h("div.card-head", {}, h("h2", { text: `Run #${run.number}` }), h("span.run-state", { text: STATES[run.state] || run.state })),
    h("p.hint", { text: [`Started ${clock(run.started, { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" })}`, trigger(run.trigger), took(run)].filter(Boolean).join(" · ") }),
    run.doing ? h("p.run-doing", { text: `Working on ${run.doing} now.` }) : null,
    run.input || (run.items && run.items.length)
      ? h(
          "details.run-input",
          { "data-keep": "input" },
          h("summary", { text: run.items && run.items.length ? `What came with what started it, and ${plural(run.items.length, "item")}` : "What came with what started it" }),
          run.input ? h("pre", { text: run.input }) : null,
          run.items && run.items.length ? h("ol.run-items", {}, run.items.map((item) => h("li", { text: item }))) : null
        )
      : null,
    run.summary ? h("div.md.run-summary", {}, markdown(run.summary)) : null
  );
  const drawn = run.dag
    ? h("section.card.dag-graph", {}, h("div.card-head", {}, h("h2", { text: "How it went" }), h("span.aside", { text: "Choose a task for what it said" })), graph({ tasks: run.dag.tasks, end: run.dag.end, run: states, selected: runs_.selected, onSelect: select }))
    : null;
  const proposals = run.proposals.map((p) => proposalCard(p, () => runsView(runs_.els.root.parentElement, runs_.id, run.id, false)));
  const tasks = h(
    "section.card.run-tasks",
    {},
    h("div.card-head", {}, h("h2", { text: "What each task did" }), h("span.aside", { text: plural(run.tasks.length, "task run") })),
    h("div.run-task-list", {}, run.tasks.map((t) => taskCard(t)))
  );
  return [head, drawn, ...proposals, tasks].filter(Boolean);
}

function taskCard(t) {
  const label = t.source ? `${t.task} #${t.index + 1}` : t.task;
  const spent = t.started && t.finished ? duration(t.finished - t.started) : "";
  const returned = typeof t.value === "boolean" ? `Said ${t.value ? "yes" : "no"}` : Array.isArray(t.value) ? `Returned ${plural(t.value.length, "item")}` : "";
  return h(
    "details.run-task",
    { "data-task": t.task, "data-keep": `task-${t.id}`, "data-state": t.state, open: runs_.selected === t.task || null },
    h(
      "summary",
      {},
      h("code", { text: label }),
      h("span.run-state", { text: STATES[t.state] || t.state }),
      returned ? h("span.chip", { text: returned }) : null,
      spent ? h("span.hint", { text: spent }) : null
    ),
    t.source ? h("p.hint", { text: `For item ${t.index + 1} from ${t.source === "trigger" ? "what started the run" : t.source}` }) : null,
    t.item ? h("div.step-io", {}, h("span.label", { text: "Its item" }), h("pre", { text: t.item })) : null,
    t.output ? h("div.md.run-output", {}, markdown(t.output)) : null,
    stepList(t.steps, `run-${t.id}`)
  );
}

function trigger(t) {
  if (t === "schedule") return "on its schedule";
  if (t === "manual") return "run by you";
  if (t.startsWith("event:")) return `when ${t.slice(6)} came in`;
  return t;
}

function took(r) {
  return r.started && r.finished ? `took ${duration(r.finished - r.started)}` : "";
}
