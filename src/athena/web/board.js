// The Board tab: coding projects handed to Claude, as epics of stories.

import { call, day, h, icon, number, plural, state, toast, when } from "./core.js";

// -- the board: coding projects handed to Claude, as epics of stories ------------------------------------

const COLUMNS = [
  ["to do", "To do"],
  ["in progress", "In progress"],
  ["done", "Done"],
];
const PROJECT_STATES = { new: "New", "in progress": "In progress", blocked: "Blocked", done: "Done", merged: "Merged", closed: "Closed" };
// Where a project is: Claude's word for it, unless its pull request is merged or closed.
const projectState = (p) => (p.pr === "open" ? p.status : p.pr);
const finished = (p) => p.pr !== "open" || p.status === "done";

export async function loadBoard() {
  try {
    state.board = await call("GET", "/api/projects");
  } catch (e) {
    if (e.status !== 409) throw e;
    state.board = { off: e.message };
  }
  renderBoard();
}

async function checkBoard(event) {
  event.target.disabled = true;
  try {
    state.board = await call("POST", "/api/projects/refresh", {});
    renderBoard();
    toast(state.board.error ? state.board.error : "Up to date.", Boolean(state.board.error));
  } catch (e) {
    toast(e.message, true);
    event.target.disabled = false;
  }
}

function renderBoard() {
  const b = state.board;
  const panel = document.getElementById("tab-board");
  if (b.off) {
    panel.replaceChildren(h("section.card", {}, h("p.note.setup", {}, icon("info"), h("span", { text: `${b.off} See docs/claude-projects.md.` }))));
    return;
  }
  const open = b.projects.filter((p) => !finished(p));
  const done = b.projects.filter(finished);
  const checked = b.checked ? `Checked ${when(b.checked)}, and every ${plural(b.poll_minutes, "minute")}.` : "Not checked yet.";
  const parts = [
    h(
      "section.card",
      {},
      h("div.card-head", {}, h("h2", { text: "Board" }), h("span.aside", { text: `Projects for Claude, in ${b.repo}` }), h("div.actions", {}, h("button.button.small", { type: "button", text: "Check now", onclick: checkBoard }))),
      h("p.hint", { text: checked }),
      b.error ? h("p.form-error", { role: "alert", text: b.error }) : null,
      b.projects.length ? null : h("p.empty", { text: "No projects yet. Ask Athena to hand a coding project to Claude, and it shows here once you've approved it." })
    ),
    ...open.map(epicCard),
  ];
  if (done.length) parts.push(h("details.card.done-epics", {}, h("summary", { text: `Done (${number(done.length)})` }), h("div.stack", {}, done.map(epicCard))));
  panel.replaceChildren(h("div.stack", {}, parts));
}

async function restartClaude(event, p) {
  event.target.disabled = true;
  try {
    state.board = await call("POST", "/api/projects/restart", { folder: p.folder });
    renderBoard();
    toast(`Asked Claude to ${p.status === "new" ? "start on" : "carry on with"} “${p.title}”.`);
  } catch (e) {
    toast(e.message, true);
    event.target.disabled = false;
  }
}

// Claude isn't working on it: why Athena thinks so, and when it'll start it again.
function waitingNote(p) {
  if (!p.waiting) return null;
  const what = p.waiting === "not started" ? "hasn't started yet" : "stopped part way, with nothing pushed for a while";
  let text;
  if (p.gave_up) text = `Claude ${what}, and Athena gave up after ${plural(p.restarts, "try", "tries")}. Check its routine at claude.ai/code/routines, then restart it.`;
  else {
    const tries = p.restarts ? ` Athena's started it again ${plural(p.restarts, "time")}.` : "";
    const next = p.next_restart ? ` Next try ${when(p.next_restart)}.` : "";
    text = `Claude ${what}: maybe your usage limit.${tries}${next}`;
  }
  return h("p.note.setup", {}, icon("info"), h("span", { text }));
}

function epicCard(p) {
  const total = p.stories.length;
  const doneCount = p.stories.filter((s) => s.status === "done").length;
  const links = [h("a.button.small", { href: p.url, target: "_blank", rel: "noopener noreferrer", text: p.number ? `Pull request #${p.number}` : "On GitHub" })];
  if (p.solution_url) links.push(h("a.button.small", { href: p.solution_url, target: "_blank", rel: "noopener noreferrer", text: "Solution" }));
  for (const r of p.rounds || []) links.push(h("a.button.small.quiet", { href: r.url, target: "_blank", rel: "noopener noreferrer", text: `#${r.number} (${r.pr})` }));
  links.push(h("span.grow"));
  if (p.pr === "open" && p.status !== "done") links.push(h("button.button.small", { type: "button", text: "Restart Claude", onclick: (e) => restartClaude(e, p) }));
  if (p.pr === "open" || p.pr === "merged") links.push(h("button.button.small", { type: "button", text: "Add stories…", onclick: () => addStories(p) }));
  const columns = COLUMNS.map(([status, label]) => {
    const stories = p.stories.filter((s) => s.status === status || (status === "in progress" && s.status === "blocked"));
    return h(
      "div.board-column",
      { "data-status": status },
      h("h4", {}, label, h("span.count", { text: number(stories.length) })),
      stories.length ? stories.map(storyCard) : h("p.hint.column-empty", { text: "Nothing here." })
    );
  });
  const requirements = [p.summary && h("p.epic-summary", { text: p.summary }), p.notes && h("p.epic-notes", {}, h("strong", { text: "Notes: " }), p.notes)];
  return h(
    "section.card.epic",
    { "data-status": projectState(p) },
    h("div.epic-head", {}, h("span.epic-key", { text: "Epic" }), h("h3", { text: p.title }), h("span.lozenge", { "data-status": projectState(p), text: PROJECT_STATES[projectState(p)] || p.status })),
    h("p.hint", { text: [p.date && `Handed over ${day(p.date)}`, p.round > 1 && `Round ${p.round}`, p.updated && `Claude last said ${when(Date.parse(p.updated) / 1000)}`].filter(Boolean).join(" · ") }),
    p.progress ? h("p.epic-progress", {}, h("strong", { text: "Claude: " }), p.progress) : null,
    waitingNote(p),
    p.problem ? h("p.form-error", { text: p.problem }) : null,
    total
      ? h("div.epic-bar", {}, h("div.bar", { role: "img", "aria-label": `${doneCount} of ${total} stories done` }, h("span", { style: { width: `${(doneCount / total) * 100}%` } })), h("span.hint", { text: `${doneCount} of ${plural(total, "story", "stories")} done` }))
      : null,
    h("div.board-columns", {}, columns),
    p.summary || p.notes ? h("details.epic-requirements", {}, h("summary", { text: "Requirements" }), requirements) : null,
    h("div.tool-actions", {}, links)
  );
}

