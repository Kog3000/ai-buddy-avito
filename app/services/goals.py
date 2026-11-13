"""Трекер прогресса стажёра: цели, задачи и ожидания на период адаптации."""
from __future__ import annotations

from datetime import date

from ..analytics import track
from ..models import Goal, User, db
from ..permissions import PermissionDenied, can_view_user
from . import checklist
from .notify import notify, resolve

KIND_LABELS = {"goal": "Цель", "expectation": "Ожидание"}


def list_goals(viewer: User, owner: User) -> list[Goal]:
    if not can_view_user(viewer, owner):
        raise PermissionDenied("Нет доступа к трекеру этого сотрудника")
    return Goal.query.filter_by(user_id=owner.id).order_by(Goal.id).all()


def add_goal(user: User, title: str, metric: str = "", due: date | None = None, kind: str = "goal") -> Goal:
    title = (title or "").strip()
    if len(title) < 3:
        raise ValueError("Сформулируй цель (минимум 3 символа)")
    if kind not in KIND_LABELS:
        raise ValueError("Неизвестный тип записи")
    goal = Goal(user_id=user.id, title=title, metric=(metric or "").strip(), due=due, kind=kind)
    db.session.add(goal)
    db.session.commit()
    checklist.auto_complete(user, "goal_added")
    if user.lead:
        notify(user.lead, f"{user.full_name} добавил(а) в трекер: «{title}» — нужно согласовать", "/team",
               dedup_key=f"goal:{goal.id}")
    track(user, "goal_added", kind=kind)
    return goal


def set_status(user: User, goal_id: int, done: bool) -> Goal:
    goal = db.session.get(Goal, goal_id)
    if goal is None or goal.user_id != user.id:
        raise LookupError(goal_id)
    goal.status = "done" if done else "active"
    db.session.commit()
    return goal


def agree(lead: User, goal_id: int) -> Goal:
    goal = db.session.get(Goal, goal_id)
    if goal is None:
        raise LookupError(goal_id)
    owner = db.session.get(User, goal.user_id)
    if owner.lead_id != lead.id:
        raise PermissionDenied("Согласовать цель может только руководитель стажёра")
    goal.agreed_by_lead = True
    db.session.commit()
    resolve(f"goal:{goal.id}")
    notify(owner, f"{lead.full_name} согласовал(а) цель «{goal.title}»", "/progress", dedup_key=f"goal-agreed:{goal.id}")
    return goal


def delete(user: User, goal_id: int) -> None:
    goal = db.session.get(Goal, goal_id)
    if goal is None or goal.user_id != user.id:
        raise LookupError(goal_id)
    db.session.delete(goal)
    db.session.commit()


def to_dict(goal: Goal) -> dict:
    return {"id": goal.id, "title": goal.title, "metric": goal.metric, "kind": goal.kind,
            "kind_label": KIND_LABELS[goal.kind], "due": goal.due.strftime("%d.%m.%Y") if goal.due else "",
            "status": goal.status, "agreed": goal.agreed_by_lead}
