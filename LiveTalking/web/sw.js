/* LiveTalking Service Worker v5
   Provide offline fallback and caching for core assets.
   注意：common.css / common.js / auth.js 是 cache-first（见下方 fetch 处理），
   改动设计系统时必须同步提升 CACHE 版本，否则老用户会一直拿到旧样式：
   即便源文件已更新，第一次打开仍会命中旧缓存，要再刷新一次才生效。
   v4：修正导航分组下拉被 .lt-nav-links 滚动容器裁剪（“内容配置/运营管理 打不开”）。
   v5：新增登录 / 会员体系（login.html、upgrade.html、admin_users.html、auth.js）。 */

const CACHE = 'livetalking-v9';
const PRECACHE = [
  '/common.css',
  '/common.js',
  '/auth.js',
  '/manifest.json',
  '/login.html',
  '/upgrade.html',
  '/forbidden.html',
  '/admin_users.html',
  '/index.html',
  '/live_console.html',
  '/live_output.html',
  '/dashboard.html',
  '/accounts.html',
  '/console.html',
  '/admin.html',
  'https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css',
  'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.0/font/bootstrap-icons.css',
];

// Install: precache core assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => {
      return Promise.allSettled(PRECACHE.map((url) => cache.add(url).catch(() => {})));
    }).then(() => self.skipWaiting())
  );
});

// Activate: clean old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    }).then(() => self.clients.claim())
  );
});

// Fetch: stale-while-revalidate for navigation, cache-first for static
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // API requests: network only
  if (url.pathname.startsWith('/api/') || url.pathname === '/offer' ||
      url.pathname === '/human' || url.pathname === '/humanaudio' ||
      url.pathname === '/record' || url.pathname === '/interrupt_talk' ||
      url.pathname === '/is_speaking' || url.pathname === '/set_audiotype') {
    return;
  }

  // Page navigations: network first, fallback to cache
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request))
    );
    return;
  }

  // Static assets: cache first with network update
  if (event.request.destination === 'style' || event.request.destination === 'script' ||
      event.request.destination === 'image' || event.request.destination === 'font') {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        const fetched = fetch(event.request).then((response) => {
          if (response.ok) {
            caches.open(CACHE).then((cache) => cache.put(event.request, response.clone()));
          }
          return response;
        }).catch(() => cached);
        return cached || fetched;
      })
    );
    return;
  }
});