// More work on a project: new stories, which Athena adds to its requirements, and starts Claude on.
function addStories(p) {
  const dialog = document.getElementById("dialog");
  const summary = h("textarea", { rows: 3, required: true, placeholder: "Add an export to a spreadsheet, as the monthly totals are hard to share." });
  const notes = h("textarea", { rows: 2, placeholder: "Optional: new constraints, such as a library to use." });
  const list = h("div.story-forms");
  const blank = () => {
    const title = h("input", { type: "text", required: true, maxLength: 200, placeholder: "Export to a spreadsheet" });
    const details = h("textarea", { rows: 2, placeholder: "Optional: what it is, for someone new to it." });
    const done = h("textarea", { rows: 3, required: true, placeholder: "One per line, such as:\nAn .xlsx with a sheet per month\nIt opens in Numbers and Excel" });
    const remove = h("button.button.small.quiet", { type: "button", text: "Remove", onclick: () => list.children.length > 1 && block.remove() });
    const block = h(
      "fieldset.story-form",
      {},
      h("label.field", {}, h("span.field-label", { text: "Story" }), title),
      h("label.field", {}, h("span.field-label", { text: "Details" }), details),
      h("label.field", {}, h("span.field-label", { text: "Done when" }), done),
      h("div.tool-actions", {}, h("span.grow"), remove)
    );
    block.read = () => ({ title: title.value, details: details.value, done_when: done.value.split("\n").map((c) => c.trim()).filter(Boolean) });
    return block;
  };
  list.append(blank());
  const error = h("p.form-error", { role: "alert" });
  const save = h("button.button.primary", { type: "submit", text: p.pr === "open" ? "Add and start Claude" : "Start a new round" });
  const form = h(
    "form.task-form",
    {
      onsubmit: async (event) => {
        event.preventDefault();
        save.disabled = true;
        error.textContent = "";
        try {
          state.board = await call("POST", "/api/projects/extend", { folder: p.folder, summary: summary.value, notes: notes.value, stories: [...list.children].map((b) => b.read()) });
          dialog.close();
          renderBoard();
          toast(`Added to “${p.title}”: Claude's on it.`);
        } catch (e) {
          error.textContent = e.message;
          save.disabled = false;
        }
      },
    },
    h("h2", { text: `More for “${p.title}”` }),
    h(
      "p.note.setup",
      {},
      icon("info"),
      h("span", {
        text:
          (p.pr === "open" ? "The new stories go on its open pull request, and Claude starts on them. " : "It's merged, so this opens a new pull request for it, and Claude starts on that. ") +
          "They go to GitHub and Claude, so leave out anything personal.",
      })
    ),
    h("label.field", {}, h("span.field-label", { text: "What's changing" }), summary),
    list,
    h("div", {}, h("button.button.small", { type: "button", text: "Another story", onclick: () => list.append(blank()) })),
    h("label.field", {}, h("span.field-label", { text: "Notes" }), notes),
    error,
    h("div.tool-actions", {}, h("button.button", { type: "button", text: "Cancel", onclick: () => dialog.close() }), save)
  );
  dialog.onclose = null;
  dialog.replaceChildren(form);
  dialog.showModal();
  summary.focus();
}

function storyCard(s) {
  return h(
    "article.story",
    { "data-status": s.status },
    h("div.story-head", {}, h("span.story-key", { text: s.id }), s.status === "blocked" ? h("span.flag", { text: "Blocked" }) : null),
    h("p.story-title", { text: s.title }),
    s.note ? h("p.story-note", { text: s.note }) : null,
    s.details || s.done_when.length
      ? h(
          "details.story-more",
          {},
          h("summary", { text: s.done_when.length ? `Done when (${s.done_when.length})` : "Details" }),
          s.details ? h("p.story-details", { text: s.details }) : null,
          s.done_when.length ? h("ul.criteria", {}, s.done_when.map((c) => h("li", { text: c }))) : null
        )
      : null
  );
}
