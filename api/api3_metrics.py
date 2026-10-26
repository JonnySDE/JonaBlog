"""
API 3 - API DE MÉTRICAS (GraphQL)   (ruta: /metrics/v1/graphql)
=============================================================================
¿Qué hace?
  Registra eventos ANÓNIMOS de lectura del blog (sin IP, sin cookies) y
  ofrece consultas con resúmenes: visitas, artículos más leídos y origen
  del tráfico. Es GraphQL, como pidió el profe: un solo endpoint y el
  cliente decide qué campos quiere.

¿Qué NO hace?
  No identifica personas, no guarda IP ni cookies, no administra contenido.

Quién la usa:
  * Mutation  recordEvent  -> el navegador del lector (PÚBLICA, solo desde SITE_ORIGIN)
  * Queries   summary, posts, post, sources -> administrador (ADMIN_API_TOKEN)

Operaciones GraphQL (equivalen a M1..M5 del documento):
  M1  mutation recordEvent(input: EventInput!)
  M2  query summary(from, to)
  M3  query posts(from, to, sort, limit)
  M4  query post(slug, from, to)
  M5  query sources(from, to, limit)
  H1  GET /metrics/v1/health  (REST, sin token)
"""
import datetime as dt
import os
import re
import uuid as uuidlib
from typing import Annotated, Optional
from urllib.parse import urlparse

import strawberry
from fastapi import APIRouter
from graphql import GraphQLError
from strawberry.fastapi import GraphQLRouter
from strawberry.permission import BasePermission
from strawberry.types import Info

from common import SLUG_RE, ApiError, get_conn, token_is_valid

BOT_RE = re.compile(r"bot|crawl|spider|slurp|headless|lighthouse|curl|wget|python-requests",
                    re.I)


def gql_error(code: str, message: str) -> GraphQLError:
    return GraphQLError(message, extensions={"code": code})


# --------------------------------------------------------------------------
# Permiso: solo el administrador puede consultar
# --------------------------------------------------------------------------
class IsAdmin(BasePermission):
    message = "Token ausente o inválido"
    error_extensions = {"code": "UNAUTHORIZED"}

    def has_permission(self, source, info: Info, **kwargs) -> bool:
        request = info.context["request"]
        return token_is_valid(request.headers.get("authorization", ""), ["ADMIN_API_TOKEN"])


# --------------------------------------------------------------------------
# Tipos GraphQL de salida (Paso 4.8 a 4.10)
# --------------------------------------------------------------------------
@strawberry.type
class SourceMetrics:
    source: str
    sessions: int
    pageviews: int


@strawberry.type
class Devices:
    mobile: int
    tablet: int
    desktop: int


@strawberry.type
class DailyViews:
    date: dt.date
    pageviews: int


@strawberry.type
class Summary:
    from_: dt.date = strawberry.field(name="from")
    to: dt.date
    pageviews: int
    sessions: int
    avg_read_seconds: Optional[float]
    devices: Devices
    top_sources: list[SourceMetrics]


@strawberry.type
class PostMetrics:
    slug: str
    pageviews: int
    sessions: int
    avg_read_seconds: Optional[float]
    avg_scroll_percent: Optional[float]
    daily: Optional[list[DailyViews]] = None      # solo en el detalle (query post)
    sources: Optional[list[SourceMetrics]] = None  # solo en el detalle


@strawberry.type
class EventResult:
    status: str  # "accepted" o "duplicate"


@strawberry.input
class EventInput:
    event_id: str
    type: str                       # "pageview" o "read"
    path: str
    session_id: str
    slug: Optional[str] = None
    referrer: Optional[str] = None
    read_seconds: Optional[int] = None
    scroll_percent: Optional[int] = None


# "from" es palabra reservada en Python, por eso el argumento se llama from_ y se
# expone en GraphQL como "from".
FromArg = Annotated[Optional[dt.date], strawberry.argument(name="from")]


