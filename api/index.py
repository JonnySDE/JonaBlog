"""
index.py - Punto de entrada. Junta las 3 APIs en una sola aplicación FastAPI.

  api1_cms.py        -> /cms/v1/...
  api2_generator.py  -> /generator/v1/...
  api3_metrics.py    -> /metrics/v1/graphql   (GraphQL)  y  /metrics/v1/health

En Vercel cada ruta corre como una función serverless; en tu computadora:
    uvicorn index:app --reload --port 8000
"""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import api1_cms
import api2_generator
import api3_metrics
from common import register_error_handlers

app = FastAPI(
    title="jonablog APIs",
    version="1.0.0",
    docs_url="/docs",   # documentación interactiva de las APIs REST
    redoc_url=None,
)
register_error_handlers(app)

# CORS: solo el dominio del blog puede llamar a la API desde el navegador
# (necesario para que el script de métricas pueda enviar eventos).
origins = [o.strip().rstrip("/") for o in os.environ.get("SITE_ORIGIN", "").split(",")
           if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(api1_cms.router, prefix="/cms/v1", tags=["API 1 · CMS"])
app.include_router(api2_generator.router, prefix="/generator/v1", tags=["API 2 · Generador"])
app.include_router(api3_metrics.router, prefix="/metrics/v1", tags=["API 3 · Métricas"])
app.include_router(api3_metrics.graphql_router, prefix="/metrics/v1/graphql")


@app.get("/", include_in_schema=False)
def root():
    return {"name": "jonablog APIs", "apis": ["/cms/v1", "/generator/v1", "/metrics/v1/graphql"]}
