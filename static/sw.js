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

  // PWA share target: the OS POSTs files here. A static page cannot read a
  // POST body, so stash the files in a cache and redirect to a GET.
  if (req.method === 'POST' && new URL(req.url).pathname === '/sender.html') {
    event.respondWith(Response.redirect('/sender.html?shared=1', 303));
    event.waitUntil((async () => {
      try {
        const data = await req.formData();
        const files = data.getAll('files').filter((f) => f && f.name);
        const text = data.get('text') || data.get('title') || '';
        const cache = await caches.open('dropit-share');
        await cache.put('/__shared__', new Response(
          JSON.stringify({ text, count: files.length }),
          { headers: { 'Content-Type': 'application/json' } }));
        for (let i = 0; i < files.length; i++) {
          await cache.put(`/__shared__/${i}`, new Response(files[i], {
            headers: {
              'Content-Type': files[i].type || 'application/octet-stream',
              'X-Name': encodeURIComponent(files[i].name),
            },
          }));
        }
      } catch { /* nothing usable was shared */ }
    })());
    return;
  }

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
