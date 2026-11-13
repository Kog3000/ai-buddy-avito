"""Имитация корпоративного мессенджера: каналы и участие в них."""
from __future__ import annotations

from ..analytics import track
from ..models import ChatMembership, ChatRoom, User, db
from . import checklist


def rooms_for(user: User) -> list[ChatRoom]:
    """Каналы, которые подходят пользователю: общие, своей команды и (для стажёров) канал потока."""
    result = []
    for room in ChatRoom.query.order_by(ChatRoom.id).all():
        if room.scope == "team" and room.team != user.team:
            continue
        if room.scope == "program" and user.role not in {"intern", "admin"}:
            continue
        result.append(room)
    return result


def status(user: User) -> list[dict]:
    joined = {m.room_id for m in ChatMembership.query.filter_by(user_id=user.id)}
    return [{"code": r.code, "name": r.name, "description": r.description, "required": r.required,
             "joined": r.id in joined} for r in rooms_for(user)]


def missing_required(user: User) -> list[dict]:
    return [r for r in status(user) if r["required"] and not r["joined"]]


def join(user: User, codes: list[str] | None = None) -> list[str]:
    """Добавляет пользователя в каналы (по умолчанию — во все обязательные, где его ещё нет)."""
    available = {r.code: r for r in rooms_for(user)}
    if codes is None:
        codes = [r["code"] for r in missing_required(user)]
    unknown = [c for c in codes if c not in available]
    if unknown:
        raise LookupError(", ".join(unknown))
    joined = {m.room_id for m in ChatMembership.query.filter_by(user_id=user.id)}
    added = []
    for code in codes:
        room = available[code]
        if room.id not in joined:
            db.session.add(ChatMembership(user_id=user.id, room_id=room.id))
            added.append(room.name)
    db.session.commit()
    if added:
        track(user, "chats_joined", count=len(added))
    if user.role == "intern" and not missing_required(user):
        checklist.auto_complete(user, "chats_joined")
    return added
