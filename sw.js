// Caches only the app itself, so it opens quickly. Episodes and audio always come fresh from the site.
const CACHE = "twicedaily-gh-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./catalog.json", "./icon-192.png", "./icon-512.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {})); self.skipWaiting(); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))); self.clients.claim(); });
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (/\/(episodes|voices)\//.test(url.pathname) || e.request.headers.has("range")) return;
  e.respondWith(
    fetch(e.request).then(res => { if (res.ok){ const copy = res.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); } return res; })
      .catch(() => caches.match(e.request).then(r => r || caches.match("./index.html")))
  );
});
