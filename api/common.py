"""
common.py - Utilidades compartidas por las tres APIs
=====================================================
Aquí vive todo lo que api1, api2 y api3 usan en común:
  * Formato de errores estándar  { code, message, detail, request_id, errors[] }
  * Autenticación con tokens Bearer (comparación en tiempo constante)
  * Conexión a Postgres (Neon)
  * Cliente de WordPress (solo lo usa api1, pero api2 también necesita la versión)
"""
import hmac
import html as htmllib
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone

import httpx
import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from starlette.exceptions import HTTPException as StarletteHTTPException

load_dotenv()  # En local lee el archivo .env; en Vercel las variables ya existen.

# Slug válido: minúsculas, números y guiones (hasta 100 caracteres)
SLUG_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


# --------------------------------------------------------------------------
# Errores
# --------------------------------------------------------------------------
class ApiError(Exception):
    """Error de negocio con código HTTP y código estable para el cliente."""

    def __init__(self, status, code, message, detail=None, errors=None, headers=None):
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail
        self.errors = errors or []
        self.headers = headers
        super().__init__(message)


def error_body(code, message, detail=None, errors=None):
    body = {
        "code": code,
        "message": message,
        "request_id": str(uuid.uuid4()),  # para rastrear en los logs de Vercel
        "errors": errors or [],
    }
    if detail:
        body["detail"] = detail
    return body


def register_error_handlers(app: FastAPI):
    """Hace que TODOS los errores salgan con el mismo formato."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(
            error_body(exc.code, exc.message, exc.detail, exc.errors),
            status_code=exc.status,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            error_body("VALIDATION_ERROR", "Datos inválidos", errors=errors),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        names = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
        return JSONResponse(
            error_body(names.get(exc.status_code, "HTTP_ERROR"), str(exc.detail)),
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        print(f"ERROR no controlado: {exc!r}")  # se ve en los logs de Vercel
        return JSONResponse(
            error_body("INTERNAL_ERROR", "Error interno"), status_code=500
        )


# --------------------------------------------------------------------------
# Autenticación (tokens de servicio, uno por función)
# --------------------------------------------------------------------------
def token_is_valid(authorization: str, env_names) -> bool:
    """True si el encabezado 'Bearer xxx' coincide con alguno de los tokens."""
    if not authorization.lower().startswith("bearer "):
        return False
    token = authorization[7:].strip().encode()
    for name in env_names:
        expected = os.environ.get(name, "").encode()
        # compare_digest = comparación en tiempo constante (NFR04)
        if expected and hmac.compare_digest(token, expected):
            return True
    return False


def require_token(*env_names):
    """Dependencia de FastAPI: exige que el token sea alguno de env_names."""

    def dependency(request: Request):
        if not token_is_valid(request.headers.get("authorization", ""), env_names):
            raise ApiError(
                401, "UNAUTHORIZED", "Token ausente o inválido",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return dependency


# --------------------------------------------------------------------------
# Base de datos (Postgres en Neon)
# --------------------------------------------------------------------------
def get_conn():
    """
    Abre una conexión nueva. En serverless no se guardan conexiones entre
    llamadas (NFR09), por eso se abre y se cierra en cada petición:
        with get_conn() as conn:
            conn.execute(...)
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ApiError(500, "DB_NOT_CONFIGURED", "Falta DATABASE_URL")
    try:
        return psycopg.connect(
            url,
            row_factory=dict_row,   # filas como diccionarios
            autocommit=True,        # cada sentencia se confirma sola
            connect_timeout=5,
            prepare_threshold=None, # necesario con el pooler de Neon (pgbouncer)
        )
    except psycopg.OperationalError:
        raise ApiError(503, "DB_UNAVAILABLE", "La base de datos no responde")


# --------------------------------------------------------------------------
# Utilidades de fecha y UUID
# --------------------------------------------------------------------------
def iso(dt: datetime) -> str:
    """datetime -> '2026-10-15T14:30:00Z' (UTC)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def uuid7() -> uuid.UUID:
    """UUID v7 (ordenable por tiempo) para publication_id."""
    ms = int(time.time() * 1000)
    b = bytearray(ms.to_bytes(6, "big") + os.urandom(10))
    b[6] = (b[6] & 0x0F) | 0x70  # versión 7
    b[8] = (b[8] & 0x3F) | 0x80  # variante RFC 4122
    return uuid.UUID(bytes=bytes(b))


# --------------------------------------------------------------------------
# Texto y HTML
# --------------------------------------------------------------------------
def strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", htmllib.unescape(text)).strip()


def clean_html(raw: str) -> str:
    """Invariante 4: sin <script> ni atributos de evento (onclick=...)."""
    raw = re.sub(r"<script\b[^>]*>.*?</script>", "", raw or "", flags=re.I | re.S)
    raw = re.sub(r"""\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", "", raw, flags=re.I)
    return raw


def reading_minutes(html_text: str) -> int:
    words = len(strip_tags(html_text).split())
    return max(1, math.ceil(words / 200))


# --------------------------------------------------------------------------
# Cliente de WordPress (API REST pública, solo lectura)
# --------------------------------------------------------------------------
def wp_base() -> str:
    site = os.environ.get("WORDPRESS_SITE")
    if not site:
        raise ApiError(500, "WP_NOT_CONFIGURED", "Falta WORDPRESS_SITE")
    return f"https://public-api.wordpress.com/wp/v2/sites/{site}"


def wp_get(path: str, params=None):
    """GET a WordPress. Timeout 8 s y un reintento ante 429/5xx (Paso 5.5)."""
    response = None
    for attempt in range(2):
        try:
            response = httpx.get(wp_base() + path, params=params, timeout=8)
        except httpx.TimeoutException:
            raise ApiError(503, "WORDPRESS_TIMEOUT", "WordPress no respondió a tiempo")
        except httpx.HTTPError:
            raise ApiError(502, "WORDPRESS_ERROR", "No se pudo contactar a WordPress")
        if (response.status_code == 429 or response.status_code >= 500) and attempt == 0:
            time.sleep(0.5)
            continue
        break

    # Pedir una página más allá del final no es un error real: lista vacía.
    if response.status_code == 400 and "invalid_page_number" in response.text:
        return [], response.headers
    if response.status_code >= 400:
        raise ApiError(
            502, "WORDPRESS_ERROR", "WordPress respondió con error",
            detail=f"Código {response.status_code}",
        )
    return response.json(), response.headers


def wp_get_all(path: str, params=None):
    """Recorre todas las páginas de una lista de WordPress."""
    params = dict(params or {})
    params["per_page"] = 100
    items, page = [], 1
    while True:
        params["page"] = page
        data, headers = wp_get(path, params)
        items.extend(data)
        total_pages = int(headers.get("X-WP-TotalPages", 1))
        if page >= total_pages or not data:
            return items
        page += 1
