"""Юна's tests mock the model's HTTP call. A real key in the developer's .env
(green_project/.env, gitignored) or shell must neither turn a test that
forgot to set the provider into a paid request nor change the defaults the
settings tests check."""

import pytest

from backend.app.core import config

_LLM_FIELDS = ("llm_base_url", "llm_model", "llm_api_key", "llm_timeout_s", "llm_reasoning_effort", "groq_api_key")


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    for field in _LLM_FIELDS:
        monkeypatch.delenv(f"GREENPROJECT_{field.upper()}", raising=False)
    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    defaults = config.Settings()
    for field in _LLM_FIELDS:
        monkeypatch.setattr(config.settings, field, getattr(defaults, field))
