from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from backend.app.api import (
    routes_assistant,
    routes_compliance,
    routes_config,
    routes_edit,
    routes_export,
    routes_generate,
    routes_projects,
)
from backend.app.core.config import settings

app = FastAPI(title="GreenProject API", version="0.1.0")

# A real-scale plan's GeoJSON response is tens of MB uncompressed (measured:
# ~35MB for 100k items) -- gzip gets that down to roughly an eighth
# (measured ~13%) for basically free (single-digit ms of CPU). Middleware
# order matters: added after CORS so it runs on the way out closer to the
# response body, before CORS headers get attached on the way back through.
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


app.include_router(routes_projects.router)
app.include_router(routes_generate.router)
app.include_router(routes_edit.router)
app.include_router(routes_export.router)
app.include_router(routes_config.router)
app.include_router(routes_assistant.router)
app.include_router(routes_compliance.router)
