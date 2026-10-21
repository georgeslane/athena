// The Tools tab: built-in tools and MCP servers, their settings and keys, and adding servers.

import { ask, breakable, call, clock, h, icon, now, state, toast } from "./core.js";
import { loadOverview } from "./dashboard.js";

// -- the Tools tab --------------------------------------------------------------------------------------

let polling = null;
let onDemand = null; // the card choosing which tools are found when they're needed

export async function loadTools() {
  showTools(await call("GET", "/api/tools"));
}

function showTools(data) {
  state.tools = data;
  const panel = document.getElementById("tab-tools");
  if (!panel.querySelector(".tools")) panel.replaceChildren(toolsPage());
  const more = data.tools.filter((t) => !t.builtin && !t.added);
  fillList("builtin", data.tools.filter((t) => t.builtin));
  fillList("servers", data.tools.filter((t) => !t.builtin && t.added));
  fillList("more", more);
  panel.querySelector("[data-group='more']").hidden = !more.length;
  onDemand.update(data.on_demand, data.editable && !data.applying);
  const ids = new Set(data.tools.map((t) => t.id));
  for (const id of [...state.cards.keys()]) if (!ids.has(id)) state.cards.delete(id);

  const banner = panel.querySelector(".tools-banner");
  const problem = data.error || (!data.editable && "Athena was started without a config.toml, so these can't be changed here.");
  banner.hidden = !data.applying && !problem;
  banner.classList.toggle("info", !problem);
  banner.replaceChildren(problem || h("span", {}, h("span.spinner"), " Applying your changes. New messages wait until it's done."));
  for (const card of state.cards.values()) card.setEditable(data.editable && !data.applying);

  clearTimeout(polling);
  if (data.applying) polling = setTimeout(() => loadTools().catch(() => {}), 1200);
  else if (state.overview && state.overview.tools.applying) loadOverview().catch(() => {});
}

function toolsPage() {
  onDemand = new OnDemand();
  return h(
    "div.tools",
    {},
    h(
      "div.tools-intro",
      {},
      h("p", { text: "Switch tools on and off, and change their settings and keys. Changes apply straight away, once Athena has finished what it's doing." }),
      h("button.button.small", { type: "button", text: "Reconnect all", onclick: reconnect })
    ),
    h("div.banner.tools-banner", { role: "status", hidden: true }),
    h("h2.label.group-title", { text: "Built in" }),
    h("div.tool-list", { "data-list": "builtin" }),
    h("h2.label.group-title", { text: "MCP servers" }),
    h("div.tool-list", { "data-list": "servers" }),
    h("div", { "data-group": "more" }, h("h2.label.group-title", { text: "Recommended, not set up yet" }), h("div.tool-list", { "data-list": "more" })),
    h("h2.label.group-title", { text: "In every message" }),
    onDemand.el,
    h("h2.label.group-title", { text: "Another server" }),
    new AddServer().el
  );
}

function fillList(name, tools) {
  const list = document.querySelector(`[data-list='${name}']`);
  const cards = tools.map((data) => {
    let card = state.cards.get(data.id);
    if (card) card.update(data);
    else state.cards.set(data.id, (card = new ToolCard(data)));
    return card.el;
  });
  // Only move cards if the list has changed: moving one takes the focus from a field you're typing in.
  const same = cards.length === list.children.length && cards.every((el, i) => list.children[i] === el);
  if (!same) list.replaceChildren(...cards);
}

async function reconnect(event) {
  const button = event.target;
  button.disabled = true;
  try {
    showTools(await call("POST", "/api/reconnect", {}));
    toast("Reconnecting every server…");
  } catch (e) {
    toast(e.message, true);
  } finally {
    button.disabled = false;
  }
}

async function change(id, body) {
  showTools(await call("POST", `/api/tools/${encodeURIComponent(id)}`, body));
}

const isRef = (value) => typeof value === "string" && /^\$\{[A-Za-z_][A-Za-z0-9_]*\}$/.test(value);

