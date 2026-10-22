// Notifications on this device: when Athena answers a message you sent on the dashboard, a task has news,
// something stops working, or it needs your OK. They come through the browser's push service (push.py), which
// on an iPhone works for the dashboard added to your Home Screen.

import { call, h, icon, toast } from "./core.js";

const push_ = { registration: null, subscription: null, key: null };

const supported = () => window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const apple = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const installed = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

export async function setUpNotifications() {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.addEventListener("message", (event) => {
    if (event.data && typeof event.data.open === "string" && event.data.open.startsWith("#")) location.hash = event.data.open;
  });
  try {
    push_.registration = await navigator.serviceWorker.register("sw.js");
    if (!("PushManager" in window)) return;
    push_.subscription = await push_.registration.pushManager.getSubscription();
    if (push_.subscription) {
      push_.key = (await call("GET", "/api/push")).key;
      if (!sameKey(push_.subscription, push_.key)) {
        await push_.subscription.unsubscribe(); // from before Athena's key changed: it can't reach this device
        push_.subscription = null;
      } else {
        // Athena may have forgotten it, as when it's moved to another Pi: it's told again.
        await call("POST", "/api/push", { subscription: push_.subscription.toJSON() });
      }
    }
  } catch {
    // Notifications are off here, or Athena isn't set up for them: the bell says so when you press it.
  }
  showBells();
}

// A button that says whether this device gets notifications, and opens what turns them on or off.
export function bell() {
  return paint(h("button.button.small.bell", { type: "button", onclick: settings, "aria-label": "Notifications" }, icon("bell")));
}

// Words that say whether they're on, as the bells do.
export function bellLabel() {
  return paint(h("span.is", { "data-bell-label": "" }));
}

function showBells() {
  for (const el of document.querySelectorAll(".bell, [data-bell-label]")) paint(el);
}

function paint(el) {
  const on = Boolean(push_.subscription);
  if (el.matches(".bell")) {
    el.dataset.on = String(on);
    el.title = on ? "Notifications are on for this device" : "Notifications";
  } else {
    el.textContent = on ? "On for this device" : "Off for this device";
  }
  return el;
}

async function settings() {
  const dialog = document.getElementById("dialog");
  const draw = (...body) => {
    dialog.replaceChildren(h("h2", { text: "Notifications" }), ...body);
    if (!dialog.open) dialog.showModal();
  };
  const close = h("button.button", { type: "button", text: "Close", onclick: () => dialog.close() });
  const what = "Athena answers a message you sent here, a task has news, something stops working, or it needs your OK";
  if (!window.isSecureContext) {
    return draw(h("p", { text: "Notifications need the dashboard over HTTPS, as Tailscale Serve gives it: open it at your Pi's https:// address (see docs/dashboard.md)." }), h("div.tool-actions", {}, close));
  }
  if (apple() && !installed() && !supported()) {
    return draw(
      h("p", { text: `On an iPhone or iPad, notifications come to Athena added to your Home Screen. In Safari, tap Share, then Add to Home Screen. Open Athena from there, and turn them on here, to hear when ${what}.` }),
      h("div.tool-actions", {}, close)
    );
  }
  if (!supported()) return draw(h("p", { text: "This browser can't take notifications from web pages." }), h("div.tool-actions", {}, close));
  if (Notification.permission === "denied") {
    return draw(h("p", { text: "Notifications from Athena are blocked on this device. Allow them in its settings, then come back here." }), h("div.tool-actions", {}, close));
  }
  if (push_.subscription) {
    return draw(
      h("p", { text: `They're on for this device: you'll hear when ${what}. Not while you're looking at the dashboard, which shows it anyway.` }),
      h(
        "div.tool-actions",
        {},
        h("button.button.danger", { type: "button", text: "Turn off", onclick: turnOff }),
        h("button.button", { type: "button", text: "Send a test", onclick: test }),
        close
      )
    );
  }
  draw(
    h("p", { text: `Hear when ${what}.` }),
    h("p.hint", { text: "Each shows the start of the message, encrypted so that only this device can read it on its way through the push service. [dashboard] notification_text = false in config.toml shows only that there's something new." }),
    h("div.tool-actions", {}, close, h("button.button.primary", { type: "button", text: "Turn on", onclick: turnOn }))
  );
}

async function turnOn(event) {
  event.target.disabled = true;
  try {
    // Asked for straight away, while it's your tap: Safari only asks then.
    if ((await Notification.requestPermission()) !== "granted") throw new Error("Notifications weren't allowed.");
    push_.key = push_.key || (await call("GET", "/api/push")).key;
    const registration = push_.registration || (await navigator.serviceWorker.ready);
    push_.subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: bytes(push_.key) });
    await call("POST", "/api/push", { subscription: push_.subscription.toJSON() });
    toast("Notifications are on for this device.");
  } catch (e) {
    toast(e.message || "Notifications couldn't be turned on.", true);
  }
  showBells();
  settings();
}

async function turnOff(event) {
  event.target.disabled = true;
  const subscription = push_.subscription;
  try {
    await call("POST", "/api/push/off", { endpoint: subscription.endpoint });
    await subscription.unsubscribe();
    push_.subscription = null;
    toast("Notifications are off for this device.");
  } catch (e) {
    toast(e.message, true);
  }
  showBells();
  settings();
}

async function test(event) {
  event.target.disabled = true;
  try {
    await call("POST", "/api/push/test", { endpoint: push_.subscription.endpoint });
    toast("Sent. It should arrive in a moment.");
  } catch (e) {
    toast(e.message, true);
  }
  event.target.disabled = false;
}

// A key as the push manager wants it, from base64url.
function bytes(key) {
  const raw = atob(key.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (key.length % 4)) % 4));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function sameKey(subscription, key) {
  const had = subscription.options && subscription.options.applicationServerKey;
  if (!had) return true; // the browser doesn't say: assume it's Athena's
  const want = bytes(key);
  const got = new Uint8Array(had);
  return got.length === want.length && got.every((b, i) => b === want[i]);
}
