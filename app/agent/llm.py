"""LLM-провайдер (OpenAI-совместимый Chat Completions API). Опционален: без ключа агент работает офлайн."""
from __future__ import annotations

import requests
from flask import current_app

from ..analytics import flag


class LLMError(Exception):
    pass


def enabled() -> bool:
    cfg = current_app.config
    return cfg["LLM_PROVIDER"] == "openai" and bool(cfg["LLM_API_KEY"]) and flag("llm_answers")


def chat(messages: list[dict], temperature: float = 0.2, max_tokens: int = 700) -> str:
    cfg = current_app.config
    try:
        response = requests.post(
            cfg["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {cfg['LLM_API_KEY']}", "Content-Type": "application/json"},
            json={"model": cfg["LLM_MODEL"], "messages": messages, "temperature": temperature,
                  "max_tokens": max_tokens},
            timeout=cfg["LLM_TIMEOUT"],
        )
    except requests.RequestException as exc:
        raise LLMError(f"LLM недоступна: {exc.__class__.__name__}") from exc
    if response.status_code != 200:
        raise LLMError(f"LLM вернула {response.status_code}")
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError("Некорректный ответ LLM") from exc
    if not content or not content.strip():
        raise LLMError("Пустой ответ LLM")
    return content.strip()