// One tool: its name and state, a switch, and its settings when you open it.
class ToolCard {
  constructor(data) {
    this.data = data;
    this.open = false;
    this.editable = true;
    this.error = "";
    this.draft = null; // what you've changed and not saved
    this.toggle = h("input", { type: "checkbox", role: "switch", onchange: () => this.switched() });
    this.state = h("span.tool-state", {}, h("span.dot"), h("span.text"));
    this.title = h("span.title");
    this.tag = h("span.tag", { text: "Added", hidden: true });
    this.summary = h("span.tool-summary");
    this.opener = h(
      "button.tool-open",
      { type: "button", "aria-expanded": "false", onclick: () => this.setOpen(!this.open) },
      h("span.tool-title", {}, icon("chevron", "chevron"), this.title, this.tag),
      this.summary,
      this.state
    );
    this.body = h("div.tool-body", { hidden: true });
    this.el = h("article.card.tool", { "data-open": "false" }, h("div.tool-head", {}, this.opener, h("label.switch", {}, this.toggle, h("span"))), this.body);
    this.renderHead();
  }

  update(data) {
    this.data = data;
    this.renderHead();
    if (this.open && !this.isDirty() && !this.el.contains(document.activeElement)) this.renderBody();
  }

  setEditable(editable) {
    this.editable = editable;
    this.toggle.disabled = !editable;
    for (const el of this.body.querySelectorAll("[data-save]")) el.disabled = !editable;
  }

  renderHead() {
    const d = this.data;
    this.title.textContent = d.title;
    this.tag.hidden = !d.custom;
    this.summary.textContent = d.summary;
    this.summary.hidden = !d.summary;
    this.state.dataset.state = d.state;
    this.state.querySelector(".text").textContent = d.detail;
    this.toggle.checked = d.enabled;
    this.toggle.setAttribute("aria-label", `Use ${d.title}`);
  }

  setOpen(open) {
    this.open = open;
    this.el.dataset.open = String(open);
    this.opener.setAttribute("aria-expanded", String(open));
    this.body.hidden = !open;
    if (open) this.renderBody();
    else this.draft = null;
  }

  isDirty() {
    return !!this.draft && Object.keys(this.changes()).length > 0;
  }

  async switched() {
    const enabled = this.toggle.checked;
    this.error = "";
    this.toggle.disabled = true;
    try {
      await change(this.data.id, { enabled });
      toast(`${this.data.title} is ${enabled ? "on" : "off"}. Applying…`);
    } catch (e) {
      this.toggle.checked = !enabled;
      this.error = e.message;
      if (this.open) this.renderBody();
      else this.setOpen(true);
    } finally {
      this.toggle.disabled = !this.editable;
    }
  }

  // -- its settings ------------------------------------------------------------------------------------

  renderBody() {
    const d = this.data;
    this.draft = {
      fields: Object.fromEntries(d.fields.map((f) => [f.key, structuredClone(f.value)])),
      secrets: {},
      tools: d.tools && d.tools[0] && d.tools[0].asks !== null ? Object.fromEntries(d.tools.map((t) => [t.name, { on: t.on, asks: t.asks }])) : null,
    };
    const parts = [];
    if (d.held && d.held.length) parts.push(this.heldEditor());
    if (d.security) parts.push(this.securityNotice());
    if (d.asks) parts.push(h("p.note", {}, icon("lock"), h("span", { text: d.asks })));
    if (d.setup) parts.push(h("p.note.setup", {}, icon("info"), h("span", { text: d.setup })));
    // A server you added runs either as a command on the Pi or at a URL: show the settings for whichever it is.
    const byUrl = d.custom && d.fields.some((f) => f.key === "url" && f.value);
    const unused = d.custom ? (byUrl ? ["command", "args", "env"] : ["url", "headers"]) : [];
    for (const f of d.fields) if (!unused.includes(f.key)) parts.push(this.fieldEditor(f));
    if (!d.custom) for (const s of d.secrets) parts.push(this.secretEditor(s));
    if (d.tools) parts.push(this.toolsEditor());
    const error = h("p.form-error", { role: "alert", text: this.error });
    const save = h("button.button.primary", { type: "button", text: "Save", "data-save": true, disabled: !this.editable, onclick: () => this.save(save, error) });
    const cancel = h("button.button.quiet", {
      type: "button",
      text: "Cancel",
      onclick: () => {
        this.error = "";
        this.setOpen(false);
      },
    });
    const remove = d.custom ? h("button.button.danger.small", { type: "button", text: "Remove server", "data-save": true, disabled: !this.editable, onclick: () => this.remove() }) : null;
    this.body.replaceChildren(...parts, error, h("div.tool-actions", {}, save, cancel, h("span.grow"), remove));
  }

