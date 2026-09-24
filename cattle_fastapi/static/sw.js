// =========================================================================
// SERVICE WORKER — offline support for Cattle Claim Validator.
// Adapted from the original app's sw.js for the FastAPI + single-HTML-file
// setup (no Vite build output, so the './assets/main.js' precache entry
// from the original doesn't apply here — everything is inline in
// index.html itself, which IS precached below).
//
// HOW VERSIONING WORKS: bump CACHE_VERSION any time index.html changes
// meaningfully, or a reference photo/model changes upstream. Forgetting to
// bump it means returning users keep silently using a stale cached copy.
// =========================================================================

const CACHE_VERSION = 'v6';
const CACHE_NAME = `cattle-claim-fastapi-${CACHE_VERSION}`;

// ---- App shell: the local files this app is built from ----
const SHELL_ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-512-maskable.png',
];

// ---- CDN library entry points ----
const CDN_ASSETS = [
  'https://cdn.jsdelivr.net/npm/onnxruntime-web/dist/ort.min.js',
];

// ---- All trained ONNX models ----
const MODEL_ASSETS = [
  'https://raw.githubusercontent.com/kshitij435/cattle/main/yolov8n.onnx',
  './models/left_right_flank_dead_int8.onnx',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/head_left_right.onnx',
  './models/left_right_live_int8.onnx',
  './models/fronthead_int8.onnx',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/live_cattle_front_head.onnx',
  
];

// ---- All reference/example photos ----
const REFERENCE_PHOTO_ASSETS = [
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_flank_left.jpg',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_flank_right.jpg',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_head_left.jpg',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_head_right.jpg',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_muzzle.png',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_ear_tag.png',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_scar_injury.png',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_flank_live_left.jpg',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_flank_live_right.jpg',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_front_view_live.jpg',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_rear_view_live.jpg',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_muzzle_live.png',
  'https://raw.githubusercontent.com/kshitij435/cattle/main/ref_owner_photo_live.jpg',
];

const PRECACHE_ASSETS = [
  ...SHELL_ASSETS,
  ...CDN_ASSETS,
  ...MODEL_ASSETS,
  ...REFERENCE_PHOTO_ASSETS,
];

// Origins safe/useful to cache-as-we-go at runtime -- onnxruntime-web
// dynamically fetches .wasm binaries from jsdelivr after ort.min.js runs,
// so those can't be listed by exact name above.
const RUNTIME_CACHE_ORIGINS = [
  'cdn.jsdelivr.net',
  'raw.githubusercontent.com',
];

// =========================================================================
// INSTALL — precache everything. allSettled so one flaky/missing asset
// doesn't abort the whole install.
// =========================================================================
self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    const results = await Promise.allSettled(
      PRECACHE_ASSETS.map(async (url) => {
        const req = new Request(url, { cache: 'reload', mode: 'cors' });
        const resp = await fetch(req);
        if (!resp.ok && resp.type !== 'opaque') {
          throw new Error(`Bad response ${resp.status} for ${url}`);
        }
        await cache.put(url, resp);
      })
    );
    const failed = results
      .map((r, i) => (r.status === 'rejected' ? PRECACHE_ASSETS[i] : null))
      .filter(Boolean);
    if (failed.length) {
      console.warn('[sw] Some assets failed to precache (will retry at runtime):', failed);
    }
    self.skipWaiting();
  })());
});

// =========================================================================
// ACTIVATE — delete any cache from a previous CACHE_VERSION.
// =========================================================================
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(
      names
        .filter((n) => n.startsWith('cattle-claim-fastapi-') && n !== CACHE_NAME)
        .map((n) => caches.delete(n))
    );
    await self.clients.claim();
  })());
});

// =========================================================================
// FETCH
//   - Navigations (the HTML page itself): network-first, falling back to
//     the cached shell when offline.
//   - Same-origin + runtime-cache origins: cache-first.
//   - The backend API (/api/*) is deliberately left alone below -- capture
//     uploads and case data are inherently online-only; offline captures
//     still work locally (your original app's design), they just won't
//     reach the FastAPI backend until connectivity returns. That matches
//     Matrix scenario #3/#11 Live's offline-queue behavior.
//   - Everything else (nominatim reverse geocoding, maps links, etc.):
//     network-only, no caching.
//
// STRATEGY: network-first, everywhere, no exceptions. Per explicit
// request -- the service worker should stay completely uninvolved while
// online (every request always goes to the real network first, gets the
// freshest possible copy, and updates the cache in the background) and
// only step in as a fallback the moment a fetch actually fails. The
// trade-off, stated plainly: this means large files (the ~7 ONNX models,
// several MB each) get re-requested over the network on every load even
// though a perfectly good cached copy already exists -- slower and more
// data-hungry than a cache-first approach would be on a slow connection,
// by design, in exchange for the service worker never overriding what's
// actually on the network when one exists.
// =========================================================================
self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  if (req.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req, { cache: 'no-store' });
        const cache = await caches.open(CACHE_NAME);
        cache.put('./index.html', fresh.clone());
        return fresh;
      } catch (err) {
        const cache = await caches.open(CACHE_NAME);
        return (await cache.match('./index.html')) || (await cache.match('./'));
      }
    })());
    return;
  }

  // Never cache API calls -- these need a live server, and stale cached
  // JSON here would be actively misleading (e.g. a cached upload response).
  if (url.pathname.startsWith('/api/')) return;

  const isSameOrigin = url.origin === self.location.origin;
  const isRuntimeOrigin = RUNTIME_CACHE_ORIGINS.includes(url.hostname);
  if (isSameOrigin || isRuntimeOrigin) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE_NAME);
      try {
        // Always try the real network FIRST -- the service worker does
        // nothing different from a normal fetch as long as the network
        // actually works.
        const fresh = await fetch(req);
        if (fresh.ok || fresh.type === 'opaque') {
          cache.put(req, fresh.clone());
        }
        return fresh;
      } catch (err) {
        // Network genuinely failed (offline, or unreachable) -- THIS is
        // the only moment the service worker actually does anything: it
        // steps in with whatever was cached from a previous successful
        // visit.
        const cached = await cache.match(req, { ignoreVary: true });
        if (cached) return cached;
        return new Response('', { status: 504, statusText: 'Offline and not cached' });
      }
    })());
    return;
  }
});
