// The Watching tab: what Athena keeps an eye on for you, all checked together every hour. Each has a key: an agent's
// DAG with the trigger "watch:<key>" runs when it happens, with what was found; otherwise you're told.

import { call, clock, h, icon, isoDay, plural, toast } from "./core.js";
import { stepList } from "./steps.js";

const watching_ = { data: null, timer: null };

export async function loadWatching() {
  watching_.data = await call("GET", "/api/watches");
  render();
}

function render() {
  const w = watching_.data;
  const panel = document.getElementById("tab-watching");
  clearTimeout(watching_.timer);
  const opened = new Set([...panel.querySelectorAll("details[data-keep][open]")].map((d) => d.dataset.keep));
  const day_ = (ts) => clock(ts, { weekday: "short", day: "numeric", month: "short" });
  const time_ = (ts) => clock(ts, { hour: "2-digit", minute: "2-digit" });
  let about;
  if (!w.every_minutes) about = "Checking is off: [tasks] heartbeat_minutes is 0.";
  else if (w.checking) about = "Checking now.";
  else if (w.asked) about = "Athena will check as soon as it's free.";
  else {
    const parts = [`Checked every ${w.every_minutes === 60 ? "hour" : plural(w.every_minutes, "minute")}: an agent's DAG starts when what it waits for happens, and otherwise you only hear when something needs you.`];
    if (w.next) parts.push(`Next about ${time_(Math.max(w.next, Date.now() / 1000))}.`);
    about = parts.join(" ");
  }
  const last =
    w.last &&
    h(
      "details.task-last.check-last",
      { "data-keep": "check" },
      h("summary", { text: `Last checked ${time_(w.last.at)}: ${w.last.outcome}` }),
      w.last.said ? h("p.task-said", { text: w.last.said }) : null,
      stepList(w.last.steps, `check-${w.last.at}`),
      w.last.said || (w.last.steps || []).length ? null : h("p.hint", { text: "It didn't say or do anything." })
    );
  const items = w.items.length
    ? h(
        "div.task-list",
        {},
        w.items.map((item) =>
          h(
            "article.task",
            { "data-status": "active" },
            h("div.task-head", {}, h("h3", { text: item.what }), h("code.watch-key", { text: `watch:${item.key}`, title: "Put this in a DAG's triggers to start it when this happens" })),
            h("p.task-when", { text: [item.until ? `Until ${day_(item.until)}` : "No end", `since ${day_(item.created)}`, item.by === "you" ? "" : "added by Athena"].filter(Boolean).join(" · ") }),
            item.starts.length
              ? h("p.watch-starts", {}, icon("clock"), h("span", { text: `When it happens, it starts the DAG of ${item.starts.map((s) => `“${s}”`).join(" and ")}.` }))
              : h("p.hint", { text: "No agent's DAG waits for it: Athena tells you when it happens." }),
            item.note ? h("p.task-said", { text: `Athena's note: ${item.note}` }) : null,
            h(
              "div.tool-actions",
              {},
              h("button.button.small", { type: "button", text: "Edit", onclick: () => watchForm(item) }),
              h("span.grow"),
              h("button.button.small.danger", { type: "button", text: "Stop watching", onclick: (e) => stopWatching(e, item) })
            )
          )
        )
      )
    : h("p.empty", { text: "Nothing yet. Add something, or ask Athena: “tell me about new emails about jobs”." });
  panel.replaceChildren(
    h(
      "div.stack",
      {},
      h(
        "section.card",
        {},
        h(
          "div.card-head",
          {},
          h("h2", { text: "Watching" }),
          h("span.aside", { text: "What Athena keeps an eye on" }),
          h(
            "div.actions",
            {},
            h("button.button.small", { type: "button", text: "Check now", disabled: !w.items.length || w.checking || !w.every_minutes, onclick: checkNow }),
            h("button.button.small.primary", { type: "button", text: "Watch for…", onclick: () => watchForm() })
          )
        ),
        h("p.note", {}, icon("info"), h("span", { text: about })),
        last,
        items
      )
    )
  );
  for (const d of panel.querySelectorAll("details[data-keep]")) if (opened.has(d.dataset.keep)) d.open = true;
  // While it's checking, or about to, follow how it's going.
  if (w.checking || w.asked) watching_.timer = setTimeout(() => location.hash === "#watching" && loadWatching().catch(() => {}), 3000);
}

async function checkNow(event) {
  event.target.disabled = true;
  try {
    watching_.data = await call("POST", "/api/watches/check", {});
    render();
    toast("Athena will check as soon as it's free.");
  } catch (e) {
    toast(e.message, true);
    event.target.disabled = false;
  }
}

async function stopWatching(event, item) {
  event.target.disabled = true;
  try {
    watching_.data = await call("DELETE", `/api/watches/${item.id}`);
    render();
    toast("Stopped watching it.");
  } catch (e) {
    toast(e.message, true);
    event.target.disabled = false;
  }
}

// Something to watch for, new or to change: what, its key, and until when, or with no end.
function watchForm(item) {
  const what = h("textarea", { required: true, rows: 3, maxLength: 300, placeholder: "New emails about jobs" });
  what.value = item ? item.what : "";
  const key = h("input", { type: "text", maxLength: 40, value: item ? item.key : "", placeholder: "made from what's watched", autocapitalize: "none", spellcheck: false, "aria-label": "Key" });
  const tomorrow = isoDay(Date.now() / 1000 + 86400);
  const until = h("input", { type: "date", min: tomorrow, required: true, value: isoDay(item && item.until ? item.until : Date.now() / 1000 + 14 * 86400), "aria-label": "Until" });
  const noEnd = h("input", { type: "checkbox", checked: Boolean(item && item.until === null), onchange: () => (until.disabled = noEnd.checked) });
  until.disabled = noEnd.checked;
  const error = h("p.form-error", { role: "alert" });
  const dialog = document.getElementById("dialog");
  const save = h("button.button.primary", { type: "submit", text: item ? "Save" : "Watch" });
  const form = h(
    "form.task-form",
    {
      onsubmit: async (event) => {
        event.preventDefault();
        save.disabled = true;
        error.textContent = "";
        try {
          const body = { what: what.value, key: key.value.trim(), until: noEnd.checked ? null : until.value };
          watching_.data = await call("POST", item ? `/api/watches/${item.id}` : "/api/watches", body);
          dialog.close();
          render();
          toast(item ? "Saved." : "Watching it.");
        } catch (e) {
          error.textContent = e.message;
          save.disabled = false;
        }
      },
    },
    h("h2", { text: item ? "Edit what's watched" : "Watch for something" }),
    h("label.field", {}, h("span.field-label", { text: "What to watch for" }), h("span.field-help", { text: "And when it's happened. Athena checks with its tools, such as email or prices." }), what),
    h("label.field", {}, h("span.field-label", { text: "Key" }), h("span.field-help", { text: "What a DAG's triggers call it: watch:<key>. Lower case, with hyphens." }), key),
    h("div.field", {}, h("span.field-label", { text: "Until" }), h("div.until", {}, until, h("label.check", {}, noEnd, h("span", { text: "No end: check every time" })))),
    error,
    h("div.tool-actions", {}, h("button.button", { type: "button", text: "Cancel", onclick: () => dialog.close() }), save)
  );
  dialog.onclose = null;
  dialog.replaceChildren(form);
  if (!dialog.open) dialog.showModal();
  what.focus();
}