  fieldEditor(f) {
    const label = h("span.field-label", { text: f.label });
    const help = f.help ? h("span.field-help", { text: f.help }) : null;
    const set = (value) => (this.draft.fields[f.key] = value);
    if (f.kind === "map") return h("div.field", {}, label, help, this.mapEditor(f));
    if (f.kind === "lines") {
      const area = h("textarea", { rows: 3, spellcheck: false, autocapitalize: "none", oninput: () => set(area.value.split("\n")) });
      area.value = f.value.join("\n");
      return h("label.field", {}, label, help, area);
    }
    if (f.kind === "select") {
      const select = h("select", { onchange: () => set(select.value) }, f.options.map((o) => h("option", { value: o.value, text: o.label })));
      select.value = f.value;
      return h("label.field", {}, label, help, select);
    }
    const input = h("input", { type: "text", spellcheck: false, autocapitalize: "none", autocomplete: "off", oninput: () => set(input.value) });
    input.value = f.value;
    return h("label.field", {}, label, help, input);
  }

  // Names and values, like news feeds. A server you added can keep a value in .env instead: a secret.
  mapEditor(f) {
    const secrets = this.data.custom && (f.key === "env" || f.key === "headers");
    const rows = Object.entries(f.value).map(([name, value]) => ({ name, value, ref: isRef(value) ? value : null, secret: isRef(value), typed: "" }));
    const box = h("div.map");
    const sync = () => {
      const result = {};
      for (const row of rows) {
        if (!row.name.trim()) continue;
        if (row.ref) result[row.name.trim()] = row.typed ? { secret: row.typed } : row.ref;
        else result[row.name.trim()] = row.secret ? { secret: row.value } : row.value;
      }
      this.draft.fields[f.key] = result;
    };
    const draw = () => {
      const cls = secrets ? "with-secret" : "";
      const head = rows.length ? [h("div.map-row.map-head", { class: cls }, h("span", { text: f.columns[0] }), h("span", { text: f.columns[1] }))] : [];
      box.replaceChildren(
        ...head,
        ...rows.map((row, i) => {
          const name = h("input", { type: "text", value: row.name, placeholder: f.columns[0], "aria-label": f.columns[0], spellcheck: false, autocapitalize: "none", oninput: () => ((row.name = name.value), sync()) });
          const value = h("input", {
            type: row.secret ? "password" : "text",
            value: row.ref ? "" : row.value,
            placeholder: row.ref ? `In .env as ${row.ref.slice(2, -1)}. Type to replace` : f.columns[1],
            "aria-label": f.columns[1],
            spellcheck: false,
            autocapitalize: "none",
            autocomplete: "off",
            oninput: () => {
              if (row.ref) row.typed = value.value;
              else row.value = value.value;
              sync();
            },
          });
          let secret = null;
          if (secrets && row.ref) secret = h("span.check", { text: "Secret" });
          else if (secrets) {
            secret = h("label.check", {}, h("input", { type: "checkbox", checked: row.secret, onchange: (e) => ((row.secret = e.target.checked), sync(), draw()) }), "Secret");
          }
          const remove = h("button.button.small.remove", { type: "button", text: "×", "aria-label": `Remove ${row.name || "this row"}`, onclick: () => (rows.splice(i, 1), sync(), draw()) });
          return h("div.map-row", { class: cls }, name, value, secret, remove);
        }),
        h(
          "div",
          {},
          h("button.button.small", {
            type: "button",
            text: `Add ${f.item}`,
            onclick: () => {
              rows.push({ name: "", value: "", ref: null, secret: false, typed: "" });
              draw();
              box.querySelectorAll(".map-row:not(.map-head) input[type='text']")[rows.length - 1].focus();
            },
          })
        )
      );
    };
    draw();
    return box;
  }

  secretEditor(s) {
    const input = h("input", {
      type: "password",
      autocomplete: "off",
      spellcheck: false,
      autocapitalize: "none",
      "aria-label": s.label,
      placeholder: s.set ? "Set. Type a new one to replace it" : "Not set yet",
      oninput: () => (this.draft.secrets[s.name] = input.value),
    });
    const remove = s.set && !s.required ? h("button.button.small.quiet", { type: "button", text: "Remove", "data-save": true, onclick: () => this.removeSecret(s) }) : null;
    return h(
      "div.secret",
      {},
      h("div.secret-head", {}, h("span.field-label", { text: s.label }), h("span.badge", { class: s.set ? "set" : "unset", text: s.set ? "Set" : "Not set" })),
      s.help ? h("span.field-help", { text: s.help }) : null,
      h("div.secret-input", {}, input, remove)
    );
  }

