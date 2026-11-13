"""Уведомления: in-app для всех ролей (колокольчик) и проактивные сообщения AI-buddy в чат стажёра."""
from __future__ import annotations

from ..models import ChatMessage, Notification, User, db


def notify(user: User, text: str, link: str = "", dedup_key: str | None = None) -> Notification | None:
    if dedup_key and Notification.query.filter_by(user_id=user.id, dedup_key=dedup_key).first():
        return None
    item = Notification(user_id=user.id, text=text, link=link, dedup_key=dedup_key)
    db.session.add(item)
    db.session.commit()
    return item


def _visible():
    return db.or_(Notification.dedup_key.is_(None), ~Notification.dedup_key.like("nudge:%"))


def recent(user: User, limit: int = 10) -> list[Notification]:
    return (Notification.query.filter_by(user_id=user.id).filter(_visible())
            .order_by(Notification.read, Notification.created_at.desc(), Notification.id.desc()).limit(limit).all())


def resolve(key: str) -> None:
    """Событие обработано (ответ дан, заявка согласована) — уведомления о нём больше не висят непрочитанными."""
    Notification.query.filter(db.or_(Notification.dedup_key == key, Notification.dedup_key.like(f"{key}:%"))) \
        .update({"read": True}, synchronize_session=False)
    db.session.commit()


def unread(user: User) -> list[Notification]:
    return (Notification.query.filter_by(user_id=user.id, read=False).filter(_visible())
            .order_by(Notification.created_at.desc()).all())


def mark_all_read(user: User) -> None:
    Notification.query.filter_by(user_id=user.id, read=False).update({"read": True})
    db.session.commit()


def post_to_chat(user: User, text: str, cards: list | None = None, suggestions: list | None = None,
                 meta: dict | None = None) -> ChatMessage:
    """Проактивное сообщение AI-buddy (например, смена статуса заявки)."""
    msg = ChatMessage(user_id=user.id, role="assistant", text=text,
                      payload={"cards": cards or [], "suggestions": suggestions or [],
                               "sources": [], "meta": {"proactive": True, **(meta or {})}})
    db.session.add(msg)
    db.session.commit()
    return msg
