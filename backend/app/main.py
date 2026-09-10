from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.api import routes_config, routes_edit, routes_export, routes_generate, routes_projects
from backend.app.core.config import settings
from backend.app.db.session import init_db


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    # Dev/demo convenience: creates tables if they don't exist yet.
    # Swap for Alembic migrations (backend/app/db/migrations) once schema churn settles.
    init_db()
    yield


app = FastAPI(title="GreenProject API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
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
