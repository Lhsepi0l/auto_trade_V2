self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_error) {
    payload = {
      title: "운영 알림",
      body: event.data ? event.data.text() : "",
    };
  }
  const title = payload.title || "운영 알림";
  const options = {
    body: payload.body || "",
    icon: payload.icon || "/operator/static/operator-icon.svg",
    badge: payload.badge || "/operator/static/operator-icon.svg",
    tag: payload.tag || "operator-update",
    data: {
      path: payload.path || "/operator/",
      eventType: payload.event_type || null,
    },
    renotify: false,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const rawPath = event.notification?.data?.path || "/operator/";
  const targetUrl = new URL(rawPath, self.location.origin).toString();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientsList) => {
      for (const client of clientsList) {
        if (client.url === targetUrl && "focus" in client) {
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
      return undefined;
    })
  );
});
