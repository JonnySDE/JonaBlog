"""
API 1 - API DEL CMS         (ruta base: /cms/v1)
=============================================================================
¿Qué hace?
  Es la ÚNICA pieza que habla con WordPress. Lee el contenido publicado,
  lo limpia y lo entrega en un formato consistente (JSON) para que Astro
  construya el sitio. También calcula la "versión" del contenido, que usa
  la API 2 para saber si hay algo nuevo que publicar.

¿Qué NO hace?
  No escribe en WordPress, no entrega borradores y no construye el sitio.

Quién la usa: el build de Astro (token CMS_API_TOKEN) y la API 2.

Endpoints:
  C1  GET /cms/v1/posts                listar artículos
  C2  GET /cms/v1/posts/{slug}         un artículo completo
  C3  GET /cms/v1/pages                listar páginas
  C4  GET /cms/v1/pages/{slug}         una página
  C5  GET /cms/v1/categories           categorías con artículos
  C6  GET /cms/v1/tags                 etiquetas con artículos
  C7  GET /cms/v1/content-version      huella (SHA-256) del contenido
  H1  GET /cms/v1/health               salud (sin token)
"""
import hashlib
from datetime import datetime, timezone
from html import unescape
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query

from common import (
    SLUG_RE, ApiError, clean_html, iso, reading_minutes, require_token,
    strip_tags, wp_get, wp_get_all,
)

router = APIRouter()

# Token de solo lectura. Se acepta también el de administrador para pruebas.
AUTH = Depends(require_token("CMS_API_TOKEN", "ADMIN_API_TOKEN"))


# --------------------------------------------------------------------------
# Normalización: convierte el JSON "crudo" de WordPress al formato del Paso 4
# --------------------------------------------------------------------------
def _utc(wp_gmt_value: str) -> str:
    """WordPress entrega '2026-10-10T10:00:00' en *_gmt; le agregamos la Z."""
    return wp_gmt_value.rstrip("Z") + "Z"


def _terms_from_embed(post: dict):
    """Saca categorías y etiquetas del bloque _embedded (por _embed=1)."""
    categories, tags = [], []
    for group in (post.get("_embedded", {}).get("wp:term") or []):
        for t in group:
            term = {
                "id": t["id"],
                "name": unescape(t["name"]),
                "slug": t["slug"],
                "parent_id": (t.get("parent") or None)
                if t.get("taxonomy") == "category" else None,
                "post_count": max(1, t.get("count", 1)),
            }
            if t.get("taxonomy") == "category":
                categories.append(term)
            elif t.get("taxonomy") == "post_tag":
                tags.append(term)
    return categories, tags


def _featured_image(post: dict):
    media = (post.get("_embedded", {}).get("wp:featuredmedia") or [None])[0]
    if not media or "source_url" not in media:
        return None
    details = media.get("media_details") or {}
    return {
        "url": media["source_url"],
        "alt_text": media.get("alt_text", ""),
        "width": details.get("width"),
        "height": details.get("height"),
    }


def normalize_post(p: dict):
    """Devuelve un ContentPost, o None si incumple la Invariante 3."""
    title = strip_tags(p.get("title", {}).get("rendered", ""))
    slug = p.get("slug")
    date_gmt = p.get("date_gmt") or p.get("date")
    if not (title and slug and date_gmt):
        print(f"ADVERTENCIA: post {p.get('id')} omitido (sin título, slug o fecha)")
        return None
    content = clean_html(p.get("content", {}).get("rendered", ""))
    author = (p.get("_embedded", {}).get("author") or [{}])[0]
    categories, tags = _terms_from_embed(p)
    return {
        "id": p["id"],
        "slug": slug,
        "path": f"/blog/{slug}/",
        "title": title,
        "excerpt": strip_tags(p.get("excerpt", {}).get("rendered", ""))[:300] or None,
        "content_html": content,
        "published_at": _utc(date_gmt),
        "modified_at": _utc(p.get("modified_gmt") or p.get("modified") or date_gmt),
        "author": {
            "id": author.get("id", 0),
            "name": author.get("name", ""),
            "slug": author.get("slug", ""),
        },
        "categories": categories,
        "tags": tags,
        "featured_image": _featured_image(p),
        "reading_minutes": reading_minutes(content),
    }


def normalize_page(p: dict, slug_by_id: dict):
    title = strip_tags(p.get("title", {}).get("rendered", ""))
    if not (title and p.get("slug")):
        return None
    return {
        "id": p["id"],
        "slug": p["slug"],
        "path": f"/{p['slug']}/",
        "title": title,
        "content_html": clean_html(p.get("content", {}).get("rendered", "")),
        "modified_at": _utc(p.get("modified_gmt") or p.get("modified")),
        "parent_slug": slug_by_id.get(p.get("parent")) if p.get("parent") else None,
        "menu_order": p.get("menu_order"),
    }


def normalize_term(t: dict, is_category: bool):
    return {
        "id": t["id"],
        "name": unescape(t["name"]),
        "slug": t["slug"],
        "parent_id": (t.get("parent") or None) if is_category else None,
        "post_count": t.get("count", 0),
    }


def _term_id(kind: str, slug: str) -> int:
    """Convierte el slug de una categoría/etiqueta en su ID de WordPress."""
    data, _ = wp_get(f"/{kind}", {"slug": slug})
    if not data:
        raise ApiError(404, "NOT_FOUND", f"No existe '{slug}' en {kind}")
    return data[0]["id"]


