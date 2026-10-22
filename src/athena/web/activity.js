// The Activity tab: what Athena did, newest first. Your messages and its answers, the tasks it ran, its checks
// on what it's watching, alerts, and when it started, each opening to the steps it took.

import { call, clock, h, isoDay, now, plural, seconds, state, toast } from "./core.js";
import { stepList } from "./steps.js";

const activity_ = { kinds: "", entries: [], more: false, keepDays: 30, off: "", loading: true };
const FILTERS = [
  ["All", ""],
  ["Messages", "message"],
  ["Tasks", "task,check"],
  ["Alerts", "alert,athena"],
];
const KINDS = { message: "You", task: "Task", alert: "Alert" }; // a label before the title, where it needs one
const OUTCOMES = { problem: "Not working", fixed: "Working again" };
const QUIET = "nothing new"; // checks that found nothing: several in a row are shown as one

export async function loadActivity() {
  const kinds = activity_.kinds;
  let data;
  try {
    data = await call("GET", `/api/activity${kinds ? `?kinds=${kinds}` : ""}`);
  } catch (e) {
    if (e.status !== 409) throw e;
    activity_.off = e.message;
    render();
    return;
  }
  if (kinds !== activity_.kinds) return; // the filter changed while it was asked
  // The newest, and what you'd already shown before them, if there's more than one page.
  const oldest = data.entries.length ? data.entries[data.entries.length - 1].id : Infinity;
  const earlier = data.more ? activity_.entries.filter((e) => e.id < oldest) : [];
  activity_.entries = [...data.entries, ...earlier];
  activity_.more = earlier.length ? activity_.more : data.more;
  activity_.keepDays = data.keep_days;
  activity_.off = "";
  activity_.loading = false;
  render();
}

async function showEarlier(event) {
  const button = event.target;
  button.disabled = true;
  const last = activity_.entries[activity_.entries.length - 1];
  const kinds = activity_.kinds;
  try {
    const data = await call("GET", `/api/activity?before=${last.id}${kinds ? `&kinds=${kinds}` : ""}`);
    if (kinds !== activity_.kinds) return;
    activity_.entries.push(...data.entries);
    activity_.more = data.more;
    render();
  } catch (e) {
    toast(e.message, true);
    button.disabled = false;
  }
}

function filter(kinds) {
  if (kinds === activity_.kinds) return;
  activity_.kinds = kinds;
  activity_.entries = [];
  activity_.more = false;
  activity_.loading = true;
  render();
  loadActivity().catch((e) => toast(e.message, true));
}

function render() {
  const panel = document.getElementById("tab-activity");
  if (activity_.off) {
    panel.replaceChildren(h("section.card", {}, h("p.empty", { text: activity_.off })));
    return;
  }
  // What you've opened stays open when the tab's drawn again, as it is whenever Athena finishes something.
  const opened = new Set([...panel.querySelectorAll("details[data-keep][open]")].map((d) => d.dataset.keep));
  const name = state.overview ? state.overview.assistant.name : "Athena";
  const filters = h(
    "div.filters",
    { role: "group", "aria-label": "Show" },
    FILTERS.map(([label, kinds]) =>
      h("button", { type: "button", text: label, "aria-pressed": String(activity_.kinds === kinds), onclick: () => filter(kinds) })
    )
  );
  let empty = activity_.kinds ? "Nothing of this kind yet." : `Nothing yet. What ${name} does shows here, newest first.`;
  if (activity_.loading) empty = "Loading…";
  panel.replaceChildren(
    h(
      "section.card",
      {},
      h("div.card-head", {}, h("h2", { text: "Activity" }), h("span.aside", { text: `What ${name} did, kept for ${plural(activity_.keepDays, "day")}` })),
      filters,
      activity_.entries.length ? days(activity_.entries) : h("p.empty", { text: empty }),
      activity_.more ? h("div.more", {}, h("button.button.small", { type: "button", text: "Show earlier", onclick: showEarlier })) : null
    )
  );
  for (const d of panel.querySelectorAll("details[data-keep]")) if (opened.has(d.dataset.keep)) d.open = true;
}

