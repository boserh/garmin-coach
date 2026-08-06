/* UI-03 · the service worker.
 *
 * The manifest has been here since EP-04 (standalone, start_url /dashboard, theme
 * colours) but there was no service worker at all, so:
 *   - Chrome never offered a real install prompt — "add to home screen" produced a
 *     bookmark, not an app;
 *   - offline meant a white page. The server lives on a Pi on the home LAN, so leaving
 *     the house (or a reboot) took away even the things that cannot change: today's
 *     plan, the last report, yesterday's metrics;
 *   - every navigation was a full round-trip over Wi-Fi for CSS/JS that never change.
 *
 * Three rules, and a hard deny-list that overrides them:
 *
 *   cache-first             /static/* — immutable per ?v=, so a hit is always correct
 *   stale-while-revalidate  GET /dashboard, GET /plan — instant paint, fresh in the
 *                           background
 *   network-only            EVERYTHING else
 *
 * The deny-list is the important half: personal pages must never persist on the device.
 * /login, /register, /settings, /admin/*, /me/export and any non-GET are never cached,
 * are actively evicted if they somehow got in, and a POST /logout wipes every cache.
 *
 * Kept deliberately tiny and dependency-free: a broken service worker is sticky, and
 * the version comes from the same ?v= digest as the rest of the assets, so a deploy
 * replaces it rather than leaving a wedged copy behind.
 */
'use strict';

var VERSION = new URL(self.location).searchParams.get('v') || 'dev';
var STATIC_CACHE = 'static-' + VERSION;
var PAGE_CACHE = 'pages-' + VERSION;
var OFFLINE_URL = '/offline';

// Enough to paint any page without the network. The versioned query strings match what
// the documents ask for, so these are hits rather than near-misses.
var PRECACHE = [
  OFFLINE_URL,
  '/static/app.css?v=' + VERSION,
  '/static/app.js?v=' + VERSION,
  '/static/icon.svg',
  '/static/fonts/inter-latin.woff2',
  '/static/fonts/inter-latin-ext.woff2',
  '/static/fonts/inter-cyrillic.woff2',
  '/static/fonts/inter-cyrillic-ext.woff2'
];

// Pages worth having offline: what you'd open standing outside the front door.
var SWR_PATHS = ['/dashboard', '/plan'];

// Never, under any circumstance, stored on the device.
var NEVER_CACHE = [
  '/login', '/register', '/logout', '/settings', '/admin', '/me/export', '/status'
];

function isNeverCache(url) {
  return NEVER_CACHE.some(function (p) {
    return url.pathname === p || url.pathname.indexOf(p + '/') === 0;
  });
}

function isStatic(url) {
  return url.pathname.indexOf('/static/') === 0;
}

function isSwrPage(url) {
  return SWR_PATHS.indexOf(url.pathname) !== -1;
}

// Everything, not just the pages: on a shared phone the previous user's dashboard must
// not be one offline visit away. The shell (CSS/fonts/icons) re-warms by itself on the
// next load — that costs one round-trip and is not anyone's data.
async function purgeAll() {
  var names = await caches.keys();
  await Promise.all(names.map(function (n) { return caches.delete(n); }));
}

