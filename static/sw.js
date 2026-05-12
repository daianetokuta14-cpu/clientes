const CACHE = 'megacredito-v1';

// Recursos que queremos cachear para funcionar offline
const STATIC_ASSETS = [
  '/acesso',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

// Instala e faz cache dos recursos estáticos
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => {
      return cache.addAll(STATIC_ASSETS).catch(() => {});
    })
  );
  self.skipWaiting();
});

// Remove caches antigos
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Estratégia: Network first, fallback para cache
// Para rotas de API e autenticação, sempre usa network
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Ignora requests que não são GET ou são de outras origens
  if (event.request.method !== 'GET') return;
  if (url.origin !== location.origin) return;

  // Rotas que nunca devem ser cacheadas
  const noCacheRoutes = ['/api/', '/logout', '/pagar', '/estornar', '/desfazer', '/renovar', '/admin'];
  if (noCacheRoutes.some(r => url.pathname.startsWith(r))) return;

  event.respondWith(
    fetch(event.request)
      .then(response => {
        // Cache de recursos estáticos (icons, manifest)
        if (url.pathname.startsWith('/static/')) {
          const clone = response.clone();
          caches.open(CACHE).then(cache => cache.put(event.request, clone));
        }
        return response;
      })
      .catch(() => {
        // Offline: tenta o cache
        return caches.match(event.request).then(cached => {
          if (cached) return cached;
          // Se não tem cache, retorna página de offline simples
          if (event.request.headers.get('accept')?.includes('text/html')) {
            return new Response(
              `<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8">
              <meta name="viewport" content="width=device-width,initial-scale=1">
              <title>Sem conexão</title>
              <style>
                body{background:#0a0a0f;color:#e8e8f0;font-family:sans-serif;
                  display:flex;align-items:center;justify-content:center;
                  min-height:100vh;text-align:center;padding:2rem}
                h1{color:#7fff6e;font-size:1.5rem;margin-bottom:1rem}
                p{color:#6b6b80;font-size:0.9rem;line-height:1.6}
                button{margin-top:1.5rem;background:#7fff6e;color:#0a0a0f;
                  border:none;padding:0.75rem 1.5rem;border-radius:8px;
                  font-size:1rem;cursor:pointer;font-weight:700}
              </style></head>
              <body>
                <div>
                  <h1>📡 Sem conexão</h1>
                  <p>Você está offline.<br>Verifique sua conexão e tente novamente.</p>
                  <button onclick="location.reload()">Tentar novamente</button>
                </div>
              </body></html>`,
              { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
            );
          }
        });
      })
  );
});
