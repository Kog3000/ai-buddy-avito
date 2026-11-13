"""Задачи для внутреннего ИИ-крона: синхронизация статусов заявок и проактивные напоминания."""
from __future__ import annotations

from datetime import date

from .analytics import flag, track
from .models import Notification, User
from .services import access, checklist
from .services.notify import post_to_chat


def daily_nudges(today: date | None = None) -> int:
    """Раз в день напоминает стажёру о следующем шаге, если на прошлых этапах остались хвосты."""
    if not flag("proactive_nudges"):
        return 0
    today = today or date.today()
    sent = 0
    for user in User.query.filter_by(role="intern").all():
        key = f"nudge:{user.id}:{today.isoformat()}"
        if Notification.query.filter_by(user_id=user.id, dedup_key=key).first():
            continue
        steps = checklist.next_steps(user, limit=1)
        if not steps:
            continue
        step = steps[0]
        overdue = step["week"] < user.stage_number(today)
        prefix = "Есть задача с прошлого этапа" if overdue else "Следующий шаг адаптации"
        suggestions = [step["chat_prompt"]] if step["chat_prompt"] else ["Что дальше?"]
        post_to_chat(user, f"{prefix}: «{step['title']}». Помочь?", suggestions=suggestions,
                     meta={"nudge": True})
        from .models import db
        db.session.add(Notification(user_id=user.id, text=step["title"], dedup_key=key, read=True))
        db.session.commit()
        track(user, "nudge_sent", overdue=overdue)
        sent += 1
    return sent


def run_all() -> dict:
    return {"access_synced": access.sync(), "nudges": daily_nudges()}
