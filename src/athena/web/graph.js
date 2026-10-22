// An agent's DAG, drawn: its tasks top to bottom, each after those it comes after, then the tasks every DAG ends
// with, as one row. For the DAG tab, as it's written, and the History tab, with how each task went in a run.
//
// Each task goes in a layer one below the lowest of those it comes after (the longest path to it), and the tasks in
// each layer are ordered by where those they're joined to are (the barycentre, sweeping down then up a few times),
// so few edges cross. A DAG has at most 30 tasks, so this takes no time at all.

import { h, icon } from "./core.js";

const NODE_W = 172;
const NODE_H = 54;
const GAP_X = 26;
const GAP_Y = 44;
const SWEEPS = 4;
const SVG = "http://www.w3.org/2000/svg";
let drawn = 0; // each drawing's arrowhead has an id of its own

// tasks: [{id, prompt, upstream, triggered, alert}], in the order the server gives (each after its upstream).
// end: [{id, about}]. run: {task id: {state, count, failed}} for a run's view. selected: a task's id.
export function graph({ tasks, end, run = null, selected = null, onSelect = null }) {
  const { positions, width, height, endY } = layout(tasks);
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
  const sinks = tasks.filter((t) => !tasks.some((other) => other.upstream.includes(t.id)));
  for (const task of tasks) {
    for (const up of task.upstream) {
      const skipped = run && ["skipped", "upstream_failed"].includes((run[task.id] || {}).state);
      svg.append(edge(positions.get(up), positions.get(task.id), skipped, arrow));
    }
  }
  if (ends) {
    const target = { x: width / 2 - NODE_W / 2, y: endY };
    for (const sink of sinks) svg.append(edge(positions.get(sink.id), target, false, arrow));
  }
  box.append(svg);
  for (const task of tasks) {
    const p = positions.get(task.id);
    box.append(node(task, p, run && run[task.id], selected === task.id, onSelect));
  }
  // The tasks every DAG ends with, in a row below, as wide as there's room for: the arrows above end at it.
  return h("div.graph-scroll", {}, box, ends ? endRow(end, run, selected, onSelect) : null);
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
  const width = Math.max(widest * NODE_W + (widest - 1) * GAP_X, NODE_W * 1.6);
  const positions = new Map();
  layers.forEach((layer, l) => {
    const span = layer.length * NODE_W + (layer.length - 1) * GAP_X;
    layer.forEach((id, i) => positions.set(id, { x: (width - span) / 2 + i * (NODE_W + GAP_X), y: l * (NODE_H + GAP_Y) }));
  });
  const height = layers.length ? layers.length * NODE_H + (layers.length - 1) * GAP_Y : 0;
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
  return path;
}

function node(task, p, went, selected, onSelect) {
  const marks = [];
  if (task.triggered) marks.push(h("span.graph-mark", { title: "Runs once for each time a task before it triggers it" }, went && went.count ? `×${went.count}` : "×n"));
  if (task.alert) marks.push(h("span.graph-mark", { title: "Tells you what it found" }, icon("bell")));
  const state = went ? went.state : "";
  return h(
    onSelect ? "button.graph-node" : "div.graph-node",
    {
      type: onSelect ? "button" : null,
      style: { left: `${p.x}px`, top: `${p.y}px`, width: `${NODE_W}px`, height: `${NODE_H}px` },
      "data-state": state || null,
      "aria-pressed": onSelect ? String(selected) : null,
      "aria-label": `${task.id}${state ? `: ${state.replace("_", " ")}` : ""}`,
      title: task.prompt,
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
          t.id.replace("_", " ")
        );
      })
    )
  );
}

function firstLine(text) {
  return (text || "").split("\n")[0];
}
