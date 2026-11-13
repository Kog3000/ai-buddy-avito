"""Метрики пользы AI-buddy (считаются из аналитики событий).

Оценка сэкономленного времени наставника — допущение прототипа: ~5 минут на вопрос, закрытый без наставника,
~10 минут на оргдействие (заявка/встреча), выполненное через AI-buddy.
"""
from __future__ import annotations

from statistics import median

from datetime import date

from .agent.nlu import CATEGORIES, categorize
from .models import AccessRequest, AnalyticsEvent, ChatMessage, MentorQuestion, User
from .services import checklist

# Текущие показатели онбординга стажёров (данные Авито, приблизительно) и целевые значения — гипотезы прототипа
BASELINE = [
    {"name": "Время до первой самостоятельной задачи", "now": "~2 недели", "target": "≤ 1,5 недели",
     "lever": "доступы и материалы в первые дни, понятные следующие шаги"},
    {"name": "Длительность адаптации", "now": "~2 месяца", "target": "~6 недель",
     "lever": "чек-лист с автозакрытием задач и напоминаниями"},
    {"name": "Нагрузка на наставника в первые 2–3 месяца", "now": "~5 ч/нед", "target": "≤ 3,5 ч/нед",
     "lever": "оргвопросы и повторяющиеся объяснения закрывает AI-buddy"},
    {"name": "Удовлетворённость чек-листом", "now": "68%", "target": "≥ 80%",
     "lever": "интерактивный чек-лист вместо таблицы"},
    {"name": "Удовлетворённость онбордингом", "now": "9/10", "target": "не ниже 9/10", "lever": "guardrail"},
    {"name": "Удовлетворённость наставником", "now": "90%", "target": "не ниже 90%",
     "lever": "guardrail: AI-buddy не заменяет 1:1"},
]
MENTOR_HOURS_BASELINE = 5.0  # ч/нед на стажёра в первые 2–3 месяца

MIN_PER_ANSWER = 5
MIN_PER_ACTION = 10
ORG_ACTIONS = {"access_confirm", "access_confirm_bundle", "absence_confirm", "meeting_confirm", "goal_confirm"}


def _events(name: str, user_ids: list[int]) -> list[AnalyticsEvent]:
    return AnalyticsEvent.query.filter(AnalyticsEvent.name == name, AnalyticsEvent.user_id.in_(user_ids)).all()


def interns_of(viewer: User) -> list[User]:
    if viewer.role == "lead":
        return User.query.filter_by(role="intern", lead_id=viewer.id).all()
    if viewer.role == "mentor":
        return User.query.filter_by(role="intern", mentor_id=viewer.id).all()
    if viewer.role == "admin":
        return User.query.filter_by(role="intern").all()
    return []


def compute(viewer: User) -> dict:
    interns = interns_of(viewer)
    ids = [u.id for u in interns] or [-1]
    messages = _events("chat_message", ids)
    kb_answers = [e for e in messages if (e.props or {}).get("source") in {"kb", "llm"}]
    not_found = [e for e in messages if (e.props or {}).get("source") == "not_found"]
    actions = [e for e in _events("chat_action", ids)
               if (e.props or {}).get("action", "") in ORG_ACTIONS and (e.props or {}).get("ok")]
    escalated = MentorQuestion.query.filter(MentorQuestion.intern_id.in_(ids)).all()
    answered = [q for q in escalated if q.status == "answered"]
    feedback = ChatMessage.query.filter(ChatMessage.user_id.in_(ids), ChatMessage.feedback.isnot(None)).all()
    positive = sum(1 for m in feedback if m.feedback > 0)
    granted = AccessRequest.query.filter(AccessRequest.user_id.in_(ids), AccessRequest.status == "granted").all()
    access_minutes = [(r.updated_at - r.created_at).total_seconds() / 60 for r in granted]
    saved_min = len(kb_answers) * MIN_PER_ANSWER + len(actions) * MIN_PER_ACTION
    total_q = len(kb_answers) + len(not_found)
    by_cat = {key: {"label": label, "bot": 0, "mentor": 0} for key, label in CATEGORIES.items()}
    for e in messages:
        props = e.props or {}
        if props.get("intent") in {"greeting", "thanks", "cancel"}:
            continue
        by_cat[props.get("category") or "other"]["bot"] += 1
    for q in escalated:
        by_cat[categorize(q.question)]["mentor"] += 1
    weeks = sum(max(1.0, (date.today() - u.start_date).days / 7) for u in interns if u.start_date) or 1.0
    return {
        "interns": [{"id": u.id, "name": u.full_name, "week": u.stage_number(),
                     "progress": checklist.get_checklist(u)["percent"]} for u in interns],
        "questions_total": total_q,
        "answered_by_bot": len(kb_answers),
        "deflection_rate": round(len(kb_answers) * 100 / total_q) if total_q else 0,
        "escalated": len(escalated),
        "escalated_answered": len(answered),
        "actions_done": len(actions),
        "csat": round(positive * 100 / len(feedback)) if feedback else None,
        "feedback_count": len(feedback),
        "median_access_minutes": round(median(access_minutes)) if access_minutes else None,
        "mentor_minutes_saved": saved_min,
        "categories": [{"key": k, **v} for k, v in by_cat.items() if v["bot"] or v["mentor"]],
        "baseline": BASELINE,
        "mentor_hours_baseline": MENTOR_HOURS_BASELINE,
        "mentor_hours_saved_per_week": round(saved_min / 60 / weeks, 2),
    }
