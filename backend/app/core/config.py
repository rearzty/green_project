from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://greenproject:greenproject@localhost:5432/greenproject"
    planting_norms_path: Path = REPO_ROOT / "geo_engine" / "config" / "planting_norms.yaml"
    ml_artifact_path: Path = REPO_ROOT / "ml_scoring" / "artifacts" / "model.joblib"
    cors_origins: list[str] = ["http://localhost:3000"]

    model_config = SettingsConfigDict(env_prefix="GREENPROJECT_", env_file=".env", extra="ignore")


settings = Settings()