  toolsEditor() {
    const d = this.data;
    if (!this.draft.tools) {
      return h("div.field", {}, h("span.field-label", { text: "Its tools" }), h("div.chips", {}, d.tools.map((t) => h("span.chip", { text: t.name }))));
    }
    const rows = d.tools.map((t) => {
      const choice = this.draft.tools[t.name];
      const row = h("div.server-tool", { class: choice.on ? "" : "off" });
      const asks = h("input", { type: "checkbox", checked: choice.asks, disabled: !choice.on, "aria-label": `${t.name} asks first`, onchange: () => (choice.asks = asks.checked) });
      const on = h("input", {
        type: "checkbox",
        role: "switch",
        checked: choice.on,
        "aria-label": `Use ${t.name}`,
        onchange: () => {
          choice.on = on.checked;
          if (on.checked) choice.asks = asks.checked = true; // a tool you've just switched on asks first until you say otherwise
          asks.disabled = !on.checked;
          row.classList.toggle("off", !on.checked);
        },
      });
      row.append(
        h("div", {}, h("span.name", {}, breakable(t.name)), t.description ? h("span.description", { text: t.description }) : null),
        h("label.switch.small", {}, on, h("span")),
        h("label.check", {}, asks)
      );
      return row;
    });
    return h(
      "div.field",
      {},
      h("span.field-label", { text: "Its tools" }),
      h("span.field-help", { text: "Only let a tool run without asking if it can't change anything, or send what's in the conversation anywhere." }),
      h("div.server-tools", {}, h("div.server-tools-head", {}, h("span", { text: "Tool" }), h("span", { text: "Use" }), h("span", { text: "Asks first" })), rows)
    );
  }