self.addEventListener('install', function (e) {
  // A single failed precache entry must not abort the install and leave the app with no
  // worker at all, so each is added individually and best-effort.
  e.waitUntil((async function () {
    var cache = await caches.open(STATIC_CACHE);
    await Promise.all(PRECACHE.map(function (u) {
      return cache.add(u).catch(function () { /* offline install, or asset renamed */ });
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', function (e) {
  e.waitUntil((async function () {
    var names = await caches.keys();
    await Promise.all(names.map(function (n) {
      // Anything from an older ?v= is dead weight — this is what makes a changed
      // app.css land without "clear site data".
      if (n !== STATIC_CACHE && n !== PAGE_CACHE) return caches.delete(n);
      return null;
    }));
    await self.clients.claim();
  })());
});

self.addEventListener('message', function (e) {
  if (e.data && e.data.type === 'purge') {
    e.waitUntil(purgeAll());
  }
});

// The marker _base.html always emits; replaced with a real banner only when the page is
// served from the cache, so a stale readiness number can never pass for a fresh one.
var SLOT = '<!--sw-offline-slot-->';

function bannerHtml(when) {
  return '<div class="banner banner--warn" role="alert">' +
         '<span class="bicon" aria-hidden="true">📴</span>' +
         '<div class="btext">Немає звʼязку із сервером — показано збережену копію, ' +
         'дані станом на <b>' + when + '</b>.</div></div>';
}

function stamp(response) {
  var d = new Date(response.headers.get('sw-cached-at') || Date.now());
  var pad = function (n) { return (n < 10 ? '0' : '') + n; };
  return pad(d.getHours()) + ':' + pad(d.getMinutes());
}

async function withOfflineBanner(response) {
  var html = await response.text();
  if (html.indexOf(SLOT) === -1) return new Response(html, {
    status: response.status, headers: {'Content-Type': 'text/html; charset=utf-8'}
  });
  return new Response(html.replace(SLOT, bannerHtml(stamp(response))), {
    status: response.status,
    headers: {'Content-Type': 'text/html; charset=utf-8'}
  });
}

async function storePage(cache, request, response) {
  // Stamp the copy so the banner can say WHEN, not just "stale".
  var body = await response.clone().blob();
  var headers = new Headers(response.headers);
  headers.set('sw-cached-at', new Date().toISOString());
  await cache.put(request, new Response(body, {
    status: response.status, statusText: response.statusText, headers: headers
  }));
}

async function staleWhileRevalidate(request) {
  var cache = await caches.open(PAGE_CACHE);
  var cached = await cache.match(request);
  var network = fetch(request).then(async function (response) {
    if (response && response.ok) await storePage(cache, request, response);
    return response;
  }).catch(function () { return null; });

  if (cached) {
    // Refresh in the background; the copy on screen is honest about its age.
    network.then(function (fresh) {
      if (!fresh) return;
      self.clients.matchAll({type: 'window'}).then(function (cs) {
        cs.forEach(function (c) { c.postMessage({type: 'fresh', url: request.url}); });
      });
    });
    return withOfflineBanner(cached);
  }
  var fresh = await network;
  if (fresh) return fresh;
  return (await caches.match(OFFLINE_URL)) ||
         new Response('Офлайн', {status: 503, headers: {'Content-Type': 'text/plain'}});
}

async function cacheFirst(request) {
  var cached = await caches.match(request);
  if (cached) return cached;
  var response = await fetch(request);
  if (response && response.ok) {
    var cache = await caches.open(STATIC_CACHE);
    await cache.put(request, response.clone());
  }
  return response;
}

async function networkOnly(request) {
  try {
    return await fetch(request);
  } catch (err) {
    if (request.mode === 'navigate') {
      var offline = await caches.match(OFFLINE_URL);
      if (offline) return offline;
    }
    throw err;
  }
}

self.addEventListener('fetch', function (e) {
  var request = e.request;
  var url = new URL(request.url);

  if (url.origin !== self.location.origin) return;      // never touch a third party

  if (request.method !== 'GET') {
    // Signing out must not leave personal pages on the device — belt and braces next to
    // the message the page sends, because a navigation can outrun a postMessage.
    if (url.pathname === '/logout') e.waitUntil(purgeAll());
    return;                                             // POSTs go straight to the net
  }

  if (isNeverCache(url)) {
    // Also evict, in case an earlier version of this worker ever stored one.
    e.waitUntil(caches.open(PAGE_CACHE).then(function (c) { return c.delete(request); }));
    e.respondWith(networkOnly(request));
    return;
  }
  if (isStatic(url)) { e.respondWith(cacheFirst(request)); return; }
  if (isSwrPage(url) && request.mode === 'navigate') {
    e.respondWith(staleWhileRevalidate(request));
    return;
  }
  e.respondWith(networkOnly(request));
});
