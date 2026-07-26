// Kill-switch service worker.
// Clears all previously-cached assets, takes immediate control of all clients,
// then passes every request straight to the network (no caching).
// Fixes white-screen on pinned PWA caused by a stale cache from an older deploy.

self.addEventListener('install', function(evt) {
  self.skipWaiting();
});

self.addEventListener('activate', function(evt) {
  evt.waitUntil(
    caches.keys()
      .then(function(names) {
        return Promise.all(names.map(function(n) { return caches.delete(n); }));
      })
      .then(function() { return self.clients.claim(); })
  );
});

// Network-only — no caching
self.addEventListener('fetch', function(evt) {
  evt.respondWith(fetch(evt.request));
});