  // Tools that are new or changed since you approved them: what changed, and a button to approve them.
  // Its dependencies, as Athena last checked them for known vulnerabilities, and fixing them.
  securityNotice() {
    const s = this.data.security;
    const when = clock(s.checked, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    const fixes = s.fixed.map((line) => line.replace(" -> ", " → "));
    const updated = fixes.length ? h("p.hint", { text: `Updated here for security fixes: ${fixes.join("; ")}.` }) : null;
    if (s.clean === null) return h("div", {}, h("p.hint", { text: `Its dependencies couldn't be checked on ${when}: ${s.summary}.` }), updated);
    if (s.clean) return h("div", {}, h("p.hint", { text: `Its dependencies were checked on ${when}: no known vulnerabilities.` }), updated);
    const error = h("p.form-error", { role: "alert", text: s.error || "" });
    const fixable = s.findings.some((f) => f.fixed);
    const fix = h("button.button.primary", {
      type: "button",
      text: "Fix",
      "data-save": true,
      disabled: !this.editable,
      onclick: async () => {
        fix.disabled = true;
        error.textContent = "";
        try {
          const data = await call("POST", `/api/tools/${encodeURIComponent(this.data.id)}/fix`, {});
          showTools(data);
          const now = data.tools.find((t) => t.id === this.data.id);
          toast(now && now.security && now.security.clean ? "Fixed, and the server's restarted." : "Checked: see what's left.", !(now && now.security && now.security.clean));
        } catch (e) {
          error.textContent = e.message;
          fix.disabled = !this.editable;
        }
      },
    });
    return h(
      "section.held",
      {},
      h("p.note.setup", {}, icon("info"), h("span", { text: `On ${when}, Athena found known vulnerabilities in what this server uses.` + (fixable ? " Fix updates each to the version that fixes it, and nothing else, then restarts the server." : "") })),
      s.findings.map((f) =>
        h(
          "div.held-tool",
          {},
          h("span.held-name", {}, breakable(`${f.package} ${f.version}`), h("span.tag", { text: f.fixed ? `Fixed in ${f.fixed}` : "No fix yet" })),
          h("p.hint", { text: `${f.ids.join(", ")}: ${f.summary}` })
        )
      ),
      updated,
      error,
      fixable ? h("div.tool-actions", {}, fix) : h("p.hint", { text: "There's no fixed version yet. Switch the server off if that worries you." })
    );
  }

  heldEditor() {
    const d = this.data;
    const pretty = (value) => JSON.stringify(value, null, 2);
    const compare = (label, before, now) =>
      h("div.compare", {}, h("span.compare-label", { text: label }), h("span.compare-tag", { text: "Before" }), h("pre.before", { text: before }), h("span.compare-tag", { text: "Now" }), h("pre.now", { text: now }));
    const items = d.held.map((t) => {
      const parts = [h("span.held-name", {}, breakable(t.name), h("span.tag", { text: t.approved ? "Changed" : "New" }))];
      if (!t.approved) {
        parts.push(h("pre.now", { text: t.now.description }), h("pre.now", { text: pretty(t.now.parameters) }));
      } else {
        if (t.now.description !== t.approved.description) parts.push(compare("Description", t.approved.description, t.now.description));
        if (pretty(t.now.parameters) !== pretty(t.approved.parameters)) parts.push(compare("Arguments", pretty(t.approved.parameters), pretty(t.now.parameters)));
      }
      return h("div.held-tool", {}, parts);
    });
    const error = h("p.form-error", { role: "alert" });
    const approve = h("button.button.primary", {
      type: "button",
      text: d.held.length === 1 ? "Approve it" : `Approve all ${d.held.length}`,
      "data-save": true,
      disabled: !this.editable,
      onclick: async () => {
        approve.disabled = true;
        try {
          const tools = Object.fromEntries(d.held.map((t) => [t.name, t.fingerprint]));
          showTools(await call("POST", `/api/tools/${encodeURIComponent(d.id)}/approve`, { tools }));
          toast(`Approved. Athena can use ${d.held.length === 1 ? "it" : "them"} now.`);
        } catch (e) {
          error.textContent = e.message;
          approve.disabled = !this.editable;
        }
      },
    });
    return h(
      "section.held",
      {},
      h("p.note.setup", {}, icon("info"), h("span", { text: "These tools are new, or have changed since you approved them, so Athena won't use them until you approve them. Check that each only describes what the tool does: a description can also carry instructions to the model." })),
      items,
      error,
      h("div.tool-actions", {}, approve)
    );
  }

  changes() {
    const d = this.data;
    const body = {};
    const fields = {};
    for (const f of d.fields) {
      let value = this.draft.fields[f.key];
      if (f.kind === "lines") value = value.map((v) => v.trim()).filter(Boolean);
      if (JSON.stringify(value) !== JSON.stringify(f.value)) fields[f.key] = value;
    }
    if (Object.keys(fields).length) body.fields = fields;
    const secrets = Object.fromEntries(Object.entries(this.draft.secrets).filter(([, v]) => v.trim()));
    if (Object.keys(secrets).length) body.secrets = secrets;
    const tools = this.draft.tools;
    if (tools && d.tools.some((t) => t.on !== tools[t.name].on || (t.on && t.asks !== tools[t.name].asks))) body.tools = tools;
    return body;
  }

  async save(button, error) {
    const body = this.changes();
    if (!Object.keys(body).length) return this.setOpen(false);
    button.disabled = true;
    error.textContent = this.error = "";
    try {
      await change(this.data.id, body);
      this.setOpen(false);
      toast(`Saved ${this.data.title}. Applying…`);
    } catch (e) {
      error.textContent = this.error = e.message;
    } finally {
      button.disabled = !this.editable;
    }
  }

  async removeSecret(s) {
    if (!(await ask({ title: `Remove the ${s.label.toLowerCase()}?`, body: [h("p", { text: "It's deleted from .env on the Pi." })], confirm: "Remove", danger: true }))) return;
    try {
      await change(this.data.id, { secrets: { [s.name]: null } });
      toast("Removed. Applying…");
    } catch (e) {
      toast(e.message, true);
    }
  }

  async remove() {
    const d = this.data;
    if (!(await ask({ title: `Remove ${d.title}?`, body: [h("p", { text: "It's taken out of config.toml. Any keys it had stay in .env." })], confirm: "Remove", danger: true }))) return;
    try {
      showTools(await call("DELETE", `/api/tools/${encodeURIComponent(d.id)}`));
      toast(`Removed ${d.title}.`);
    } catch (e) {
      toast(e.message, true);
    }
  }
}

// A form for an MCP server that isn't one of the recommended ones.
// Every tool the model has, by how much it adds to each message: those rarely used can be found when needed instead.
class OnDemand {
  constructor() {
    this.el = h("section.card.on-demand");
    this.data = null;
    this.chosen = null; // the names you've ticked, until saved
    this.editable = false;
  }

