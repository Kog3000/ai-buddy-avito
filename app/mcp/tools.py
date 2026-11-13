"""Определения MCP-инструментов. Каждый инструмент — тонкая обёртка над имитацией корпоративного сервиса."""
from __future__ import annotations

from datetime import date, datetime

from ..models import ROLE_INTERN, ROLE_LEAD, ROLE_MENTOR, MentorQuestion, User
from ..seed_data import LEVEL_LABELS
from ..services import absence, access, calendar, checklist, goals, kb, mentor_questions, messenger
from .registry import tool

INTERN = {ROLE_INTERN}
APPROVERS = {ROLE_LEAD, ROLE_MENTOR}
STAFF = {ROLE_MENTOR, ROLE_LEAD}


def _snippet(text: str, size: int = 280) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[:size].rsplit(" ", 1)[0] + "…"


# ------------------------------------------------------------------ профиль и база знаний
@tool("profile_get", "Профиль текущего пользователя: команда, роль, наставник, руководитель, неделя адаптации")
def profile_get(user: User) -> dict:
    return {
        "name": user.full_name, "role": user.role_label, "team": user.team, "position": user.position,
        "program": user.program, "week": user.stage_number() if user.role == ROLE_INTERN else None,
        "mentor": user.mentor.full_name if user.mentor else None,
        "lead": user.lead.full_name if user.lead else None,
        "start_date": user.start_date.isoformat() if user.start_date else None,
    }