def _paginated(items, headers, page, per_page):
    return {
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": int(headers.get("X-WP-Total", len(items))),
        "total_pages": int(headers.get("X-WP-TotalPages", 1)),
    }


# --------------------------------------------------------------------------
# C1  Listar artículos
# --------------------------------------------------------------------------
@router.get("/posts", dependencies=[AUTH])
def list_posts(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    category: str | None = Query(None, pattern=SLUG_RE, max_length=100),
    tag: str | None = Query(None, pattern=SLUG_RE, max_length=100),
    view: Literal["summary", "full"] = "summary",
):
    params = {
        "page": page, "per_page": per_page, "_embed": 1,
        "orderby": "date", "order": "desc",
    }
    if category:
        params["categories"] = _term_id("categories", category)
    if tag:
        params["tags"] = _term_id("tags", tag)

    data, headers = wp_get("/posts", params)
    items = [n for n in (normalize_post(p) for p in data) if n]
    if view == "summary":  # el resumen no incluye el cuerpo (más ligero)
        for item in items:
            item.pop("content_html")
    return _paginated(items, headers, page, per_page)


# --------------------------------------------------------------------------
# C2  Obtener un artículo
# --------------------------------------------------------------------------
@router.get("/posts/{slug}", dependencies=[AUTH])
def get_post(slug: str = Path(pattern=SLUG_RE, max_length=100)):
    data, _ = wp_get("/posts", {"slug": slug, "_embed": 1})
    post = normalize_post(data[0]) if data else None
    if not post:
        raise ApiError(404, "NOT_FOUND", "El artículo no existe o no está publicado")
    return post


# --------------------------------------------------------------------------
# C3  Listar páginas   |   C4  Obtener una página
# --------------------------------------------------------------------------
def _page_slug_map():
    pages = wp_get_all("/pages", {"_fields": "id,slug"})
    return {p["id"]: p["slug"] for p in pages}


@router.get("/pages", dependencies=[AUTH])
def list_pages(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=100)):
    data, headers = wp_get(
        "/pages", {"page": page, "per_page": per_page, "orderby": "menu_order", "order": "asc"}
    )
    slugs = _page_slug_map()
    items = [n for n in (normalize_page(p, slugs) for p in data) if n]
    return _paginated(items, headers, page, per_page)


@router.get("/pages/{slug}", dependencies=[AUTH])
def get_page(slug: str = Path(pattern=SLUG_RE, max_length=100)):
    data, _ = wp_get("/pages", {"slug": slug})
    page = normalize_page(data[0], _page_slug_map()) if data else None
    if not page:
        raise ApiError(404, "NOT_FOUND", "La página no existe o no está publicada")
    return page


# --------------------------------------------------------------------------
# C5  Categorías   |   C6  Etiquetas  (solo las que tienen artículos)
# --------------------------------------------------------------------------
@router.get("/categories", dependencies=[AUTH])
def list_categories():
    data = wp_get_all("/categories", {"hide_empty": "true"})
    items = [normalize_term(t, True) for t in data if t.get("count", 0) >= 1][:500]
    return {"items": items, "next_cursor": None}


@router.get("/tags", dependencies=[AUTH])
def list_tags():
    data = wp_get_all("/tags", {"hide_empty": "true"})
    items = [normalize_term(t, False) for t in data if t.get("count", 0) >= 1][:500]
    return {"items": items, "next_cursor": None}


# --------------------------------------------------------------------------
# C7  Versión del contenido
#   La versión es un SHA-256 de los IDs y fechas de modificación publicados.
#   Si se agrega, edita, despublica o borra algo, la huella cambia.
#   La API 2 también llama a esta función directamente (sin pasar por HTTP).
# --------------------------------------------------------------------------
def compute_content_version() -> dict:
    posts = wp_get_all("/posts", {"_fields": "id,modified_gmt"})
    pages = wp_get_all("/pages", {"_fields": "id,modified_gmt"})
    cats = wp_get_all("/categories", {"hide_empty": "true", "_fields": "id,count"})
    tags = wp_get_all("/tags", {"hide_empty": "true", "_fields": "id,count"})

    lines = sorted(
        [f"post:{p['id']}:{p['modified_gmt']}" for p in posts]
        + [f"page:{p['id']}:{p['modified_gmt']}" for p in pages]
        + [f"cat:{c['id']}:{c['count']}" for c in cats]
        + [f"tag:{t['id']}:{t['count']}" for t in tags]
    )
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()

    now = datetime.now(timezone.utc)
    modified = [x["modified_gmt"] for x in posts + pages]
    return {
        "version": digest,
        "last_modified_at": _utc(max(modified)) if modified else iso(now),
        "counts": {
            "posts": len(posts), "pages": len(pages),
            "categories": len(cats), "tags": len(tags),
        },
        "generated_at": iso(now),
    }


@router.get("/content-version", dependencies=[AUTH])
def content_version():
    return compute_content_version()


# --------------------------------------------------------------------------
# H1  Salud (sin token): verifica que WordPress responda
# --------------------------------------------------------------------------
@router.get("/health")
def health():
    try:
        wp_get("/categories", {"per_page": 1})
        wordpress = "ok"
    except ApiError:
        wordpress = "fail"
    if wordpress == "fail":
        raise ApiError(503, "DEPENDENCY_DOWN", "WordPress no responde",
                       detail="dependencies: wordpress=fail")
    return {"status": "ok", "version": "v1", "dependencies": {"wordpress": wordpress}}