  update(data, editable) {
    const same = this.data && JSON.stringify(this.data) === JSON.stringify(data);
    this.editable = editable;
    if (!same || !this.chosen || !this.dirty()) {
      this.data = data;
      this.chosen = new Set(data.tools.filter((t) => t.on_demand).map((t) => t.name));
    }
    this.render();
  }

  dirty() {
    const saved = this.data.tools.filter((t) => t.on_demand).map((t) => t.name);
    return saved.length !== this.chosen.size || saved.some((n) => !this.chosen.has(n));
  }

  render() {
    const d = this.data;
    const save = h("button.button.small", { type: "button", text: "Save", disabled: !this.editable || !this.dirty() });
    const error = h("p.form-error", { role: "alert" });
    const rows = [...d.tools]
      .sort((a, b) => b.tokens - a.tokens || a.name.localeCompare(b.name))
      .map((t) => {
        const box = h("input", {
          type: "checkbox",
          checked: this.chosen.has(t.name),
          disabled: !this.editable || Boolean(t.pattern),
          title: t.pattern ? `Found when needed because of “${t.pattern}” in config.toml` : null,
          "aria-label": `Find ${t.name} only when it's needed`,
          onchange: () => {
            if (box.checked) this.chosen.add(t.name);
            else this.chosen.delete(t.name);
            save.disabled = !this.editable || !this.dirty();
          },
        });
        return h(
          "div.on-demand-tool",
          {},
          h("div", {}, h("span.name", {}, breakable(t.name)), h("span.description", { text: t.from })),
          h("span.number", { text: t.tokens.toLocaleString() }),
          h("span.number", { text: t.used.toLocaleString() }),
          h("label.check", {}, box)
        );
      });
    save.onclick = async () => {
      save.disabled = true;
      error.textContent = "";
      try {
        const saved = await call("POST", "/api/tools/on-demand", { tools: [...this.chosen] });
        this.chosen = null;
        showTools(saved);
        toast("Saved. Applying…");
      } catch (e) {
        error.textContent = e.message;
        save.disabled = false;
      }
    };
    this.el.replaceChildren(
      h(
        "div.card-head",
        {},
        h("h2", { text: "Tools in every message" }),
        h("span.aside", { text: `About ${d.in_prompt.toLocaleString()} tokens` + (d.finding ? `, ${d.finding.toLocaleString()} of them for finding the rest` : "") })
      ),
      h("p.hint", {
        text:
          "Athena reads every tool's description with each message, which takes the Mac a moment. Tools you rarely use can be found " +
          "when they're needed instead: Athena looks them up first, and they work just the same. Looking them up takes a little room " +
          `itself, about 200 tokens and 30 more for each, so it's worth it for the bigger ones. Used: in the last ${d.days} days.`,
      }),
      h(
        "div.server-tools.on-demand-tools",
        {},
        h("div.on-demand-tool.head", {}, h("span", { text: "Tool" }), h("span", { text: "Tokens" }), h("span", { text: "Used" }), h("span", { text: "When needed" })),
        rows
      ),
      error,
      h("div.actions", {}, save)
    );
  }
}

class AddServer {
  constructor() {
    this.el = h("section.card");
    this.collapsed();
  }

  collapsed() {
    this.el.replaceChildren(
      h(
        "div.tools-intro",
        { style: { margin: "0" } },
        h(
          "p",
          {},
          "Most servers are in the ",
          h("a", { href: "https://registry.modelcontextprotocol.io", target: "_blank", rel: "noopener noreferrer", text: "MCP Registry" }),
          ". Choose carefully: a server sees what Athena sends it, and can act for you."
        ),
        h("button.button", { type: "button", text: "Add a server…", onclick: () => this.expanded() })
      )
    );
  }

