// Athena's service worker: it shows the notifications Athena sends this device (push.py), and opens the dashboard
// where they point when you tap one. It keeps nothing: the page always comes from Athena as it is now.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let said = {};
  try {
    said = event.data ? event.data.json() : {};
  } catch {
    said = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(said.title || "Athena", {
      body: said.body || "",
      tag: said.tag || undefined,
      renotify: Boolean(said.tag),
      icon: "icon-180.png",
      data: { url: said.url || "#chat" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const hash = new URL(event.notification.data.url || "#chat", self.location.origin).hash || "#chat";
  event.waitUntil(
    (async () => {
      const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of open) {
        if (new URL(client.url).origin !== self.location.origin) continue;
        await client.focus();
        client.postMessage({ open: hash }); // the page goes there itself
        return;
      }
      await self.clients.openWindow(`/${hash}`);
    })()
  );
});
