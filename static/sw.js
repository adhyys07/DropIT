/* DropIT service worker — caches the app shell only.
   Signalling (/socket.io/) and object URLs are never cached. */

const VERSION = 'dropit-v1';
const SHELL = [
  '/',
  '/index.html',
  '/sender.html',
  '/reciever.html',
  '/css/dropit.css',
  '/js/protocol.js',
  '/js/send-engine.js',
  '/js/recv-engine.js',
  '/js/preview.js',
  '/js/history.js',
  '/js/extras.js',
  '/js/reconnect.js',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/favicon-64.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(VERSION)
      // addAll fails the whole install if any single entry 404s; cache
      // individually so a missing icon cannot break the install.
      .then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith('/socket.io/')) return;   // never cache signalling
  if (url.pathname === '/healthz') return;

  // Navigations: network first, so a deploy is picked up immediately.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match(req).then((hit) => hit || caches.match('/index.html')))
    );
    return;
  }

  event.respondWith(
    caches.match(req).then((hit) =>
      hit || fetch(req).then((res) => {
        if (res && res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
    )
  );
});