  expanded() {
    const v = { name: "", runs: "command", command: "uvx", args: "", url: "", env: [], headers: [] };
    const error = h("p.form-error", { role: "alert" });
    const where = h("div.add-form");
    const name = h("input", { type: "text", placeholder: "weather", autocapitalize: "none", spellcheck: false, oninput: () => (v.name = name.value) });
    const rowsEditor = (list, columns, noun) => {
      const box = h("div.map");
      const draw = () =>
        box.replaceChildren(
          ...list.map((row, i) => {
            const key = h("input", { type: "text", value: row.key, placeholder: columns[0], "aria-label": columns[0], autocapitalize: "none", spellcheck: false, oninput: () => (row.key = key.value) });
            const value = h("input", { type: row.secret ? "password" : "text", value: row.value, placeholder: columns[1], "aria-label": columns[1], autocapitalize: "none", spellcheck: false, autocomplete: "off", oninput: () => (row.value = value.value) });
            return h(
              "div.map-row.with-secret",
              {},
              key,
              value,
              h("label.check", {}, h("input", { type: "checkbox", checked: row.secret, onchange: (e) => ((row.secret = e.target.checked), draw()) }), "Secret"),
              h("button.button.small.remove", { type: "button", text: "×", "aria-label": "Remove", onclick: () => (list.splice(i, 1), draw()) })
            );
          }),
          h("div", {}, h("button.button.small", { type: "button", text: `Add ${noun}`, onclick: () => (list.push({ key: "", value: "", secret: false }), draw()) }))
        );
      draw();
      return box;
    };
    const drawWhere = () => {
      if (v.runs === "command") {
        const command = h("input", { type: "text", value: v.command, autocapitalize: "none", spellcheck: false, oninput: () => (v.command = command.value) });
        const args = h("textarea", { rows: 3, placeholder: "some-weather-mcp==1.2.3", autocapitalize: "none", spellcheck: false, oninput: () => (v.args = args.value) });
        args.value = v.args;
        where.replaceChildren(
          h("label.field", {}, h("span.field-label", { text: "Command" }), h("span.field-help", { text: "uvx for Python servers from PyPI, or npx for npm ones, which need Node.js on the Pi." }), command),
          h("label.field", {}, h("span.field-label", { text: "Arguments" }), h("span.field-help", { text: "One per line. Pin a version, like name==1.2.3, so an update can't change what runs." }), args),
          h("div.field", {}, h("span.field-label", { text: "Environment" }), h("span.field-help", { text: "Settings it reads, like an API key. Tick Secret to keep a value in .env." }), rowsEditor(v.env, ["Name", "Value"], "a variable"))
        );
      } else {
        const url = h("input", { type: "url", value: v.url, placeholder: "https://example.com/mcp", autocapitalize: "none", spellcheck: false, oninput: () => (v.url = url.value) });
        where.replaceChildren(
          h("label.field", {}, h("span.field-label", { text: "URL" }), url),
          h("div.field", {}, h("span.field-label", { text: "Headers" }), h("span.field-help", { text: "Such as Authorization. Tick Secret to keep a value in .env." }), rowsEditor(v.headers, ["Header", "Value"], "a header"))
        );
      }
    };
    const runs = h(
      "div.choice",
      { role: "radiogroup", "aria-label": "Where it runs" },
      [
        ["command", "On the Pi"],
        ["url", "On the internet"],
      ].map(([value, label]) =>
        h("label", {}, h("input", { type: "radio", name: "runs", value, checked: v.runs === value, onchange: () => ((v.runs = value), drawWhere()) }), label)
      )
    );
    drawWhere();
    const add = h("button.button.primary", {
      type: "button",
      text: "Add server",
      onclick: async () => {
        const map = (rows) => Object.fromEntries(rows.filter((r) => r.key.trim()).map((r) => [r.key.trim(), r.secret ? { secret: r.value } : r.value]));
        const body = { name: v.name.trim() };
        if (v.runs === "command") Object.assign(body, { command: v.command.trim(), args: v.args.split("\n").map((a) => a.trim()).filter(Boolean), env: map(v.env) });
        else Object.assign(body, { url: v.url.trim(), headers: map(v.headers) });
        add.disabled = true;
        error.textContent = "";
        try {
          showTools(await call("POST", "/api/tools", body));
          toast(`Added ${body.name}. Connecting…`);
          this.collapsed();
        } catch (e) {
          error.textContent = e.message;
        } finally {
          add.disabled = false;
        }
      },
    });
    this.el.replaceChildren(
      h(
        "div.add-form",
        {},
        h("label.field", {}, h("span.field-label", { text: "Name" }), h("span.field-help", { text: "Lower-case letters, digits, - or _." }), name),
        h("div.field", {}, h("span.field-label", { text: "Where it runs" }), runs),
        where,
        h("p.note", {}, icon("lock"), h("span", { text: "Every tool it has asks first, until you choose otherwise once it's connected." })),
        error,
        h("div.tool-actions", {}, add, h("button.button.quiet", { type: "button", text: "Cancel", onclick: () => this.collapsed() }))
      )
    );
    name.focus();
  }
}
