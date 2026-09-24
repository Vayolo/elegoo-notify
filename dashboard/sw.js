const CACHE='elegoo-notify-v1';
const CORE=['/','/assets/app.css','/assets/app.js','/assets/inter-var.woff2',
  '/assets/icon-192.png','/vendor/three.min.js','/vendor/STLLoader.js','/vendor/OrbitControls.js'];
self.addEventListener('install',e=>{
  e.waitUntil(caches.open(CACHE).then(c=>c.addAll(CORE)).then(()=>self.skipWaiting()));
});
self.addEventListener('activate',e=>{
  e.waitUntil(caches.keys().then(ks=>
    Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))
  ).then(()=>self.clients.claim()));
});
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET')return;
  const url=new URL(e.request.url);
  // mai cache per API/stream (sempre fresche)
  if(url.pathname.startsWith('/api/')||url.pathname.startsWith('/video')||
     url.pathname.startsWith('/photo')||url.pathname.startsWith('/status')||
     url.pathname.startsWith('/snapshots')||url.pathname.startsWith('/health'))return;
  e.respondWith(
    fetch(e.request).then(r=>{
      if(r.ok&&CORE.includes(url.pathname)){
        const cp=r.clone();caches.open(CACHE).then(c=>c.put(e.request,cp));
      }
      return r;
    }).catch(()=>caches.match(e.request))
  );
});
