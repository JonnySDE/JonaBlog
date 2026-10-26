// Cliente de la API del CMS (API 1). Se ejecuta SOLO durante el build.
const BASE = import.meta.env.CMS_API_URL;
const TOKEN = import.meta.env.CMS_API_TOKEN;

async function get(path: string) {
  if (!BASE || !TOKEN) throw new Error('Faltan CMS_API_URL o CMS_API_TOKEN');
  const res = await fetch(`${BASE}${path}`, {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });
  if (!res.ok) throw new Error(`CMS API ${path} respondió ${res.status}`);
  return res.json();
}

// Recorre todas las páginas de una lista paginada.
async function getAll(path: string) {
  const sep = path.includes('?') ? '&' : '?';
  const items: any[] = [];
  for (let page = 1; ; page++) {
    const data = await get(`${path}${sep}page=${page}&per_page=100`);
    items.push(...data.items);
    if (page >= data.total_pages || data.items.length === 0) break;
  }
  return items;
}

export async function getPosts() {
  const posts = await getAll('/posts?view=full');
  // Invariante 2: dos contenidos con la misma ruta hacen fallar el build.
  const seen = new Set<string>();
  for (const p of posts) {
    if (seen.has(p.path)) throw new Error(`Ruta duplicada: ${p.path}`);
    seen.add(p.path);
  }
  return posts;
}
export const getPages = () => getAll('/pages');
export const getCategories = async () => (await get('/categories')).items;
export const getTags = async () => (await get('/tags')).items;

export function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString('es-MX', {
    year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC',
  });
}