# --------------------------------------------------------------------------
# Utilidades de consulta
# --------------------------------------------------------------------------
def resolve_range(from_, to):
    """Por defecto los últimos 30 días; máximo 366 días."""
    today = dt.datetime.now(dt.timezone.utc).date()
    to = to or today
    from_ = from_ or (to - dt.timedelta(days=29))
    if to < from_ or (to - from_).days > 365:
        raise gql_error("INVALID_RANGE", "Rango de fechas inválido (máximo 366 días)")
    start = dt.datetime.combine(from_, dt.time.min, tzinfo=dt.timezone.utc)
    end = dt.datetime.combine(to + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc)
    return from_, to, start, end


def _num(value):
    return float(value) if value is not None else None


def query_sources(conn, start, end, limit, slug=None):
    """
    Origen de cada SESIÓN = referente de su primer evento (así la navegación
    interna del blog no cuenta como 'direct').
    """
    extra, params = "", [start, end, start, end]
    if slug:
        extra = " AND e.slug = %s"
        params.append(slug)
    params.append(limit)
    rows = conn.execute(
        f"""WITH first_ev AS (
              SELECT DISTINCT ON (session_id) session_id, referrer_domain
              FROM events WHERE occurred_at >= %s AND occurred_at < %s
              ORDER BY session_id, occurred_at)
            SELECT coalesce(f.referrer_domain, 'direct') AS source,
                   count(DISTINCT e.session_id) AS sessions,
                   count(*) FILTER (WHERE e.type = 'pageview') AS pageviews
            FROM events e JOIN first_ev f USING (session_id)
            WHERE e.occurred_at >= %s AND e.occurred_at < %s{extra}
            GROUP BY 1 ORDER BY sessions DESC, pageviews DESC LIMIT %s""",
        params,
    ).fetchall()
    return [SourceMetrics(source=r["source"], sessions=r["sessions"],
                          pageviews=r["pageviews"]) for r in rows]


def detect_device(user_agent: str) -> str:
    """Solo se guarda mobile/tablet/desktop; el user-agent NUNCA se guarda."""
    ua = user_agent.lower()
    if "ipad" in ua or "tablet" in ua or ("android" in ua and "mobile" not in ua):
        return "tablet"
    if "mobi" in ua or "iphone" in ua or "android" in ua:
        return "mobile"
    return "desktop"


def allowed_origins() -> list[str]:
    return [o.strip().rstrip("/") for o in os.environ.get("SITE_ORIGIN", "").split(",")
            if o.strip()]


