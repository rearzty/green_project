from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]

SELECTEL_BASE_URL = "https://api.selectel.ru/aig/v1"
SELECTEL_MODEL = "z-ai/glm-5.3-flash"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# Groq's lineup turns over without notice (llama-3.3-70b-versatile was
# retired outright, 404 model_not_found) -- if Юна stops answering, check
# GET {llm_base_url}/models before picking a replacement.
GROQ_MODEL = "openai/gpt-oss-20b"

# A model running on this machine (Ollama, llama.cpp, LM Studio) takes no
# key -- demanding one would report a perfectly working local setup as "not
# connected".
_LOCAL_LLM_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})
_OLLAMA_PORT = 11434


def _llm_host_port(url: str) -> tuple[str, int | None]:
    parts = urlsplit(url or "")
    try:
        port = parts.port
    except ValueError:  # a typo in the port must not crash importing settings
        port = None
    return (parts.hostname or "").lower(), port


class Settings(BaseSettings):
    planting_norms_path: Path = REPO_ROOT / "geo_engine" / "config" / "planting_norms.yaml"
    ml_artifact_path: Path = REPO_ROOT / "ml_scoring" / "artifacts" / "model.joblib"
    designer_artifact_path: Path = REPO_ROOT / "ml_scoring" / "artifacts" / "designer_model.joblib"
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

    # Юна (backend/app/services/assistant_service.py) -- any OpenAI-compatible
    # chat completions endpoint with tool calling: Selectel AI Gateway by
    # default (only the key is needed), a local Ollama, Groq (setups in
    # infra/.env.example). Without a model Юна isn't off: routes_assistant.py
    # answers with the built-in command parser (assistant_rules.py) instead,
    # so an image built straight from the repo, with no key at all, still has
    # a working chat.
    llm_base_url: str = SELECTEL_BASE_URL
    # "z-ai/glm-5.3-flash", not "selectel/glm-5.3-flash": the gateway lists
    # both, and only the first has tool calling (capabilities in GET
    # {llm_base_url}/models) -- without tools Юна can't change anything.
    llm_model: str = SELECTEL_MODEL
    llm_api_key: str | None = None
    # A local model on CPU takes tens of seconds per answer; the 20 s that
    # was enough for a hosted model would time out every Ollama reply.
    llm_timeout_s: float = 45.0
    # Sent as "reasoning_effort". GLM-5.3 Flash thinks before every answer by
    # default: 5.5 s per reply against 2.4 s at "low", the same tool call
    # (measured on Selectel) -- so unset means "low" there and nothing
    # elsewhere (not every model accepts the field). "off" never sends it.
    llm_reasoning_effort: str | None = None
    # The key's old name -- an infra/.env set up when Groq was the default
    # keeps working unchanged (see _legacy_groq_key).
    groq_api_key: str | None = None

    # env_ignore_empty: docker-compose passes every unset variable through as
    # an empty string (${VAR:-}), and without this an empty
    # GREENPROJECT_LLM_BASE_URL would overwrite the default URL with "".
    model_config = SettingsConfigDict(env_prefix="GREENPROJECT_", env_file=".env", extra="ignore", env_ignore_empty=True)

    @model_validator(mode="after")
    def _legacy_groq_key(self) -> Settings:
        """A Groq key alone still means Groq: sending it to the new default
        endpoint would turn a working setup into "ключ не подошёл"."""
        if self.groq_api_key and not self.llm_api_key and "llm_base_url" not in self.model_fields_set:
            self.llm_base_url = GROQ_BASE_URL
            if "llm_model" not in self.model_fields_set:
                self.llm_model = GROQ_MODEL
        return self

    @property
    def llm_reasoning(self) -> str | None:
        """What goes into "reasoning_effort", None -- leave the field out."""
        if self.llm_reasoning_effort:
            return None if self.llm_reasoning_effort.strip().lower() == "off" else self.llm_reasoning_effort.strip()
        return "low" if self.llm_provider == "selectel" else None

    @property
    def llm_key(self) -> str | None:
        return self.llm_api_key or self.groq_api_key or None

    @property
    def llm_configured(self) -> bool:
        """A key, or an endpoint that needs none (a model on this machine or
        an Ollama anywhere on the network)."""
        if self.llm_key:
            return True
        host, port = _llm_host_port(self.llm_base_url)
        return host in _LOCAL_LLM_HOSTS or port == _OLLAMA_PORT or "ollama" in host

    @property
    def llm_provider(self) -> str:
        """For GET /api/config/assistant -- which setup is actually live."""
        if not self.llm_configured:
            return "none"
        host, port = _llm_host_port(self.llm_base_url)
        if host == "selectel.ru" or host.endswith(".selectel.ru"):
            return "selectel"
        if host == "groq.com" or host.endswith(".groq.com"):
            return "groq"
        if port == _OLLAMA_PORT or "ollama" in host:
            return "ollama"
        return "openai-compatible"


settings = Settings()
