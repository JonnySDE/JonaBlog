"""
API 2 - API DEL GENERADOR   (ruta base: /generator/v1)
=============================================================================
¿Qué hace?
  Decide CUÁNDO reconstruir el sitio. Le pide a Vercel (con el Deploy Hook)
  que ejecute un nuevo build de Astro, guarda el historial de publicaciones
  en Postgres y consulta a Vercel para saber si el build salió bien.

¿Qué NO hace?
  No edita contenido, no construye el sitio (lo hace Vercel) y no mide visitas.

Quién la usa: el administrador (ADMIN_API_TOKEN) y Vercel Cron (CRON_SECRET).

Endpoints:
  G1  POST /generator/v1/publications        pedir una publicación
  G2  GET  /generator/v1/publications        historial
  G3  GET  /generator/v1/publications/{id}   estado (y consulta a Vercel)
  G4  GET  /generator/v1/sync                la usa el cron diario
  H1  GET  /generator/v1/health              salud (sin token)

Estados de una publicación (status):  REQUESTED -> ACCEPTED | FAILED
Estados del build (vercel_state):     PENDING -> QUEUED/INITIALIZING -> BUILDING
                                       -> READY | ERROR | CANCELED
"""
import os
import uuid as uuidlib
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, StrictBool

from api1_cms import compute_content_version
from common import ApiError, get_conn, iso, require_token, uuid7

router = APIRouter()

ADMIN = Depends(require_token("ADMIN_API_TOKEN"))
# El cron usa CRON_SECRET; se acepta también ADMIN para poder probar a mano.
CRON = Depends(require_token("CRON_SECRET", "ADMIN_API_TOKEN"))

FINAL_STATES = {"READY", "ERROR", "CANCELED"}
STATE_ORDER = {"PENDING": 0, "QUEUED": 1, "INITIALIZING": 2, "BUILDING": 3,
               "READY": 4, "ERROR": 4, "CANCELED": 4}   # vercel_state solo avanza


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def serialize(row: dict) -> dict:
    """Fila de la base de datos -> JSON de Publication (Paso 4.6)."""
    out = dict(row)
    out["publication_id"] = str(out["publication_id"])
    out["created_at"] = iso(out["created_at"])
    out["updated_at"] = iso(out["updated_at"])
    return out


