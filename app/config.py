"""Конфигурация приложения. Все значения читаются из переменных окружения (12-factor)."""
from __future__ import annotations

import os


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///ai_buddy.db")
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # LLM (опционально). Без ключа агент работает в офлайн-режиме (retrieval + шаблоны).
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "offline")  # offline | openai
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "25"))

    # Фоновые задачи: в тестах выполняются синхронно.
    SYNC_JOBS = _bool("SYNC_JOBS", False)
    JOB_WORKERS = int(os.getenv("JOB_WORKERS", "4"))

    # Имитация согласований: заявки на чтение одобряются политикой через N секунд.
    ACCESS_AUTO_APPROVE_SECONDS = int(os.getenv("ACCESS_AUTO_APPROVE_SECONDS", "8"))

    CRON_TOKEN = os.getenv("CRON_TOKEN", "dev-cron-token")
    SEED_ON_START = _bool("SEED_ON_START", True)
    WTF_CSRF_ENABLED = _bool("CSRF_ENABLED", True)


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    SYNC_JOBS = True
    LLM_PROVIDER = "offline"
    ACCESS_AUTO_APPROVE_SECONDS = 0
    WTF_CSRF_ENABLED = False
    SEED_ON_START = True