# --------------------------------------------------------------------------
# Queries (solo administrador)
# --------------------------------------------------------------------------
@strawberry.type
class Query:
    @strawberry.field(permission_classes=[IsAdmin], description="M2 · Resumen general")
    def summary(
        self,
        from_: FromArg = None,
        to: Optional[dt.date] = None,
    ) -> Summary:
        from_, to, start, end = resolve_range(from_, to)
        with get_conn() as conn:
            t = conn.execute(
                """SELECT count(*) FILTER (WHERE type='pageview') AS pageviews,
                          count(DISTINCT session_id) AS sessions,
                          avg(read_seconds) FILTER (WHERE type='read') AS avg_read
                   FROM events WHERE occurred_at >= %s AND occurred_at < %s""",
                (start, end),
            ).fetchone()
            dev = {r["device"]: r["n"] for r in conn.execute(
                """SELECT device, count(DISTINCT session_id) AS n FROM events
                   WHERE occurred_at >= %s AND occurred_at < %s GROUP BY device""",
                (start, end)).fetchall()}
            sources = query_sources(conn, start, end, 5)
        return Summary(
            from_=from_, to=to, pageviews=t["pageviews"], sessions=t["sessions"],
            avg_read_seconds=_num(t["avg_read"]),
            devices=Devices(mobile=dev.get("mobile", 0), tablet=dev.get("tablet", 0),
                            desktop=dev.get("desktop", 0)),
            top_sources=sources,
        )

    @strawberry.field(permission_classes=[IsAdmin], description="M3 · Ranking de artículos")
    def posts(
        self,
        from_: FromArg = None,
        to: Optional[dt.date] = None,
        sort: str = "pageviews",
        limit: int = 10,
    ) -> list[PostMetrics]:
        # Lista blanca: nunca se mete texto del usuario dentro del ORDER BY.
        order = {"pageviews": "pageviews DESC", "sessions": "sessions DESC",
                 "read_time": "avg_read DESC NULLS LAST"}.get(sort)
        if not order:
            raise gql_error("VALIDATION_ERROR", "sort debe ser pageviews, sessions o read_time")
        if not 1 <= limit <= 50:
            raise gql_error("VALIDATION_ERROR", "limit debe estar entre 1 y 50")
        _, _, start, end = resolve_range(from_, to)
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT slug,
                           count(*) FILTER (WHERE type='pageview') AS pageviews,
                           count(DISTINCT session_id) AS sessions,
                           avg(read_seconds) FILTER (WHERE type='read') AS avg_read,
                           avg(scroll_percent) FILTER (WHERE type='read') AS avg_scroll
                    FROM events
                    WHERE slug IS NOT NULL AND occurred_at >= %s AND occurred_at < %s
                    GROUP BY slug ORDER BY {order} LIMIT %s""",
                (start, end, limit),
            ).fetchall()
        return [PostMetrics(slug=r["slug"], pageviews=r["pageviews"], sessions=r["sessions"],
                            avg_read_seconds=_num(r["avg_read"]),
                            avg_scroll_percent=_num(r["avg_scroll"])) for r in rows]

    @strawberry.field(permission_classes=[IsAdmin], description="M4 · Métricas de un artículo")
    def post(
        self,
        slug: str,
        from_: FromArg = None,
        to: Optional[dt.date] = None,
    ) -> PostMetrics:
        if not re.match(SLUG_RE, slug):
            raise gql_error("VALIDATION_ERROR", "slug inválido")
        _, _, start, end = resolve_range(from_, to)
        with get_conn() as conn:
            t = conn.execute(
                """SELECT count(*) AS total,
                          count(*) FILTER (WHERE type='pageview') AS pageviews,
                          count(DISTINCT session_id) AS sessions,
                          avg(read_seconds) FILTER (WHERE type='read') AS avg_read,
                          avg(scroll_percent) FILTER (WHERE type='read') AS avg_scroll
                   FROM events
                   WHERE slug = %s AND occurred_at >= %s AND occurred_at < %s""",
                (slug, start, end),
            ).fetchone()
            if t["total"] == 0:
                raise gql_error("NOT_FOUND", "No hay eventos para ese artículo en el periodo")
            daily = conn.execute(
                """SELECT (occurred_at AT TIME ZONE 'UTC')::date AS day,
                          count(*) FILTER (WHERE type='pageview') AS pageviews
                   FROM events WHERE slug = %s AND occurred_at >= %s AND occurred_at < %s
                   GROUP BY 1 ORDER BY 1""",
                (slug, start, end),
            ).fetchall()
            sources = query_sources(conn, start, end, 10, slug=slug)
        return PostMetrics(
            slug=slug, pageviews=t["pageviews"], sessions=t["sessions"],
            avg_read_seconds=_num(t["avg_read"]), avg_scroll_percent=_num(t["avg_scroll"]),
            daily=[DailyViews(date=d["day"], pageviews=d["pageviews"]) for d in daily],
            sources=sources,
        )

    @strawberry.field(permission_classes=[IsAdmin], description="M5 · Origen del tráfico")
    def sources(
        self,
        from_: FromArg = None,
        to: Optional[dt.date] = None,
        limit: int = 10,
    ) -> list[SourceMetrics]:
        if not 1 <= limit <= 50:
            raise gql_error("VALIDATION_ERROR", "limit debe estar entre 1 y 50")
        _, _, start, end = resolve_range(from_, to)
        with get_conn() as conn:
            return query_sources(conn, start, end, limit)


# --------------------------------------------------------------------------
# Mutation pública: registrar un evento (M1)
# --------------------------------------------------------------------------
def validate_event(e: EventInput):
    """Devuelve el UUID de event_id y session_id ya validados."""
    try:
        event_id = uuidlib.UUID(e.event_id)
        session_id = uuidlib.UUID(e.session_id)
    except ValueError:
        raise gql_error("VALIDATION_ERROR", "eventId y sessionId deben ser UUID")
    if e.type not in ("pageview", "read"):
        raise gql_error("VALIDATION_ERROR", "type debe ser pageview o read")
    if not e.path.startswith("/") or len(e.path) > 200:
        raise gql_error("VALIDATION_ERROR", "path debe empezar con / y tener hasta 200 caracteres")
    if e.slug is not None and (len(e.slug) > 100 or not re.match(SLUG_RE, e.slug)):
        raise gql_error("VALIDATION_ERROR", "slug inválido")
    if e.referrer is not None and len(e.referrer) > 300:
        raise gql_error("VALIDATION_ERROR", "referrer demasiado largo")
    if e.type == "pageview" and (e.read_seconds is not None or e.scroll_percent is not None):
        raise gql_error("VALIDATION_ERROR", "readSeconds y scrollPercent solo van en eventos read")
    if e.read_seconds is not None and not 0 <= e.read_seconds <= 3600:
        raise gql_error("VALIDATION_ERROR", "readSeconds debe estar entre 0 y 3600")
    if e.scroll_percent is not None and not 0 <= e.scroll_percent <= 100:
        raise gql_error("VALIDATION_ERROR", "scrollPercent debe estar entre 0 y 100")
    return event_id, session_id


@strawberry.type
class Mutation:
    @strawberry.mutation(description="M1 · Registrar un evento anónimo (público)")
    def record_event(self, info: Info, input: EventInput) -> EventResult:
        request = info.context["request"]
        origins = allowed_origins()

        # 1) Solo el dominio del blog puede enviar eventos.
        if request.headers.get("origin", "").rstrip("/") not in origins:
            raise gql_error("ORIGIN_NOT_ALLOWED", "Origen no permitido")
        # 2) Cuerpo pequeño (máx. 2 KB).
        if int(request.headers.get("content-length", 0) or 0) > 2048:
            raise gql_error("PAYLOAD_TOO_LARGE", "El cuerpo supera 2 KB")
        # 3) Bots conocidos se rechazan; el user-agent se usa y se descarta.
        user_agent = request.headers.get("user-agent", "")
        if BOT_RE.search(user_agent):
            raise gql_error("BOT_REJECTED", "Tráfico de bot rechazado")

        event_id, session_id = validate_event(input)

        # Solo se guarda el DOMINIO del referente, y se ignora el propio blog.
        referrer_domain = None
        if input.referrer:
            host = (urlparse(input.referrer).hostname or "").lower()
            own = {(urlparse(o).hostname or "").lower() for o in origins}
            if host and host not in own:
                referrer_domain = host[:100]

        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO events
                     (event_id, type, path, slug, session_id, referrer_domain,
                      device, read_seconds, scroll_percent)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (event_id) DO NOTHING""",
                (event_id, input.type, input.path, input.slug, session_id,
                 referrer_domain, detect_device(user_agent),
                 input.read_seconds, input.scroll_percent),
            )
            duplicated = cur.rowcount == 0   # occurred_at lo pone el servidor (now())
        return EventResult(status="duplicate" if duplicated else "accepted")


schema = strawberry.Schema(query=Query, mutation=Mutation)

# Router GraphQL (se monta en /metrics/v1/graphql desde index.py).
# graphql_ide=None: sin interfaz web pública; se usa con curl/Postman.
graphql_router = GraphQLRouter(schema, graphql_ide=None)

# Router REST solo para la salud.
router = APIRouter()


@router.get("/health")
def health():
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1")
    except ApiError:
        raise ApiError(503, "DEPENDENCY_DOWN", "La base de datos no responde",
                       detail="dependencies: database=fail")
    return {"status": "ok", "version": "v1", "dependencies": {"database": "ok"}}
