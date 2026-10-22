// Athena's dashboard: what every tab uses. Helpers for building the page, talking to Athena, what it's
// said so far, and dialogs and toasts.

import { showSignIn } from "./dashboard.js";

// -- helpers ---------------------------------------------------------------------------------------

// h("div.card", {text: "hi", onclick: f}, ...children): an element, with classes, properties and children.
export function h(tag, props, ...children) {
  const [name, ...classes] = tag.split(".");
  const el = document.createElement(name);
  if (classes.length) el.className = classes.join(" ");
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null) continue;
    if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "text") el.textContent = value;
    else if (key === "class") value && el.classList.add(...value.split(" "));
    else if (key === "style") Object.assign(el.style, value);
    else if (typeof value === "boolean") {
      if (key in el) el[key] = value;
      else if (value) el.setAttribute(key, "");
    } else if (typeof value === "number" && key in el) el[key] = value;
    else el.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === "") continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const ICONS = {
  chevron: "M9 6l6 6-6 6",
  lock: "M7 11V8a5 5 0 0 1 10 0v3M5 11h14v10H5z",
  info: "M12 8h.01M11 12h1v5h1M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  plus: "M12 5v14M5 12h14",
  back: "M15 6l-6 6 6 6",
  send: "M5 12h13M13 6l6 6-6 6",
  stop: "M7 7h10v10H7z",
  shield: "M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6zM9 12l2 2 4-4",
  bell: "M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20a2 2 0 0 0 4 0",
  chevron_down: "M6 9l6 6 6-6",
};

export function icon(name, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  if (cls) svg.setAttribute("class", cls);
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

export const number = (n) => Number(n || 0).toLocaleString("en-GB");

// A tool's name, which a narrow screen can break after an underscore rather than mid-word.
export const breakable = (name) => name.split(/(?<=_)/).flatMap((part, i) => (i ? [h("wbr"), part] : [part]));
export const plural = (n, word, many) => `${number(n)} ${n === 1 ? word : many || word + "s"}`;

export function seconds(s) {
  if (!s) return "–";
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 90) return `${Math.round(s)} s`;
  return duration(s);
}

export function duration(s) {
  s = Math.max(0, Math.floor(s));
  const hours = Math.floor(s / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const secs = String(s % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${secs}` : `${minutes}:${secs}`;
}

export function ago(s) {
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return plural(Math.floor(s / 86400), "day") + " ago";
}

// A time in Athena's time zone, which is the one you live in.
export function clock(timestamp, options) {
  const date = new Date(timestamp * 1000);
  try {
    return new Intl.DateTimeFormat("en-GB", { timeZone: state.timezone, ...options }).format(date);
  } catch {
    return new Intl.DateTimeFormat("en-GB", options).format(date); // a time zone this browser doesn't know
  }
}

// A day in Athena's time zone as YYYY-MM-DD, as a date field has it.
export function isoDay(timestamp) {
  const [d, m, y] = clock(timestamp, { day: "2-digit", month: "2-digit", year: "numeric" }).split("/");
  return `${y}-${m}-${d}`;
}

const sameDay = (a, b) => clock(a, { dateStyle: "short" }) === clock(b, { dateStyle: "short" });
export const when = (t) => (sameDay(t, now()) ? clock(t, { hour: "2-digit", minute: "2-digit" }) : clock(t, { day: "numeric", month: "short" }));

// Athena's clock rather than this device's, for timing what it reports.
export const now = () => Date.now() / 1000 + state.skew;
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// -- talking to Athena --------------------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function call(method, path, body, signal) {
  const options = { method, credentials: "same-origin", cache: "no-store", signal, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "Can't reach Athena.");
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    // not JSON
  }
  if (response.status === 401 && path !== "/api/login") showSignIn();
  if (!response.ok) throw new ApiError(response.status, (data && data.error) || `Athena said ${response.status}.`);
  return data;
}

// -- state -------------------------------------------------------------------------------------------

export const state = {
  overview: null,
  status: null, // what Athena is doing: /api/status
  tools: null,
  cards: new Map(), // the Tools tab's cards, by tool id
  timezone: undefined,
  skew: 0,
  offline: false,
  show: "session", // on a phone, this session's numbers or all time's
  watching: null, // the AbortController of the request waiting for the status to change
};

export const appShown = () => !document.getElementById("app").hidden;

// -- dialogs and toasts ---------------------------------------------------------------------------------

export function ask({ title, body, confirm, danger, ready, watch }) {
  const dialog = document.getElementById("dialog");
  return new Promise((resolve) => {
    const ok = h(`button.button.${danger ? "danger.solid" : "primary"}`, { type: "button", text: confirm, disabled: ready ? !ready() : false });
    const cancel = h("button.button", { type: "button", text: "Cancel" });
    const done = (answer) => {
      resolve(answer);
      if (dialog.open) dialog.close();
    };
    ok.addEventListener("click", () => done(true));
    cancel.addEventListener("click", () => done(false));
    if (watch) watch.addEventListener("input", () => (ok.disabled = !ready()));
    dialog.onclose = () => resolve(false);
    dialog.replaceChildren(h("h2", { text: title }), ...body, h("div.tool-actions", {}, cancel, ok));
    dialog.showModal();
    (watch || cancel).focus();
  });
}

let toastTimer = null;
export function toast(message, error = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("error", error);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), error ? 6000 : 3500);
}


// Dates, as the tabs show them.
export const day = (date) => (date ? clock(Date.parse(date) / 1000, { day: "numeric", month: "short", year: "numeric" }) : "");
