const CACHE = "training-coach-v4";
const SHELL = ["/", "/index.html", "/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // Drop any older caches so a new deploy fully takes over.
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("push", (event) => {
  let data = { title: "Training Coach", body: "You have a new update." };
  try { data = event.data.json(); } catch (e) {}
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "icon-192.png",
      badge: "icon-192.png",
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow("/"));
});

// API: network-first (data must be live), with the read-only plan/fitness/sleep
// GETs cached so the last-good version survives offline.
//
// App shell: stale-while-revalidate. The same free-tier backend now serves this
// page, and a cold instance can take ~30s to answer — network-first there would
// mean staring at a blank screen. So: serve the cached shell instantly, refresh
// the cache in the background, and the new version lands on the next open.
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  if (url.pathname.startsWith("/api/")) {
    const cacheable =
      event.request.method === "GET" &&
      /\/api\/(plan\/week|fitness|sleep)(\?|$)/.test(url.pathname + url.search);
    event.respondWith(
      fetch(event.request)
        .then((res) => {
          if (cacheable && res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((cache) => cache.put(event.request, copy));
          }
          return res;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  if (event.request.method !== "GET" || url.origin !== self.location.origin) {
    return; // let the browser handle it normally
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const fresh = fetch(event.request)
        .then((res) => {
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(CACHE).then((cache) => cache.put(event.request, copy));
          }
          return res;
        })
        .catch(() => cached);          // offline / backend asleep -> keep the cached copy
      return cached || fresh;          // instant if cached, else wait for network
    })
  );
});
