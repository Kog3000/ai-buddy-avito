"""Чек-лист адаптации: этапы (по сроку от начала стажировки), статусы задач, следующие шаги."""
from __future__ import annotations

from ..analytics import track
from ..models import ChecklistTask, User, UserTask, db, now
from ..seed_data import CHECKLIST

STAGES = {stage: (period, title) for stage, (period, title, _) in CHECKLIST.items()}


def _ensure(user: User) -> dict[int, UserTask]:
    rows = {ut.task_id: ut for ut in UserTask.query.filter_by(user_id=user.id).all()}
    missing = [t for t in ChecklistTask.query.all() if t.id not in rows]
    for task in missing:
        ut = UserTask(user_id=user.id, task_id=task.id, status="todo")
        db.session.add(ut)
        rows[task.id] = ut
    if missing:
        db.session.commit()
    return rows


def get_checklist(user: User) -> dict:
    rows = _ensure(user)
    tasks = ChecklistTask.query.order_by(ChecklistTask.week, ChecklistTask.order).all()
    weeks: list[dict] = []
    for week, (period, title) in STAGES.items():
        items = []
        for task in tasks:
            if task.week != week:
                continue
            ut = rows[task.id]
            items.append({
                "id": task.id, "title": task.title, "description": task.description,
                "status": ut.status, "done": ut.status == "done", "source": ut.source,
                "kb_topic": task.kb_topic_slug, "chat_prompt": task.chat_prompt,
                "auto": bool(task.auto_key),
            })
        done = sum(1 for i in items if i["done"])
        weeks.append({"week": week, "period": period, "title": title, "tasks": items, "done": done,
                      "total": len(items)})
    total = sum(w["total"] for w in weeks)
    done = sum(w["done"] for w in weeks)
    current = user.stage_number()
    return {
        "weeks": weeks, "done": done, "total": total,
        "percent": round(done * 100 / total) if total else 0,
        "current_week": current,
    }


def set_status(user: User, task_id: int, done: bool, source: str = "manual") -> UserTask:
    rows = _ensure(user)
    ut = rows.get(task_id)
    if ut is None:
        raise KeyError(task_id)
    ut.status = "done" if done else "todo"
    ut.completed_at = now() if done else None
    ut.source = source if done else None
    db.session.commit()
    track(user, "checklist_task_done" if done else "checklist_task_undone", task_id=task_id, source=source)
    return ut


def auto_complete(user: User, auto_key: str) -> ChecklistTask | None:
    """Закрывает задачу чек-листа по событию из корпоративного сервиса."""
    task = ChecklistTask.query.filter_by(auto_key=auto_key).first()
    if task is None:
        return None
    rows = _ensure(user)
    if rows[task.id].status == "done":
        return None
    set_status(user, task.id, True, source="auto")
    return task


def next_steps(user: User, limit: int = 3) -> list[dict]:
    data = get_checklist(user)
    result = []
    for week in data["weeks"]:
        for task in week["tasks"]:
            if not task["done"]:
                result.append({**task, "week": week["week"], "period": week["period"], "week_title": week["title"]})
                if len(result) >= limit:
                    return result
    return result
