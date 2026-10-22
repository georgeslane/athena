// Athena's dashboard. Plain JavaScript, no build step: it asks Athena for JSON (see dashboard.py)
// and builds the page from it. Everything shown comes from Athena, so it's added as text, never HTML.
// This is where it starts: signing in, the tabs, and following what Athena is doing. Each tab is a
// module of its own, and core.js has what they share.

import { appShown, call, h, now, plural, sleep, state, toast } from "./core.js";
import { renderOverview, renderStatus } from "./status.js";
import { loadTools } from "./tools.js";
import { loadMemory } from "./memory.js";
import { loadSkills } from "./skills.js";
import { loadWatching } from "./watching.js";
import { followAgents, loadAgents } from "./agents.js";
import { setUpNotifications } from "./notifications.js";

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
  setUpMenu();
  followViewport();
  document.addEventListener("visibilitychange", () => !document.hidden && appShown() && refresh());
  for (const [target, event] of [[document, "visibilitychange"], [window, "focus"], [window, "blur"]]) {
    target.addEventListener(event, lookingChanged);
  }
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
  setUpNotifications();
}

function refresh() {
  loadOverview().catch(() => {});
  if (location.hash === "#tools") loadTools().catch(() => {});
  if (location.hash === "#memory") loadMemory().catch(() => {});
  if (location.hash === "#watching") loadWatching().catch(() => {});
  if (location.hash.startsWith("#agents")) loadAgents().catch(() => {});
  if (location.hash === "#skills") loadSkills().catch(() => {});
  if (!state.watching) watchStatus();
}

const TABS = ["status", "agents", "watching", "memory", "skills", "tools"];

function showTab() {
  if (location.hash.startsWith("#chat")) {
    // From before agents: #chat/12, as an old notification or bookmark says, is agent 12.
    history.replaceState(null, "", location.hash.replace(/^#chat/, "#agents"));
  }
  const named = location.hash.slice(1).split("/")[0]; // #agents/12/dag is the Agents tab, showing agent 12's DAG
  const tab = TABS.includes(named) ? named : location.hash ? "status" : "agents";
  const was = document.querySelector(".tab[aria-current]");
  for (const link of document.querySelectorAll(".tab")) {
    if (link.dataset.tab === tab) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const panel of document.querySelectorAll(".panel")) panel.hidden = panel.dataset.panel !== tab;
  document.getElementById("brand-where").textContent = document.querySelector(`.tab[data-tab="${tab}"]`).title;
  menu_.close();
  if (tab === "agents") {
    loadAgents().catch((e) => toast(e.message, true));
    if (was && was.dataset.tab === "agents") return; // another agent, view or the list of them: stay where you are
  }
  if (tab === "tools") loadTools().catch((e) => toast(e.message, true));
  if (tab === "memory") loadMemory().catch((e) => toast(e.message, true));
  if (tab === "watching") loadWatching().catch((e) => toast(e.message, true));
  if (tab === "skills") loadSkills().catch((e) => toast(e.message, true));
  window.scrollTo(0, 0);
}

// -- the menu, on a phone -----------------------------------------------------------------------------------

const menu_ = { close: () => {} };

// On a phone the tabs are a menu, which the shield in the corner opens and closes. Elsewhere it's a link to the
// Status tab.
function setUpMenu() {
  const top = document.querySelector(".top");
  const brand = document.getElementById("brand");
  const backdrop = document.getElementById("menu-backdrop");
  const phone = window.matchMedia("(max-width: 719px)");
  const open = (yes) => {
    top.dataset.menu = yes ? "open" : "";
    backdrop.hidden = !yes;
    if (phone.matches) brand.setAttribute("aria-expanded", String(yes));
    else brand.removeAttribute("aria-expanded");
    brand.setAttribute("aria-label", phone.matches ? "Sections" : "Status");
    if (yes) (document.querySelector(".tab[aria-current]") || document.querySelector(".tab")).focus();
  };
  brand.addEventListener("click", (event) => {
    if (!phone.matches) return;
    event.preventDefault();
    open(top.dataset.menu !== "open");
  });
  backdrop.addEventListener("click", () => open(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && top.dataset.menu === "open") {
      open(false);
      brand.focus();
    }
  });
  document.getElementById("tabs").addEventListener("click", () => open(false));
  phone.addEventListener("change", () => open(false));
  menu_.close = () => open(false);
  open(false);
  // The shield says how many things are waiting for you, as the tabs in the menu do.
  const count = () => {
    const n = [...document.querySelectorAll(".tab .tab-count")].reduce((sum, el) => sum + (parseInt(el.textContent, 10) || 0), 0);
    const badge = document.getElementById("brand-count");
    badge.hidden = !n;
    badge.textContent = n > 9 ? "9+" : String(n);
  };
  new MutationObserver(count).observe(document.getElementById("tabs"), { childList: true, subtree: true, characterData: true });
}

// An agent fills the screen, less the header, and less the keyboard while it's up on a phone.
function followViewport() {
  const root = document.documentElement;
  const top = document.querySelector(".top");
  const update = () => {
    const height = window.visualViewport ? window.visualViewport.height : window.innerHeight;
    root.style.setProperty("--vvh", `${Math.round(height)}px`);
    root.style.setProperty("--top-h", `${top.offsetHeight}px`);
  };
  update();
  if (window.visualViewport) window.visualViewport.addEventListener("resize", update);
  window.addEventListener("resize", update);
  new ResizeObserver(update).observe(top);
}

function setOffline(offline) {
  if (state.offline === offline) return;
  state.offline = offline;
  document.getElementById("offline").hidden = !offline;
  renderStatus();
}

// Whether you're looking at the page: then Athena doesn't notify your devices of what it shows (push.py).
const looking = () => document.visibilityState === "visible" && document.hasFocus();
let wasLooking = null;

function lookingChanged() {
  const now_ = looking();
  if (now_ === wasLooking || !appShown()) return;
  wasLooking = now_;
  fetch("/api/presence", {
    method: "POST",
    credentials: "same-origin",
    keepalive: true, // it's sent even as the page goes away
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ looking: now_ }),
  }).catch(() => {});
  if (state.watching) state.watching.abort(); // so the next wait says so too
  watchStatus();
}

// Follow what Athena is doing: each request waits until something changes, or 25 seconds pass.
async function watchStatus() {
  const controller = new AbortController();
  state.watching = controller;
  let version = "";
  while (!controller.signal.aborted) {
    try {
      const wait = version ? "&wait=25" : "";
      const seen = looking() ? "&looking=1" : "";
      const data = await call("GET", `/api/status?after=${encodeURIComponent(version)}${wait}${seen}`, undefined, controller.signal);
      const was = state.status && state.status.status.state;
      setStatus(data);
      setOffline(false);
      if (version && was !== "idle" && data.status.state === "idle") {
        loadOverview().catch(() => {}); // new numbers
        if (location.hash === "#watching") loadWatching().catch(() => {}); // and what a check found
      }
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
  const changed = !state.status || state.status.version !== data.version;
  state.status = data;
  state.skew = data.now - Date.now() / 1000;
  renderStatus();
  if (changed) followAgents(data);
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
