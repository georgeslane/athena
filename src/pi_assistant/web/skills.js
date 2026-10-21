// The Skills tab: skills, their history, and Athena's suggestions.

import { ask, call, clock, day, h, icon, number, plural, state, toast, when } from "./core.js";
import { setReviewCount } from "./dashboard.js";

// -- skills: how to do things your way ---------------------------------------------------------------

const by = (who) => (who === "you" ? "you" : state.overview ? state.overview.assistant.name : "Athena");
const dateTime = (t) => clock(t, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

export async function loadSkills() {
  state.skills = await call("GET", "/api/skills");
  renderSkills();
}

function renderSkills() {
  const k = state.skills;
  setReviewCount("skills", k.suggestions.length);
  const parts = [];
  if (k.suggestions.length) {
    parts.push(
      h(
        "section.card.review",
        {},
        h("div.card-head", {}, h("h2", { text: "Suggested by Athena" }), h("span.aside", { text: plural(k.suggestions.length, "to review", "to review") })),
        h("p.note", {}, icon("info"), h("span", { text: "Made while Athena worked by itself. Nothing changes until you approve it." })),
        h("div.review-list", {}, k.suggestions.map(skillSuggestion))
      )
    );
  }
  const after = k.reviews ? " After using one, it suggests a change if one would help next time." : "";
  parts.push(
    h(
      "section.card",
      {},
      h("div.card-head", {}, h("h2", { text: "Skills" }), h("span.aside", { text: "How to do things your way" }), h("div.actions", {}, h("button.button.small.primary", { type: "button", text: "New skill", onclick: () => editSkill() }))),
      h(
        "p.note",
        {},
        icon("info"),
        h("span", {
          text:
            "Athena sees each skill's name and when to use it in every message, and reads the rest when it needs it. " +
            "It writes skills when you ask, and suggests them for you to approve when it's working by itself." +
            after,
        })
      ),
      k.skills.length
        ? h("div.task-list", {}, k.skills.map(skillCard))
        : h("p.empty", { text: "No skills yet. Write one, or tell Athena: “that's how I like trip plans: make it a skill”." })
    )
  );
  document.getElementById("tab-skills").replaceChildren(h("div.stack", {}, parts));
}

function skillCard(skill) {
  const used = skill.uses ? `Used ${plural(skill.uses, "time")}, last ${when(skill.last_used)}` : "Not used yet";
  const changed = `${skill.versions > 1 ? "Changed" : "Written"} ${dateTime(skill.updated_at)} by ${by(skill.updated_by)}`;
  const actions = [h("button.button.small", { type: "button", text: "Edit", onclick: () => editSkill(skill) })];
  if (skill.versions > 1) actions.push(h("button.button.small", { type: "button", text: "History", onclick: () => skillHistory(skill) }));
  actions.push(h("button.button.small.danger", { type: "button", text: "Delete", onclick: () => deleteSkill(skill) }));
  return h(
    "article.task.skill",
    {},
    h("div.task-head", {}, h("h3", { text: skill.name }), h("span.task-state", { text: used })),
    h("p.task-when", { text: skill.description }),
    h("details.skill-how", {}, h("summary", { text: "How" }), h("pre.skill-body", { text: skill.body })),
    h("p.hint", { text: changed }),
    h("div.tool-actions", {}, actions)
  );
}

function skillSuggestion(sg) {
  const decide = async (event, action) => {
    event.target.disabled = true;
    try {
      state.skills = await call("POST", `/api/skills/suggestions/${sg.id}`, { action });
      renderSkills();
      toast(action === "approve" ? `Saved “${sg.name}”.` : "Discarded.");
    } catch (e) {
      toast(e.message, true);
      event.target.disabled = false;
    }
  };
  const changes = [];
  if (sg.was && sg.was.name !== sg.name) changes.push(h("p.review-change", {}, h("span.hint", { text: "Name: " }), h("del", { text: sg.was.name }), " → ", h("ins", { text: sg.name })));
  if (!sg.was || sg.was.description !== sg.description) {
    changes.push(h("p.review-change", {}, h("span.hint", { text: "When to use it: " }), sg.was ? [h("del", { text: sg.was.description }), " → "] : null, h("ins", { text: sg.description })));
  }
  return h(
    "article.review-item",
    {},
    h("div.task-head", {}, h("h3", { text: sg.was ? `Change to “${sg.was.name}”` : `New skill: “${sg.name}”` })),
    h("p.hint", { text: `From ${sg.source}, ${dateTime(sg.created_at)}` }),
    sg.why ? h("p.review-why", { text: sg.why }) : null,
    changes,
    sg.diff ? diffView(sg.diff) : h("pre.skill-body", { text: sg.body }),
    h(
      "div.tool-actions",
      {},
      h("span.grow"),
      h("button.button.small", { type: "button", text: "Discard", onclick: (e) => decide(e, "discard") }),
      h("button.button.small", { type: "button", text: "Edit…", onclick: () => editSkill(null, sg) }),
      h("button.button.small.primary", { type: "button", text: "Approve", onclick: (e) => decide(e, "approve") })
    )
  );
}

// What a change does to a skill, line by line: unchanged stretches are folded, to show what's new.
function diffView(lines) {
  const CONTEXT = 2;
  const near = lines.map((line, i) => line.op !== " " || lines.slice(Math.max(0, i - CONTEXT), i + CONTEXT + 1).some((l) => l.op !== " "));
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    if (near[i]) {
      const line = lines[i];
      const cls = line.op === "+" ? ".added" : line.op === "-" ? ".removed" : "";
      out.push(h(`div.diff-line${cls}`, { text: `${line.op} ${line.text}` }));
      continue;
    }
    let j = i;
    while (j < lines.length && !near[j]) j++;
    out.push(h("div.diff-line.folded", { text: `… ${plural(j - i, "line")} the same` }));
    i = j - 1;
  }
  return h("div.diff", { role: "group", "aria-label": "What changes" }, out.length ? out : h("div.diff-line.folded", { text: "The instructions are the same." }));
}

