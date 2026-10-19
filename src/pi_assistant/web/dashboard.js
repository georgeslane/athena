// Athena's dashboard. Plain JavaScript, no build step: it asks Athena for JSON (see dashboard.py)
// and builds the page from it. Everything shown comes from Athena, so it's added as text, never HTML.
// This is where it starts: signing in, the tabs, and following what Athena is doing. Each tab is a
// module of its own, and core.js has what they share.

import { appShown, call, h, now, plural, sleep, state, toast } from "./core.js";
import { renderOverview, renderStatus } from "./status.js";
import { loadTools } from "./tools.js";
import { loadMemory } from "./memory.js";
import { loadSkills } from "./skills.js";
import { loadBoard } from "./board.js";
import { loadTasks } from "./tasks.js";

// -- signing in -------------------------------------------------------------------------------------------

export function showSignIn() {
  if (state.watching) state.watching.abort();
  document.getElementById("app").hidden = true;
  document.getElementById("sign-in").hidden = false;
  document.getElementById("password").focus();
}

async function signIn(event) {
  event.preventDefault();
  const form = event.target;
  const error = document.getElementById("sign-in-error");
  const button = form.querySelector("button");
  error.textContent = "";
  button.disabled = true;
  try {
    await call("POST", "/api/login", { password: form.password.value });
    form.password.value = "";
    document.getElementById("sign-in").hidden = true;
    await showApp();
  } catch (e) {
    error.textContent = e.message;
  } finally {
    button.disabled = false;
  }
}

export async function signOut() {
  await call("POST", "/api/logout", {}).catch(() => {});
  location.reload();
}

// -- the app ------------------------------------------------------------------------------------------

async function start() {
  document.getElementById("sign-in-form").addEventListener("submit", signIn);
  window.addEventListener("hashchange", showTab);
  document.addEventListener("visibilitychange", () => !document.hidden && appShown() && refresh());
  setInterval(tick, 1000);
  setInterval(() => !document.hidden && appShown() && refresh(), 30000);
  for (;;) {
    try {
      await showApp();
      return;
    } catch (e) {
      if (e.status === 401) return; // the sign-in form is showing
      document.getElementById("app").hidden = false;
      setOffline(true);
      await sleep(5000);
    }
  }
}

async function showApp() {
  await loadOverview();
  document.getElementById("app").hidden = false;
  showTab();
  if (!state.watching) watchStatus();
}

function refresh() {
  loadOverview().catch(() => {});
  if (location.hash === "#tools") loadTools().catch(() => {});
  if (location.hash === "#memory") loadMemory().catch(() => {});
  if (location.hash === "#tasks") loadTasks().catch(() => {});
  if (location.hash === "#skills") loadSkills().catch(() => {});
  if (location.hash === "#board") loadBoard().catch(() => {});
  if (!state.watching) watchStatus();
}

function showTab() {
  const tab = { "#tools": "tools", "#memory": "memory", "#tasks": "tasks", "#skills": "skills", "#board": "board" }[location.hash] || "status";
  for (const link of document.querySelectorAll(".tab")) {
    if (link.dataset.tab === tab) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const panel of document.querySelectorAll(".panel")) panel.hidden = panel.dataset.panel !== tab;
  if (tab === "tools") loadTools().catch((e) => toast(e.message, true));
  if (tab === "memory") loadMemory().catch((e) => toast(e.message, true));
  if (tab === "tasks") loadTasks().catch((e) => toast(e.message, true));
  if (tab === "skills") loadSkills().catch((e) => toast(e.message, true));
  if (tab === "board") loadBoard().catch((e) => toast(e.message, true));
  window.scrollTo(0, 0);
}

function setOffline(offline) {
  if (state.offline === offline) return;
  state.offline = offline;
  document.getElementById("offline").hidden = !offline;
  renderStatus();
}

// Follow what Athena is doing: each request waits until something changes, or 25 seconds pass.
async function watchStatus() {
  const controller = new AbortController();
  state.watching = controller;
  let version = "";
  while (!controller.signal.aborted) {
    try {
      const wait = version ? "&wait=25" : "";
      const data = await call("GET", `/api/status?after=${encodeURIComponent(version)}${wait}`, undefined, controller.signal);
      const was = state.status && state.status.status.state;
      setStatus(data);
      setOffline(false);
      if (version && was !== "idle" && data.status.state === "idle") loadOverview().catch(() => {}); // new numbers
      version = data.version;
    } catch (e) {
      if (controller.signal.aborted || e.status === 401) break;
      setOffline(true);
      version = "";
      await sleep(5000);
    }
  }
  if (state.watching === controller) state.watching = null;
}

function setStatus(data) {
  state.status = data;
  state.skew = data.now - Date.now() / 1000;
  renderStatus();
}

export async function loadOverview() {
  const overview = await call("GET", "/api/overview");
  state.overview = overview;
  state.timezone = overview.assistant.timezone;
  for (const el of document.querySelectorAll("[data-name]")) el.textContent = overview.assistant.name;
  document.title = overview.assistant.name;
  setStatus({ version: overview.version, now: overview.now, status: overview.status });
  setOffline(false);
  renderOverview();
  showReviewCounts();
  // Athena asks the model server how it is in the background: ask again for its answer.
  if (overview.model.reachable === null) setTimeout(() => loadOverview().catch(() => {}), 2500);
}

// On the Memory and Skills tabs, how many of Athena's suggestions are waiting for you.
function showReviewCounts() {
  const review = state.overview.review;
  for (const [tab, n] of [["memory", review.memories], ["skills", review.skills]]) {
    const link = document.querySelector(`.tab[data-tab="${tab}"]`);
    let count = link.querySelector(".tab-count");
    if (!n) {
      if (count) count.remove();
      continue;
    }
    if (!count) link.append((count = h("span.tab-count")));
    count.textContent = n > 9 ? "9+" : String(n);
    count.title = `${plural(n, "suggestion")} to review`;
  }
}

export function setReviewCount(kind, n) {
  if (!state.overview) return;
  state.overview.review[kind] = n;
  showReviewCounts();
}

function tick() {
  if (document.hidden || !state.status) return;
  if (state.status.status.state !== "idle" || Math.floor(now()) % 30 === 0) renderStatus();
}

start();