// Each day, under its name, with what was done on it.
function days(entries) {
  const groups = [];
  for (const entry of entries) {
    const day = isoDay(entry.at);
    if (!groups.length || groups[groups.length - 1].day !== day) groups.push({ day, at: entry.at, entries: [] });
    groups[groups.length - 1].entries.push(entry);
  }
  return groups.map((g) => h("div.activity-day", {}, h("h3.label", { text: dayName(g.at, g.day) }), h("ol.activity", {}, quietTogether(g.entries))));
}

function dayName(at, day) {
  if (day === isoDay(now())) return "Today";
  if (day === isoDay(now() - 86400)) return "Yesterday";
  return clock(at, { weekday: "long", day: "numeric", month: "long" });
}

// Checks that found nothing, one after another, as one line that opens to each of them.
function quietTogether(entries) {
  const items = [];
  for (let i = 0; i < entries.length; ) {
    let j = i;
    while (j < entries.length && quiet(entries[j])) j++;
    if (j - i >= 2) {
      items.push(quietRun(entries.slice(i, j)));
      i = j;
    } else {
      items.push(entry(entries[i]));
      i++;
    }
  }
  return items;
}

const quiet = (e) => e.kind === "check" && e.outcome === QUIET && !e.steps.some((s) => s.approval === "you");

function quietRun(run) {
  const time = (e) => clock(e.at, { hour: "2-digit", minute: "2-digit" });
  return h(
    "li.entry",
    { "data-kind": "check" },
    h(
      "details",
      { "data-keep": `quiet-${run[run.length - 1].id}` },
      h(
        "summary",
        {},
        h("span.entry-time", { text: time(run[0]) }),
        h("span.entry-dot", { "data-tone": "quiet" }),
        h(
          "span.entry-main",
          {},
          h("span.entry-title", { text: `Checked what you're watching ${run.length} times` }),
          h("span.entry-sub", {}, h("span.entry-outcome", { text: "Nothing new" }), ` · since ${time(run[run.length - 1])}`)
        )
      ),
      h("ol.activity.nested", {}, run.map(entry))
    )
  );
}

function entry(e) {
  const asked = e.steps.filter((s) => s.approval === "you").length;
  const meta = [
    e.channel,
    e.seconds >= 1 ? seconds(e.seconds) : "",
    e.steps.length ? plural(e.steps.length, "step") : "",
    asked ? `asked you ${asked === 1 ? "once" : plural(asked, "time")}` : "",
  ].filter(Boolean);
  const summary = [
    h("span.entry-time", { text: clock(e.at, { hour: "2-digit", minute: "2-digit" }) }),
    h("span.entry-dot", { "data-tone": tone(e) }),
    h(
      "span.entry-main",
      {},
      h("span.entry-title", {}, KINDS[e.kind] ? h("b.entry-kind", { text: KINDS[e.kind] }) : null, e.title),
      e.kind === "athena" ? null : h("span.entry-sub", {}, h("span.entry-outcome", { text: outcome(e) }), meta.length ? ` · ${meta.join(" · ")}` : "")
    ),
  ];
  const attrs = { "data-kind": e.kind };
  if (!e.said && !e.steps.length) return h("li.entry", attrs, h("div.entry-row", {}, summary));
  return h(
    "li.entry",
    attrs,
    h(
      "details",
      { "data-keep": `entry-${e.id}` },
      h("summary", {}, summary),
      h("div.entry-body", {}, e.said ? h("p.task-said", { text: e.said }) : null, stepList(e.steps, `entry-${e.id}`))
    )
  );
}

// How it went, in a word or two: "Told you", or why it failed.
function outcome(e) {
  const said = OUTCOMES[e.outcome] || e.outcome.replace(/^failed: /, "");
  return said.charAt(0).toUpperCase() + said.slice(1);
}

function tone(e) {
  if (e.outcome.startsWith("failed") || e.outcome.startsWith("ran out of time") || e.outcome === "problem") return "bad";
  if (e.steps.some((s) => s.outcome === "declined")) return "approval";
  if (e.outcome === QUIET || e.outcome === "stopped" || e.kind === "athena") return "quiet";
  return "good";
}