// A form for a skill: a new one, one to change, or Athena's suggestion as you'd change it before saving.
function editSkill(skill, suggestion) {
  const from = suggestion || skill || {};
  const name = h("input", { type: "text", maxLength: 60, required: true, value: from.name || "" });
  const description = h("input", { type: "text", maxLength: 200, required: true, value: from.description || "", placeholder: "When you plan a trip for me" });
  const body = h("textarea.skill-editor", { required: true, rows: 12, maxLength: 5000, spellcheck: true });
  body.value = from.body || "";
  const count = h("span.field-help");
  const counted = () => (count.textContent = `${number(body.value.length)} of 5,000 characters. Markdown is fine: short steps work best.`);
  body.addEventListener("input", counted);
  counted();
  const error = h("p.form-error", { role: "alert" });
  const dialog = document.getElementById("dialog");
  const save = h("button.button.primary", { type: "submit", text: suggestion ? "Approve" : "Save" });
  const form = h(
    "form.task-form",
    {
      onsubmit: async (event) => {
        event.preventDefault();
        save.disabled = true;
        error.textContent = "";
        const fields = { name: name.value, description: description.value, body: body.value };
        try {
          if (suggestion) state.skills = await call("POST", `/api/skills/suggestions/${suggestion.id}`, { action: "approve", ...fields });
          else if (skill) state.skills = await call("POST", `/api/skills/${skill.id}`, fields);
          else state.skills = await call("POST", "/api/skills", fields);
          dialog.close();
          renderSkills();
          toast(`Saved “${name.value.trim()}”.`);
        } catch (e) {
          error.textContent = e.message;
          save.disabled = false;
        }
      },
    },
    h("h2", { text: suggestion ? "Change, then approve" : skill ? "Edit skill" : "New skill" }),
    h("label.field", {}, h("span.field-label", { text: "Name" }), name),
    h("label.field", {}, h("span.field-label", { text: "When to use it" }), h("span.field-help", { text: "One line. Athena sees this in every conversation, to know when to read the rest." }), description),
    h("label.field", {}, h("span.field-label", { text: "How" }), count, body),
    error,
    h("div.tool-actions", {}, h("button.button", { type: "button", text: "Cancel", onclick: () => dialog.close() }), save)
  );
  dialog.onclose = null;
  dialog.replaceChildren(form);
  if (!dialog.open) dialog.showModal();
  name.focus();
}

async function skillHistory(skill) {
  let data;
  try {
    data = await call("GET", `/api/skills/${skill.id}/versions`);
  } catch (e) {
    return toast(e.message, true);
  }
  const dialog = document.getElementById("dialog");
  const items = data.versions.map((v, i) =>
    h(
      "article.version",
      {},
      h("div.task-head", {}, h("h3", { text: i === 0 ? "Now" : dateTime(v.changed_at) }), h("span.hint", { text: `${i === 0 ? dateTime(v.changed_at) + ", " : ""}by ${by(v.changed_by)}` })),
      h("details", {}, h("summary", { text: v.name === skill.name ? "What it said" : `What it said, as “${v.name}”` }), h("p.task-when", { text: v.description }), h("pre.skill-body", { text: v.body })),
      i === 0
        ? null
        : h("div.tool-actions", {}, h("button.button.small", {
            type: "button",
            text: "Go back to this",
            onclick: async (event) => {
              event.target.disabled = true;
              try {
                state.skills = await call("POST", `/api/skills/${skill.id}`, { restore: v.id });
                dialog.close();
                renderSkills();
                toast(`“${skill.name}” is as it was on ${dateTime(v.changed_at)}.`);
              } catch (e) {
                toast(e.message, true);
                event.target.disabled = false;
              }
            },
          }))
    )
  );
  dialog.onclose = null;
  dialog.replaceChildren(
    h("h2", { text: `History of “${skill.name}”` }),
    h("div.versions", {}, items),
    h("div.tool-actions", {}, h("span.grow"), h("button.button", { type: "button", text: "Close", onclick: () => dialog.close() }))
  );
  dialog.showModal();
}

async function deleteSkill(skill) {
  const ok = await ask({ title: `Delete “${skill.name}”?`, body: [h("p", { text: "Athena won't know it any more, and its history goes too." })], confirm: "Delete", danger: true });
  if (!ok) return;
  try {
    state.skills = await call("DELETE", `/api/skills/${skill.id}`);
    renderSkills();
  } catch (e) {
    toast(e.message, true);
  }
}
