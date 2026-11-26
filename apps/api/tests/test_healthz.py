"""Startup and health check tests for the local FastAPI scaffold."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from career_memory_api.app import create_app
from career_memory_api.config import Settings
from career_memory_api.main import app as main_app


def test_healthz_on_created_app() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_on_main_entrypoint_app() -> None:
    with TestClient(main_app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_settings_from_env_defaults_and_overrides() -> None:
    defaults = Settings.from_env({})
    assert defaults.host == "127.0.0.1"
    assert defaults.port == 8000
    assert defaults.log_level == "info"

    custom = Settings.from_env(
        {
            "CAREER_MEMORY_API_NAME": "Custom Local API",
            "CAREER_MEMORY_API_HOST": "0.0.0.0",
            "CAREER_MEMORY_API_PORT": "8081",
            "CAREER_MEMORY_API_LOG_LEVEL": "DEBUG",
        }
    )
    assert custom.app_name == "Custom Local API"
    assert custom.host == "0.0.0.0"
    assert custom.port == 8081
    assert custom.log_level == "debug"


def test_settings_from_env_rejects_invalid_port() -> None:
    with pytest.raises(ValueError, match="CAREER_MEMORY_API_PORT"):
        Settings.from_env({"CAREER_MEMORY_API_PORT": "not-a-port"})

    with pytest.raises(ValueError, match="port must be between"):
        Settings.from_env({"CAREER_MEMORY_API_PORT": "70000"})