@tool("kb_search", "Поиск по базе знаний с учётом прав пользователя",
      {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"])
def kb_search(user: User, query: str, limit: int = 3) -> list[dict]:
    return [{**h.to_dict(), "snippet": _snippet(h.topic.body)} for h in kb.search(user, query, min(limit, 5))]


@tool("kb_get_topic", "Полный текст материала базы знаний", {"slug": {"type": "string"}}, ["slug"])
def kb_get_topic(user: User, slug: str) -> dict:
    t = kb.get_topic(user, slug)
    return {"slug": t.slug, "title": t.title, "body": t.body, "category": t.category.title}


# ------------------------------------------------------------------ чек-лист
@tool("checklist_get", "Чек-лист адаптации стажёра со статусами задач", roles=INTERN)
def checklist_get(user: User) -> dict:
    return checklist.get_checklist(user)


@tool("checklist_next_steps", "Следующие невыполненные шаги адаптации", {"limit": {"type": "integer"}},
      roles=INTERN)
def checklist_next_steps(user: User, limit: int = 3) -> list[dict]:
    return checklist.next_steps(user, limit)


@tool("checklist_set_task", "Отметить задачу чек-листа выполненной или снять отметку",
      {"task_id": {"type": "integer"}, "done": {"type": "boolean"}}, ["task_id", "done"], roles=INTERN,
      mutating=True)
def checklist_set_task(user: User, task_id: int, done: bool) -> dict:
    ut = checklist.set_status(user, task_id, done)
    return {"task_id": task_id, "status": ut.status}


# ------------------------------------------------------------------ доступы
@tool("access_list_resources", "Каталог ресурсов и доступные пользователю уровни прав")
def access_list_resources(user: User) -> list[dict]:
    result = []
    for res in access.resources():
        levels = access.allowed_levels(user, res)
        active = access.active_request(user, res)
        result.append({"code": res.code, "name": res.name, "description": res.description,
                       "levels": levels, "recommended": access.recommended_level(user, res), "base": res.base,
                       "active_request": access.to_dict(active) if active else None})
    return result


@tool("access_create_request", "Создать заявку на доступ в корпоративном сервисе доступов",
      {"resource": {"type": "string"}, "level": {"type": "string", "enum": list(LEVEL_LABELS)},
       "justification": {"type": "string"}, "project": {"type": "string"}},
      ["resource", "level", "justification"], mutating=True)
def access_create_request(user: User, resource: str, level: str, justification: str, project: str = "") -> dict:
    try:
        req = access.create_request(user, resource, level, justification, project)
    except access.DuplicateRequest as dup:
        return {"duplicate": True, "request": access.to_dict(dup.existing)}
    return {"duplicate": False, "request": access.to_dict(req)}


@tool("access_my_requests", "Мои заявки на доступ и их статусы")
def access_my_requests(user: User) -> list[dict]:
    access.sync(user)
    return [access.to_dict(r) for r in access.my_requests(user)]


@tool("access_decide", "Согласовать или отклонить заявку на доступ (для согласующего)",
      {"request_id": {"type": "integer"}, "approve": {"type": "boolean"}, "comment": {"type": "string"}},
      ["request_id", "approve"], roles=APPROVERS, mutating=True)
def access_decide(user: User, request_id: int, approve: bool, comment: str = "") -> dict:
    return access.to_dict(access.decide(user, request_id, approve, comment))


# ------------------------------------------------------------------ отсутствия
@tool("absence_policy", "Правила оформления отсутствия (тестовые правила прототипа)",
      {"kind": {"type": "string", "enum": list(absence.KIND_LABELS)}})
def absence_policy(user: User, kind: str = "day_off") -> dict:
    return {"kind": kind, "text": absence.policy_text(kind), **absence.POLICY[kind]}


@tool("absence_create_request", "Создать заявку на отсутствие в сервисе отсутствий",
      {"kind": {"type": "string", "enum": list(absence.KIND_LABELS)}, "date_from": {"type": "string"},
       "date_to": {"type": "string"}, "comment": {"type": "string"}, "notify_mentor": {"type": "boolean"}},
      ["kind", "date_from", "date_to"], roles=INTERN, mutating=True)
def absence_create_request(user: User, kind: str, date_from: str, date_to: str, comment: str = "",
                           notify_mentor: bool = True) -> dict:
    req = absence.create_request(user, kind, date.fromisoformat(date_from), date.fromisoformat(date_to),
                                 comment, notify_mentor)
    return absence.to_dict(req)


@tool("absence_my_requests", "Мои заявки на отсутствие")
def absence_my_requests(user: User) -> list[dict]:
    return [absence.to_dict(r) for r in absence.my_requests(user)]


@tool("absence_decide", "Согласовать или отклонить отсутствие (для руководителя)",
      {"request_id": {"type": "integer"}, "approve": {"type": "boolean"}, "comment": {"type": "string"}},
      ["request_id", "approve"], roles={ROLE_LEAD}, mutating=True)
def absence_decide(user: User, request_id: int, approve: bool, comment: str = "") -> dict:
    return absence.to_dict(absence.decide(user, request_id, approve, comment))


# ------------------------------------------------------------------ календарь
@tool("calendar_upcoming", "Ближайшие события пользователя (включая встречи, ждущие подтверждения)")
def calendar_upcoming(user: User) -> list[dict]:
    return [calendar.to_dict(m, user) for m in calendar.upcoming(user)]


@tool("calendar_free_slots", "Свободные слоты для встречи стажёра с наставником", {"days": {"type": "integer"}},
      roles=INTERN)
def calendar_free_slots(user: User, days: int = 5) -> list[dict]:
    return [{"start": s.isoformat(), "label": calendar.fmt_dt(s)} for s in calendar.free_slots(user, user.mentor, days)]


@tool("calendar_request_mentor_meeting", "Предложить наставнику встречу (он подтверждает её у себя)",
      {"start": {"type": "string"}, "title": {"type": "string"}, "agenda": {"type": "string"}}, ["start"],
      roles=INTERN, mutating=True)
def calendar_request_mentor_meeting(user: User, start: str, title: str = "Встреча 1:1 с наставником",
                                    agenda: str = "") -> dict:
    return calendar.to_dict(calendar.request_mentor_meeting(user, datetime.fromisoformat(start), title, agenda), user)


@tool("calendar_pending_requests", "Встречи, которые стажёры предложили наставнику", roles={ROLE_MENTOR})
def calendar_pending_requests(user: User) -> list[dict]:
    return [calendar.to_dict(m, user) for m in calendar.pending_for(user)]


@tool("calendar_decide_meeting", "Подтвердить или отклонить встречу, предложенную стажёром",
      {"meeting_id": {"type": "integer"}, "accept": {"type": "boolean"}, "comment": {"type": "string"}},
      ["meeting_id", "accept"], roles={ROLE_MENTOR}, mutating=True)
def calendar_decide_meeting(user: User, meeting_id: int, accept: bool, comment: str = "") -> dict:
    return calendar.to_dict(calendar.decide(user, meeting_id, accept, comment), user)


# ------------------------------------------------------------------ мессенджер
@tool("messenger_list_chats", "Каналы корпоративного мессенджера, подходящие пользователю, и участие в них")
def messenger_list_chats(user: User) -> list[dict]:
    return messenger.status(user)


@tool("messenger_join_chats", "Добавить пользователя в каналы (по умолчанию — во все обязательные)",
      {"codes": {"type": "array"}}, mutating=True)
def messenger_join_chats(user: User, codes: list | None = None) -> dict:
    return {"added": messenger.join(user, codes)}


# ------------------------------------------------------------------ трекер целей
@tool("goals_list", "Цели, задачи и ожидания стажёра в трекере прогресса", roles=INTERN)
def goals_list(user: User) -> list[dict]:
    return [goals.to_dict(g) for g in goals.list_goals(user, user)]


@tool("goals_add", "Добавить цель или ожидание в трекер прогресса",
      {"title": {"type": "string"}, "metric": {"type": "string"},
       "kind": {"type": "string", "enum": list(goals.KIND_LABELS)}}, ["title"], roles=INTERN, mutating=True)
def goals_add(user: User, title: str, metric: str = "", kind: str = "goal") -> dict:
    return goals.to_dict(goals.add_goal(user, title, metric, kind=kind))


# ------------------------------------------------------------------ наставник
@tool("mentor_ask", "Передать вопрос наставнику, если ответа нет в базе знаний",
      {"question": {"type": "string"}, "attachment_id": {"type": "integer"}}, ["question"], roles=INTERN,
      mutating=True)
def mentor_ask(user: User, question: str, attachment_id: int | None = None) -> dict:
    q = mentor_questions.ask(user, question, attachment_id)
    return {"id": q.id, "mentor": user.mentor.full_name, "status": q.status}


@tool("mentor_answer", "Ответить на вопрос стажёра (для наставника)",
      {"question_id": {"type": "integer"}, "answer": {"type": "string"}, "publish_to_faq": {"type": "boolean"}},
      ["question_id", "answer"], roles=STAFF, mutating=True)
def mentor_answer(user: User, question_id: int, answer: str, publish_to_faq: bool = False) -> dict:
    q = mentor_questions.answer(user, question_id, answer, publish_to_faq)
    return {"id": q.id, "status": q.status}


# ------------------------------------------------------------------ сводки для наставника
@tool("mentor_interns_overview", "Сводка по стажёрам наставника: прогресс, следующие шаги, открытые вопросы, встречи",
      roles={ROLE_MENTOR, ROLE_LEAD})
def mentor_interns_overview(user: User) -> list[dict]:
    field = User.mentor_id if user.role == ROLE_MENTOR else User.lead_id
    result = []
    for intern in User.query.filter(field == user.id, User.role == ROLE_INTERN, User.active.is_(True)).all():
        data = checklist.get_checklist(intern)
        stage = data["weeks"][data["current_week"] - 1]
        overdue = [s for s in checklist.next_steps(intern, 28) if s["week"] < data["current_week"]]
        open_q = MentorQuestion.query.filter_by(intern_id=intern.id, status="open").count()
        pending = [m for m in calendar.pending_for(user) if m.organizer_id == intern.id] if user.role == ROLE_MENTOR else []
        result.append({"id": intern.id, "name": intern.full_name, "stage": f"{stage['period']}. {stage['title']}",
                       "done": data["done"], "total": data["total"], "percent": data["percent"],
                       "next": [s["title"] for s in checklist.next_steps(intern, 2)],
                       "overdue": [s["title"] for s in overdue][:3], "open_questions": open_q,
                       "pending_meetings": len(pending)})
    return result


@tool("mentor_intern_context", "Контекст стажёра для подготовки 1:1: недавние вопросы, прогресс, цели",
      {"intern_id": {"type": "integer"}}, ["intern_id"], roles={ROLE_MENTOR, ROLE_LEAD})
def mentor_intern_context(user: User, intern_id: int) -> dict:
    from ..models import ChatMessage, db
    from ..permissions import PermissionDenied, can_view_user
    intern = db.session.get(User, intern_id)
    if intern is None or intern.role != ROLE_INTERN or not can_view_user(user, intern):
        raise PermissionDenied("Нет доступа к данным этого стажёра")
    questions = [m.text for m in ChatMessage.query.filter_by(user_id=intern.id, role="user")
                 .order_by(ChatMessage.id.desc()).limit(30) if m.text.strip().endswith("?")][:5]
    data = checklist.get_checklist(intern)
    return {"name": intern.full_name, "percent": data["percent"], "questions": questions,
            "next": [s["title"] for s in checklist.next_steps(intern, 3)],
            "goals": [g.title for g in goals.list_goals(user, intern)],
            "open_questions": [q.question for q in MentorQuestion.query.filter_by(intern_id=intern.id, status="open")]}