def check_not_too_recent(conn):
    """Regla: máximo una publicación ACCEPTED por minuto (límite del hook)."""
    row = conn.execute(
        """SELECT ceil(60 - extract(epoch FROM (now() - created_at)))::int AS wait
           FROM publications
           WHERE status = 'ACCEPTED' AND created_at > now() - interval '60 seconds'
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()
    if row:
        raise ApiError(409, "PUBLICATION_TOO_RECENT", "Ya hay una publicación reciente",
                       detail=f"Espere {row['wait']} segundos")


def last_accepted_version(conn):
    row = conn.execute(
        """SELECT content_version FROM publications
           WHERE status = 'ACCEPTED' AND content_version IS NOT NULL
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()
    return row["content_version"] if row else None


def try_content_version():
    """Versión actual del contenido, o None si WordPress no responde."""
    try:
        return compute_content_version()["version"]
    except ApiError:
        return None


def fire_deploy_hook():
    """
    V1: llama al Deploy Hook de Vercel (POST sin cuerpo).
    Devuelve (job_id, None) si funcionó o (None, {code, message}) si falló.
    Que Vercel acepte la orden NO significa que el build termine bien.
    """
    url = os.environ.get("VERCEL_DEPLOY_HOOK_URL")
    if not url:
        return None, {"code": "HOOK_NOT_CONFIGURED",
                      "message": "Falta VERCEL_DEPLOY_HOOK_URL"}
    try:
        r = httpx.post(url, timeout=5)
    except httpx.HTTPError:
        return None, {"code": "VERCEL_HOOK_ERROR", "message": "Vercel no respondió"}
    if r.status_code >= 400:
        return None, {"code": "VERCEL_HOOK_ERROR",
                      "message": f"Vercel rechazó la orden ({r.status_code})"}
    try:
        job_id = r.json().get("job", {}).get("id")
    except ValueError:
        job_id = None
    return job_id, None


def create_publication(conn, trigger, requested_by, reason, content_version) -> dict:
    """Crea el registro (REQUESTED), dispara el hook y lo deja ACCEPTED o FAILED."""
    pid = uuid7()
    conn.execute(
        """INSERT INTO publications
             (publication_id, status, trigger, requested_by, reason, content_version)
           VALUES (%s, 'REQUESTED', %s, %s, %s, %s)""",
        (pid, trigger, requested_by, reason, content_version),
    )
    job_id, err = fire_deploy_hook()
    if err:
        conn.execute(
            "UPDATE publications SET status='FAILED', error=%s, updated_at=now() "
            "WHERE publication_id=%s",
            (Jsonb(err), pid),
        )
        http = 503 if err["code"] == "HOOK_NOT_CONFIGURED" else 502
        raise ApiError(http, err["code"], err["message"])
    row = conn.execute(
        """UPDATE publications
           SET status='ACCEPTED', vercel_job_id=%s, vercel_state='PENDING', updated_at=now()
           WHERE publication_id=%s RETURNING *""",
        (job_id, pid),
    ).fetchone()
    return row


def refresh_from_vercel(conn, row) -> tuple[dict, bool]:
    """
    V2: pregunta a Vercel por el estado del build y lo guarda.
    Devuelve (fila, stale). stale=True si no se pudo consultar.
    """
    token = os.environ.get("VERCEL_API_TOKEN")
    project = os.environ.get("VERCEL_WEB_PROJECT_ID")
    if not (token and project):
        return row, True
    params = {
        "projectId": project,
        "since": int(row["created_at"].timestamp() * 1000),
        "limit": 5,
    }
    if os.environ.get("VERCEL_TEAM_ID"):
        params["teamId"] = os.environ["VERCEL_TEAM_ID"]
    try:
        r = httpx.get("https://api.vercel.com/v6/deployments", params=params,
                      headers={"Authorization": f"Bearer {token}"}, timeout=5)
        if r.status_code >= 400:
            return row, True
        deployments = [d for d in r.json().get("deployments", [])
                       if d.get("source") == "api-trigger-git-deploy"]
    except (httpx.HTTPError, ValueError):
        return row, True

    if not deployments:                       # el build aún no aparece
        return row, False
    d = min(deployments, key=lambda x: x["created"])   # el más antiguo posterior
    state = d.get("readyState") or d.get("state")
    current = row["vercel_state"] or "PENDING"
    if state not in STATE_ORDER or STATE_ORDER[state] < STATE_ORDER[current]:
        return row, False                     # vercel_state nunca retrocede
    error = None
    if state == "ERROR":
        error = Jsonb({"code": d.get("errorCode", "BUILD_ERROR"),
                       "message": d.get("errorMessage", "El build falló")})
    row = conn.execute(
        """UPDATE publications
           SET vercel_state=%s, deployment_url=%s, error=COALESCE(%s, error), updated_at=now()
           WHERE publication_id=%s RETURNING *""",
        (state, d.get("url"), error, row["publication_id"]),
    ).fetchone()
    return row, False


# --------------------------------------------------------------------------
# G1  Solicitar una publicación
# --------------------------------------------------------------------------
class PublicationIn(BaseModel):
    reason: str | None = Field(None, max_length=200)
    only_if_changed: StrictBool = False


@router.post("/publications", dependencies=[ADMIN])
def request_publication(body: PublicationIn | None = None):
    body = body or PublicationIn()
    with get_conn() as conn:
        check_not_too_recent(conn)                       # 409 si fue hace < 1 min
        version = try_content_version()                  # None si WordPress falla
        if body.only_if_changed and version and version == last_accepted_version(conn):
            return JSONResponse(
                {"published": False, "reason": "NO_CHANGES", "content_version": version}
            )
        row = create_publication(conn, "manual", "admin", body.reason, version)
    return JSONResponse(serialize(row), status_code=201)


# --------------------------------------------------------------------------
# G2  Listar publicaciones (último estado guardado, sin consultar a Vercel)
# --------------------------------------------------------------------------
@router.get("/publications", dependencies=[ADMIN])
def list_publications(
    status: Literal["ACCEPTED", "FAILED"] | None = None,
    vercel_state: Literal["PENDING", "QUEUED", "INITIALIZING", "BUILDING",
                          "READY", "ERROR", "CANCELED"] | None = None,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
):
    where, params = [], []
    if status:
        where.append("status = %s"); params.append(status)
    if vercel_state:
        where.append("vercel_state = %s"); params.append(vercel_state)
    if cursor:                                   # cursor = created_at del último ítem
        where.append("created_at < %s"); params.append(cursor)
    sql = "SELECT * FROM publications"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit + 1)
    try:
        with get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
    except ApiError:
        raise
    except Exception:
        raise ApiError(422, "INVALID_CURSOR", "El cursor no es válido")
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [serialize(r) for r in rows],
        "next_cursor": rows[-1]["created_at"].isoformat() if more else None,
    }


