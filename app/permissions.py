"""Ролевая модель и проверки прав.

Все проверки выполняются на бэкенде до того, как данные попадут в ответ API или в контекст LLM.
"""
from __future__ import annotations

from .models import ROLE_ADMIN, ROLE_INTERN, ROLE_LEAD, ROLE_MENTOR, KBTopic, User

STAFF_ROLES = {ROLE_MENTOR, ROLE_LEAD}
ALL_ROLES = {ROLE_INTERN, ROLE_MENTOR, ROLE_LEAD, ROLE_ADMIN}


class PermissionDenied(Exception):
    """Операция недоступна пользователю."""


def topic_visible(user: User, topic: KBTopic) -> bool:
    if topic.audience == "mentor" and user.role not in STAFF_ROLES:
        return False
    if topic.audience == "lead" and user.role != ROLE_LEAD:
        return False
    if topic.team and topic.team != user.team:
        return False
    return True


def can_view_user(viewer: User, target: User) -> bool:
    """Свои данные видит каждый; данные стажёра — его наставник, руководитель и куратор стажировки."""
    if viewer.id == target.id or viewer.role == ROLE_ADMIN:
        return True
    return target.mentor_id == viewer.id or target.lead_id == viewer.id


def require(condition: bool, message: str = "Недостаточно прав") -> None:
    if not condition:
        raise PermissionDenied(message)
