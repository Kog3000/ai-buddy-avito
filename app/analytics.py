"""Аналитика событий (имитация clickstream) и feature flags."""
from __future__ import annotations

from .models import AnalyticsEvent, FeatureFlag, User, db

DEFAULT_FLAGS = {
    "absence_requests": (True, "Оформление отгулов через сервис отсутствий"),
    "mentor_escalation": (True, "Передача вопросов наставнику, если ответа нет в базе знаний"),
    "llm_answers": (True, "Ответы с помощью LLM (если настроен ключ)"),
    "proactive_nudges": (True, "Проактивные напоминания AI-buddy (ИИ-крон)"),
}


def track(user: User | None, name: str, **props) -> None:
    db.session.add(AnalyticsEvent(user_id=user.id if user else None, name=name, props=props))
    db.session.commit()


def flag(key: str) -> bool:
    row = db.session.get(FeatureFlag, key)
    if row is None:
        return DEFAULT_FLAGS.get(key, (False, ""))[0]
    return bool(row.enabled)


def set_flag(key: str, enabled: bool) -> None:
    if key not in DEFAULT_FLAGS:
        raise KeyError(key)
    row = db.session.get(FeatureFlag, key)
    if row is None:
        row = FeatureFlag(key=key, description=DEFAULT_FLAGS[key][1])
        db.session.add(row)
    row.enabled = enabled
    db.session.commit()


def all_flags() -> list[dict]:
    return [
        {"key": key, "enabled": flag(key), "description": desc}
        for key, (_, desc) in DEFAULT_FLAGS.items()
    ]


def ensure_flags() -> None:
    for key, (enabled, desc) in DEFAULT_FLAGS.items():
        if db.session.get(FeatureFlag, key) is None:
            db.session.add(FeatureFlag(key=key, enabled=enabled, description=desc))
    db.session.commit()