# --------------------------------------------------------------------------
# G3  Consultar una publicación
# --------------------------------------------------------------------------
@router.get("/publications/{publication_id}", dependencies=[ADMIN])
def get_publication(publication_id: str, refresh: bool = True):
    try:
        pid = uuidlib.UUID(publication_id)
    except ValueError:
        raise ApiError(422, "VALIDATION_ERROR", "publication_id debe ser un UUID")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM publications WHERE publication_id=%s", (pid,)
        ).fetchone()
        if not row:
            raise ApiError(404, "NOT_FOUND", "La publicación no existe")
        stale = False
        if (refresh and row["status"] == "ACCEPTED"
                and row["vercel_state"] not in FINAL_STATES):
            row, stale = refresh_from_vercel(conn, row)
    headers = {"X-State-Stale": "true"} if stale else None
    return JSONResponse(serialize(row), headers=headers)


# --------------------------------------------------------------------------
# G4  Sincronizar (la llama Vercel Cron una vez al día)
#   Publica SOLO si el contenido cambió desde la última publicación aceptada.
#   Además borra eventos de métricas con más de 12 meses (retención, NFR08).
# --------------------------------------------------------------------------
@router.get("/sync", dependencies=[CRON])
def sync():
    with get_conn() as conn:
        try:  # limpieza de retención; si falla no debe impedir la sincronización
            conn.execute("DELETE FROM events WHERE occurred_at < now() - interval '12 months'")
        except Exception as e:
            print(f"ADVERTENCIA: limpieza de eventos falló: {e!r}")

        try:
            version = compute_content_version()["version"]
        except ApiError:
            raise ApiError(502, "CMS_UNAVAILABLE", "No se pudo consultar el contenido")

        if version == last_accepted_version(conn):
            return {"published": False, "reason": "NO_CHANGES"}
        try:
            check_not_too_recent(conn)
        except ApiError:
            return {"published": False, "reason": "PUBLICATION_TOO_RECENT"}

        row = create_publication(conn, "scheduled", "cron", "Sincronización diaria", version)
    return {"published": True, "reason": "CONTENT_CHANGED", "publication": serialize(row)}


# --------------------------------------------------------------------------
# H1  Salud (sin token): verifica la base de datos
# --------------------------------------------------------------------------
@router.get("/health")
def health():
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1")
    except ApiError:
        raise ApiError(503, "DEPENDENCY_DOWN", "La base de datos no responde",
                       detail="dependencies: database=fail")
    return {"status": "ok", "version": "v1", "dependencies": {"database": "ok"}}
