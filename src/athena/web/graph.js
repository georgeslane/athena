// An agent's DAG, drawn: its tasks top to bottom, each after those it comes after, then the tasks every DAG ends
// with, as one row. For the DAG tab, as it's written, and the History tab, with how each task went in a run.
//
// Each task goes in a layer one below the lowest of those it comes after (the longest path to it), and the tasks in
// each layer are ordered by where those they're joined to are (the barycentre, sweeping down then up a few times),
// so few edges cross. A DAG has at most 30 tasks, so this takes no time at all.
//
// As agentic-airflow runs them: a task after one that fans out a list runs once for each item, and so does one that
// follows the mapping of one that does; those are drawn as stacks, the edge from the list says "each", and an edge
// from a stack to a task that runs once says "all", as it's given every item's. An edge from a branch says "if
// picked". Task("items", fn=trigger_items) stands for what started the run. A task group is a box round its tasks,
// where that doesn't take in others, and a tag on each of them where it would. Only the edges to what a task comes
// after directly are drawn: not one to a task it comes after through another.

import { h, icon } from "./core.js";

let NODE_W = 176; // narrower on a phone (graph())
const NODE_H = 56;
let GAP_X = 28;
const GAP_Y = 48;
const PAD = 8; // round a group's tasks
const SWEEPS = 4;
const SVG = "http://www.w3.org/2000/svg";
let drawn = 0; // each drawing's arrowhead has an id of its own

