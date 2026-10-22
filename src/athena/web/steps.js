// What Athena did for something, step by step: each tool it used, what it was given and what came back, whether
// it worked, and who allowed it. On the Tasks and Activity tabs.

import { h, plural } from "./core.js";

const MARKS = { ran: "✓", failed: "✗", declined: "–" }; // how each step went
const APPROVALS = { you: "you allowed", allowed: "allowed unasked" };

export function stepList(steps, key) {
  if (!steps || !steps.length) return null;
  const io = (label, text) => (text ? h("div.step-io", {}, h("span.label", { text: label }), h("pre", { text })) : null);
  return h(
    "div.steps",
    {},
    h("span.label", { text: `What it did: ${plural(steps.length, "step")}` }),
    h(
      "ol.step-list",
      {},
      steps.map((s, i) =>
        h(
          "li.step",
          { "data-outcome": s.outcome },
          h(
            "details",
            { "data-keep": `${key}-${i}` },
            h(
              "summary",
              {},
              h("span.step-mark", { text: MARKS[s.outcome] || "·", title: s.outcome }),
              h("code.step-tool", { text: s.tool }),
              s.outcome === "ran" ? null : h("span.step-outcome", { text: s.outcome }),
              s.approval && s.outcome === "ran" ? h("span.step-approval", { text: APPROVALS[s.approval] }) : null,
              h("span.step-given", { text: s.given })
            ),
            s.flagged ? h("p.step-flagged", { text: `The privacy check found ${s.flagged}.` }) : null,
            io("Given", s.given),
            io("Got back", s.said || "(nothing)")
          )
        )
      )
    )
  );
}
