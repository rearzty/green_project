from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    # asyncpg, not psycopg2 -- backend/app/db/session.py uses a real async
    # SQLAlchemy engine (create_async_engine), not the sync one.
    database_url: str = "postgresql+asyncpg://greenproject:greenproject@localhost:5432/greenproject"
    planting_norms_path: Path = REPO_ROOT / "geo_engine" / "config" / "planting_norms.yaml"
    ml_artifact_path: Path = REPO_ROOT / "ml_scoring" / "artifacts" / "model.joblib"
    # Browsers treat localhost and 127.0.0.1 as different origins even
    # though they're the same machine — both need to be allowed, or
    # whichever one a dev happens to type in the address bar gets a CORS
    # error on every request ("failed to fetch" with no server-side trace,
    # since the browser blocks the request before it's sent).
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    # Also match any device on a private LAN (phone on the same wifi, e.g.
    # testing from http://192.168.1.43:3000) regardless of which address
    # DHCP happens to hand out -- a fixed IP in cors_origins would break the
    # moment the router reassigns it. Covers all of RFC 1918 (10.0.0.0/8,
    # 172.16.0.0/12, 192.168.0.0/16), not just 192.168.x.x, since which
    # private range a given router uses varies.
    cors_origin_regex: str = r"^http://(192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}):3000$"

    model_config = SettingsConfigDict(env_prefix="GREENPROJECT_", env_file=".env", extra="ignore")


settings = Settings()