// tasks: [{id, kind, prompt, fn, return_type, fan_out, follow_mapping, allow_none, group, upstream, outlets, alert}],
// in the order the server gives (each after its upstream). end: [{id, about}]. run: {task id: {state, count,
// states}} for a run's view. selected: a task's id.
export function graph({ tasks, end, run = null, selected = null, onSelect = null }) {
  [NODE_W, GAP_X] = window.matchMedia("(max-width: 479px)").matches ? [136, 14] : [176, 28];
  const each = mapped(tasks);
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const drawnTasks = direct(tasks).map((t) => ({ ...t, each: each.has(t.id) }));
  const { positions, width, height, endY } = layout(drawnTasks);
  const ends = Boolean(end && end.length);
  const box = h("div.graph", { style: { width: `${width}px`, height: `${ends ? endY : height}px` } });
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "graph-edges");
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", box.style.height.replace("px", ""));
  svg.setAttribute("aria-hidden", "true");
  const defs = document.createElementNS(SVG, "defs");
  const arrow = `arrow-${++drawn}`;
  const marker = document.createElementNS(SVG, "marker");
  for (const [k, v] of Object.entries({ id: arrow, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" })) marker.setAttribute(k, v);
  const tip = document.createElementNS(SVG, "path");
  tip.setAttribute("d", "M0 1L9 5L0 9z");
  marker.append(tip);
  defs.append(marker);
  svg.append(defs);
  const { boxes, tagged } = groups(tasks, positions);
  box.append(...boxes);
  const labels = [];
  for (const task of drawnTasks) {
    for (const up of task.upstream) {
      const skipped = run && ["skipped", "upstream_failed"].includes((run[task.id] || {}).state);
      const from = byId.get(up);
      const kind = from.fan_out ? "each" : from.kind === "agentic_branch" ? "if" : each.has(up) && !each.has(task.id) ? "all" : "";
      const [path, middle] = edge(positions.get(up), positions.get(task.id), skipped || kind === "if", arrow);
      svg.append(path);
      if (kind) labels.push(h(`span.graph-edge-label.${kind}`, { text: kind === "if" ? "if picked" : kind, style: { left: `${middle.x}px`, top: `${middle.y}px` } }));
    }
  }
  if (ends) {
    const sinks = drawnTasks.filter((t) => !drawnTasks.some((other) => other.upstream.includes(t.id)));
    const target = { x: width / 2 - NODE_W / 2, y: endY };
    for (const sink of sinks) svg.append(edge(positions.get(sink.id), target, false, arrow)[0]);
  }
  box.append(svg, ...labels);
  for (const task of drawnTasks) {
    const p = positions.get(task.id);
    box.append(task.fn === "trigger_items" ? startNode(task, p, run && run[task.id]) : node(task, p, run && run[task.id], selected === task.id, onSelect, tagged.has(task.id)));
  }
  // The tasks every DAG ends with, in a row below, as wide as there's room for: the arrows above end at it.
  return h("div.graph-scroll", {}, box, ends ? endRow(end, run, selected, onSelect) : null);
}

// The tasks that run once for each item, as agentic-airflow maps them: those after a task that fans out a list, and
// those that follow the mapping of one that runs for each item.
function mapped(tasks) {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const each = new Set();
  for (const t of tasks) {
    const ups = t.upstream.map((u) => byId.get(u)).filter(Boolean);
    if (ups.some((u) => u.fan_out) || (t.follow_mapping && ups.some((u) => each.has(u.id)))) each.add(t.id);
  }
  return each;
}

// The tasks, each with only what it comes after directly: not a task it comes after through another of those.
function direct(tasks) {
  const before = new Map(); // id -> every task it comes after, however far back
  for (const task of tasks) {
    const all = new Set(task.upstream);
    for (const up of task.upstream) for (const earlier of before.get(up) || []) all.add(earlier);
    before.set(task.id, all);
  }
  return tasks.map((task) => ({ ...task, upstream: task.upstream.filter((up) => !task.upstream.some((other) => other !== up && before.get(other).has(up))) }));
}

// Where each task goes: {positions: id -> {x, y}, width, height, endY}.
export function layout(tasks) {
  const layerOf = new Map();
  for (const task of tasks) {
    const above = task.upstream.map((u) => layerOf.get(u) ?? 0);
    layerOf.set(task.id, above.length ? Math.max(...above) + 1 : 0);
  }
  const layers = [];
  for (const task of tasks) (layers[layerOf.get(task.id)] ||= []).push(task.id);
  const up = new Map(tasks.map((t) => [t.id, t.upstream]));
  const down = new Map(tasks.map((t) => [t.id, []]));
  for (const task of tasks) for (const u of task.upstream) down.get(u).push(task.id);
  const order = new Map();
  const place = () => layers.forEach((layer) => layer.forEach((id, i) => order.set(id, i)));
  place();
  const centre = (ids) => (ids.length ? ids.reduce((sum, id) => sum + order.get(id), 0) / ids.length : null);
  const sweep = (layer, joined) => {
    const keyed = layer.map((id, i) => [centre(joined.get(id)) ?? i, i, id]);
    keyed.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    return keyed.map(([, , id]) => id);
  };
  for (let n = 0; n < SWEEPS; n++) {
    for (let l = 1; l < layers.length; l++) {
      layers[l] = sweep(layers[l], up);
      place();
    }
    for (let l = layers.length - 2; l >= 0; l--) {
      layers[l] = sweep(layers[l], down);
      place();
    }
  }
  const widest = Math.max(1, ...layers.map((layer) => layer.length));
  const width = Math.max(widest * NODE_W + (widest - 1) * GAP_X + 2 * PAD, NODE_W * 1.6);
  const positions = new Map();
  layers.forEach((layer, l) => {
    const span = layer.length * NODE_W + (layer.length - 1) * GAP_X;
    layer.forEach((id, i) => positions.set(id, { x: (width - span) / 2 + i * (NODE_W + GAP_X), y: PAD * 2 + l * (NODE_H + GAP_Y) }));
  });
  const height = layers.length ? PAD * 2 + layers.length * NODE_H + (layers.length - 1) * GAP_Y + PAD : 0;
  return { positions, width, height, endY: height + GAP_Y };
}

function edge(from, to, faint, arrow) {
  const x1 = from.x + NODE_W / 2;
  const y1 = from.y + NODE_H;
  const x2 = to.x + NODE_W / 2;
  const y2 = to.y - 3;
  const bend = Math.max(18, (y2 - y1) / 2);
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", `M${x1} ${y1} C${x1} ${y1 + bend}, ${x2} ${y2 - bend}, ${x2} ${y2}`);
  path.setAttribute("marker-end", `url(#${arrow})`);
  if (faint) path.setAttribute("class", "faint");
  return [path, { x: (x1 + x2) / 2, y: (y1 + y2) / 2 }];
}

// Each task group, as a box round its tasks, if one wouldn't take in another task; otherwise its tasks are tagged.
// Returns {boxes, tagged: the ids of the tasks tagged}.
function groups(tasks, positions) {
  const named = new Map();
  for (const t of tasks) if (t.group) (named.get(t.group) || named.set(t.group, []).get(t.group)).push(t);
  const boxes = [];
  const tagged = new Set();
  for (const [name, members] of named) {
    const at = members.map((t) => positions.get(t.id));
    const left = Math.min(...at.map((p) => p.x)) - PAD;
    const top = Math.min(...at.map((p) => p.y)) - PAD * 2;
    const right = Math.max(...at.map((p) => p.x + NODE_W)) + PAD;
    const bottom = Math.max(...at.map((p) => p.y + NODE_H)) + PAD;
    const inside = tasks.some((t) => t.group !== name && positions.has(t.id) && (({ x, y }) => x < right && x + NODE_W > left && y < bottom && y + NODE_H > top)(positions.get(t.id)));
    if (inside) {
      for (const t of members) tagged.add(t.id);
      continue;
    }
    boxes.push(h("div.graph-group", { style: { left: `${left}px`, top: `${top}px`, width: `${right - left}px`, height: `${bottom - top}px` } }, h("span.graph-group-label", { text: name })));
  }
  return { boxes, tagged };
}

function startNode(task, p, went) {
  return h(
    "div.graph-node.graph-start",
    {
      style: { left: `${p.x}px`, top: `${p.y}px`, width: `${NODE_W}px`, height: `${NODE_H}px` },
      title: "The items that came with what started the run: what comes after it runs once for each",
      "data-state": went ? went.state : null,
    },
    h("span.graph-node-head", {}, icon("bolt"), h("code", { text: task.id })),
    h("span.graph-node-text", { text: "Each item that started the run" })
  );
}

function node(task, p, went, selected, onSelect, tagged) {
  const marks = [];
  if (tagged) marks.push(h("span.graph-mark", { title: `In the task group ${task.group}`, text: task.group }));
  if (task.each) marks.push(h("span.graph-mark", { title: "Runs once for each item" }, went && went.count ? `×${went.count}` : "×n"));
  if (task.fan_out) marks.push(h("span.graph-mark", { title: "Returns a list: what's after it runs for each item", text: "list" }));
  else if (task.return_type && task.return_type !== "string") marks.push(h("span.graph-mark", { title: `Returns ${task.return_type}`, text: task.return_type }));
  if (task.kind === "agentic_branch") marks.push(h("span.graph-mark", { title: task.allow_none ? "Picks what runs after it, or none" : "Picks what runs after it", text: "picks" }));
  if (task.kind === "agentic_evaluator") marks.push(h("span.graph-mark", { title: "Checks what's before it, and has it done again", text: "checks" }));
  if (task.alert) marks.push(h("span.graph-mark", { title: "Tells you what it found" }, icon("bell")));
  if (task.outlets && task.outlets.length) marks.push(h("span.graph-mark", { title: `Updates ${task.outlets.join(", ")} with what it finds` }, icon("send")));
  const state = went ? went.state : "";
  const how = went && went.states ? ` (${went.states})` : "";
  return h(
    onSelect ? "button.graph-node" : "div.graph-node",
    {
      type: onSelect ? "button" : null,
      style: { left: `${p.x}px`, top: `${p.y}px`, width: `${NODE_W}px`, height: `${NODE_H}px` },
      "data-state": state || null,
      "data-each": task.each ? "" : null,
      "aria-pressed": onSelect ? String(selected) : null,
      "aria-label": `${task.id}${state ? `: ${state.replace("_", " ")}${how}` : ""}`,
      title: `${task.prompt || ""}${how ? `\n${how.slice(2, -1)}` : ""}`,
      onclick: onSelect ? () => onSelect(task.id) : null,
    },
    h("span.graph-node-head", {}, h("code", { text: task.id }), ...marks),
    h("span.graph-node-text", { text: firstLine(task.prompt) })
  );
}

function endRow(end, run, selected, onSelect) {
  return h(
    "div.graph-end",
    {},
    h("span.graph-end-label", { text: "Every DAG ends with" }),
    h(
      "span.graph-end-tasks",
      {},
      end.map((t) => {
        const went = run && run[t.id];
        return h(
          onSelect ? "button.graph-chip" : "span.graph-chip",
          {
            type: onSelect ? "button" : null,
            title: t.about,
            "data-state": went ? went.state : null,
            "aria-pressed": onSelect ? String(selected === t.id) : null,
            onclick: onSelect ? () => onSelect(t.id) : null,
          },
          t.id.replace("end.", "").replace("_", " ")
        );
      })
    )
  );
}

function firstLine(text) {
  return (text || "").split("\n")[0];
}
