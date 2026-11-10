// Destiny Pay service worker: makes the app installable and opens its shell without a connection.
// Network first, always: money data (/api) is never cached, and a new deploy is picked up on the next load.
const CACHE = "dp-shell-v1";
const SHELL = ["/", "/static/app.css", "/static/app.js", "/static/i18n.js", "/static/favicon.svg", "/static/icon-192.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  const shell = e.request.mode === "navigate" || SHELL.includes(url.pathname);
  if (!shell) return;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request.mode === "navigate" ? "/" : e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request.mode === "navigate" ? "/" : e.request)));
});
