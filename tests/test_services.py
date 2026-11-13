from datetime import date, datetime, time, timedelta

import pytest

from app import cron
from app.models import AccessRequest, ChatMessage, KBTopic, Notification, db
from app.permissions import PermissionDenied, can_view_user
from app.services import absence, access, calendar, checklist, goals, kb, mentor_questions, notes

from .conftest import next_monday, user


# ------------------------------------------------------------------ база знаний
def test_kb_search_relevance(arina):
    hits = kb.search(arina, "Где хранятся документы нашей команды?")
    assert hits[0].topic.slug == "faq-documents-storage"
    assert kb.search(arina, "развернуть проект локально")[0].topic.slug == "learn-local-setup"
    assert kb.search(arina, "и в на") == []


def test_kb_restricted_topics_hidden_from_intern(arina):
    slugs = {h.topic.slug for h in kb.search(arina, "шкала оценки стажёров экватор наставник", limit=10)}
    assert "mentor-intern-assessment" not in slugs
    with pytest.raises(LookupError):
        kb.get_topic(arina, "mentor-intern-assessment")
    ivan = user("ivan")
    assert kb.get_topic(ivan, "mentor-intern-assessment").title
    with pytest.raises(LookupError):
        kb.get_topic(ivan, "lead-offer-criteria")
    assert kb.get_topic(user("maria"), "lead-offer-criteria")


def test_kb_team_scoped_topics(arina):
    curator = user("elena")  # другая команда (программа стажировок)
    with pytest.raises(LookupError):
        kb.get_topic(curator, "access-stash")
    assert kb.get_topic(arina, "access-stash")


def test_kb_categories_progress(arina):
    cats = kb.categories(arina)
    assert len(cats) == 12 and sum(c["count"] for c in cats) == 84
    assert kb.progress(arina) == {"done": 2, "total": 84, "percent": 2.4}
    kb.mark_done(arina, "team-structure")
    kb.mark_done(arina, "team-structure")  # идемпотентно
    assert kb.progress(arina)["done"] == 3
    kb.mark_done(arina, "team-structure", done=False)
    assert kb.progress(arina)["done"] == 2


def test_kb_add_faq_permissions(arina):
    with pytest.raises(PermissionDenied):
        kb.add_faq(arina, "Вопрос?", "Ответ", None)
    topic = kb.add_faq(user("ivan"), "Где взять пропуск в паркинг", "На ресепшене", arina.team)
    assert topic.source == "faq" and topic.title.endswith("?")
    assert kb.search(arina, "пропуск в паркинг")[0].topic.id == topic.id


# ------------------------------------------------------------------ чек-лист
def test_checklist_status_and_next_steps(arina):
    data = checklist.get_checklist(arina)
    assert (data["done"], data["total"], data["percent"]) == (3, 28, 11)
    assert data["current_week"] == 1 and len(data["weeks"]) == 5
    assert [w["period"] for w in data["weeks"]] == ["Неделя 1", "Неделя 2", "Месяц 1", "Месяцы 2–3", "После 3 месяцев"]
    step = checklist.next_steps(arina, 1)[0]
    assert step["title"] == "Узнать структуру компании и подразделения"
    checklist.set_status(arina, step["id"], True)
    assert checklist.get_checklist(arina)["done"] == 4
    checklist.set_status(arina, step["id"], False)
    assert checklist.get_checklist(arina)["done"] == 3
    with pytest.raises(KeyError):
        checklist.set_status(arina, 9999, True)
    assert checklist.auto_complete(arina, "unknown") is None


def test_stage_number(arina):
    start = arina.start_date
    assert [arina.stage_number(start + timedelta(days=d)) for d in (0, 6, 7, 15, 30, 89, 90, 200)] == \
        [1, 1, 2, 3, 4, 4, 5, 5]
    assert user("ivan").stage_number() == 1


# ------------------------------------------------------------------ доступы
def test_access_policy_for_intern(arina):
    with pytest.raises(PermissionDenied):
        access.validate(arina, "prod-db", "read")
    with pytest.raises(PermissionDenied):
        access.validate(arina, "stash", "admin")
    with pytest.raises(ValueError):
        access.validate(arina, "vpn", "edit")
    with pytest.raises(LookupError):
        access.validate(arina, "nope", "read")
    assert access.allowed_levels(user("maria"), access.get_resource("stash")) == ["read", "edit", "admin"]
    assert access.recommended_level(arina, access.get_resource("prod-db")) is None


def test_access_lifecycle_with_owner_approval(arina):
    maria = user("maria")
    req = access.create_request(arina, "stash", "edit", "Задачи первой недели")
    assert req.status == "pending_approval" and req.approver_id == maria.id
    assert Notification.query.filter_by(user_id=maria.id).count() == 1
    with pytest.raises(access.DuplicateRequest):
        access.create_request(arina, "stash", "read", "Ещё раз")
    with pytest.raises(PermissionDenied):
        access.decide(user("ivan"), req.id, True)
    access.decide(maria, req.id, True, "ок")
    assert req.status == "granted"
    with pytest.raises(ValueError):
        access.decide(maria, req.id, False)
    msg = ChatMessage.query.filter_by(user_id=arina.id).order_by(ChatMessage.id.desc()).first()
    assert "выдан" in msg.text and msg.payload["meta"]["proactive"]
    assert [e["status"] for e in access.timeline(req)] == ["created", "pending_approval", "granted"]


def test_access_rejection_and_cancel(arina):
    maria = user("maria")
    req = access.create_request(arina, "jira", "edit", "Нужна доска")
    access.decide(maria, req.id, False, "Сначала обсудим")
    assert req.status == "rejected"
    req2 = access.create_request(arina, "jira", "read", "Только чтение")
    access.cancel(arina, req2.id)
    assert req2.status == "cancelled"
    with pytest.raises(ValueError):
        access.cancel(arina, req2.id)
    with pytest.raises(LookupError):
        access.cancel(user("dima"), req.id)
    with pytest.raises(LookupError):
        access.decide(maria, 999, True)
    with pytest.raises(ValueError):
        access.create_request(arina, "vpn", "read", "")


def test_access_policy_auto_approve_and_base_checklist(arina):
    maria = user("maria")
    ids = [access.create_request(arina, code, lvl, "Онбординг").id
           for code, lvl in [("vpn", "read"), ("confluence", "read"), ("stash", "edit"), ("jira", "edit")]]
    assert access.sync(arina) == 2  # VPN и Confluence — по политике
    for rid in ids[2:]:
        access.decide(maria, rid, True)
    task = [t for w in checklist.get_checklist(arina)["weeks"] for t in w["tasks"]
            if t["title"].startswith("Настроить доступы")][0]
    assert task["done"] and task["source"] == "auto"
    assert len(access.for_approval(maria)) == 0
    assert access.to_dict(db.session.get(AccessRequest, ids[0]))["status_label"] == "Доступ выдан"


def test_access_owner_approver_and_limits(arina):
    req = access.create_request(arina, "stash", "edit", "Код команды")
    assert req.approver.login == "maria"  # владелец проекта
    assert access.request_card(req)["title"].startswith("Заявка №A-")
    with pytest.raises(PermissionDenied):
        access.validate(arina, "stash", "admin")  # администрирование — только владельцу
    with pytest.raises(PermissionDenied):
        access.validate(arina, "prod-db", "read")
    maria = user("maria")
    assert "admin" in access.allowed_levels(maria, access.get_resource("stash"))


# ------------------------------------------------------------------ отсутствия
def test_absence_validation(arina):
    monday = next_monday(2)
    today = date.today()
    with pytest.raises(ValueError, match="задним числом"):
        absence.validate(arina, "day_off", today - timedelta(days=1), today - timedelta(days=1))
    with pytest.raises(ValueError, match="не позднее"):
        absence.validate(arina, "day_off", today, today)
    with pytest.raises(ValueError, match="не больше"):
        absence.validate(arina, "day_off", monday, monday + timedelta(days=4))
    with pytest.raises(ValueError, match="раньше"):
        absence.validate(arina, "day_off", monday, monday - timedelta(days=1))
    with pytest.raises(ValueError, match="выходные"):
        absence.validate(arina, "day_off", monday - timedelta(days=2), monday - timedelta(days=1))
    with pytest.raises(ValueError, match="14"):
        absence.validate(arina, "vacation", today + timedelta(days=3), today + timedelta(days=4))
    with pytest.raises(ValueError):
        absence.validate(arina, "sick", monday, monday)
    with pytest.raises(PermissionDenied):
        absence.validate(user("maria"), "day_off", monday, monday)  # нет руководителя


def test_absence_lifecycle(arina):
    maria, ivan = user("maria"), user("ivan")
    monday = next_monday(2)
    req = absence.create_request(arina, "day_off", monday, monday, "дела")
    assert Notification.query.filter_by(user_id=maria.id).count() == 1
    assert Notification.query.filter_by(user_id=ivan.id, dedup_key=f"absence-info:{req.id}").count() == 1
    with pytest.raises(ValueError, match="уже есть"):
        absence.create_request(arina, "day_off", monday, monday)
    with pytest.raises(PermissionDenied):
        absence.decide(ivan, req.id, True)
    absence.decide(maria, req.id, True, "Хорошего отдыха")
    assert req.status == "approved"
    assert absence.to_dict(req)["timeline"][-1]["status"] == "approved"
    # день отсутствия исключается из слотов встреч
    slots = calendar.free_slots(arina, ivan, days=14, limit=10)
    assert all(s.date() != monday for s in slots)
    with pytest.raises(ValueError):
        absence.cancel(arina, req.id)
    req2 = absence.create_request(arina, "remote", monday + timedelta(days=1), monday + timedelta(days=1))
    absence.cancel(arina, req2.id)
    assert req2.status == "cancelled"
    req3 = absence.create_request(arina, "remote", monday + timedelta(days=2), monday + timedelta(days=2))
    absence.decide(maria, req3.id, False)
    assert req3.status == "rejected" and absence.for_approval(maria) == []
    with pytest.raises(LookupError):
        absence.decide(maria, 999, True)
    with pytest.raises(LookupError):
        absence.cancel(user("dima"), req3.id)
    assert "тестовое правило" in absence.policy_text("remote")


# ------------------------------------------------------------------ календарь
def test_calendar_booking(arina):
    ivan = user("ivan")
    monday = next_monday(2)
    start = datetime.combine(monday, time(14))
    meeting = calendar.book_mentor_meeting(arina, start, "1:1", "- вопросы")
    assert meeting.attendee_id == ivan.id
    with pytest.raises(ValueError, match="занято"):
        calendar.book_mentor_meeting(arina, start)
    with pytest.raises(ValueError, match="прошлом"):
        calendar.book_mentor_meeting(arina, datetime.now() - timedelta(hours=1))
    with pytest.raises(ValueError, match="рабочее"):
        calendar.book_mentor_meeting(arina, datetime.combine(monday, time(8)))
    with pytest.raises(PermissionDenied):
        calendar.book_mentor_meeting(ivan, start + timedelta(hours=1))
    assert any(m.id == meeting.id for m in calendar.upcoming(arina))
    assert calendar.to_dict(meeting)["when"].endswith("14:00 - 15:00")
    assert calendar.relative_label(datetime.combine(date.today(), time(9))).startswith("Сегодня")
    assert calendar.relative_label(datetime.combine(date.today() + timedelta(days=1), time(9))).startswith("Завтра")


def test_free_slots_are_free_and_in_future(arina):
    ivan = user("ivan")
    slots = calendar.free_slots(arina, ivan)
    assert 1 <= len(slots) <= 4
    assert all(s > datetime.now() and s.weekday() < 5 for s in slots)


# ------------------------------------------------------------------ цели, заметки, вопросы
def test_goals(arina):
    maria = user("maria")
    with pytest.raises(ValueError):
        goals.add_goal(arina, "")
    with pytest.raises(ValueError):
        goals.add_goal(arina, "Цель", kind="dream")
    g = goals.add_goal(arina, "Закрыть 2 задачи", "2 MR")
    assert Notification.query.filter_by(user_id=maria.id).count() == 1
    with pytest.raises(PermissionDenied):
        goals.list_goals(user("dima"), arina)
    assert len(goals.list_goals(maria, arina)) == 1
    with pytest.raises(PermissionDenied):
        goals.agree(user("ivan"), g.id)
    assert goals.agree(maria, g.id).agreed_by_lead
    assert goals.set_status(arina, g.id, True).status == "done"
    assert goals.to_dict(g)["kind_label"] == "Цель"
    with pytest.raises(LookupError):
        goals.set_status(user("dima"), g.id, True)
    with pytest.raises(LookupError):
        goals.agree(maria, 999)
    goals.delete(arina, g.id)
    with pytest.raises(LookupError):
        goals.delete(arina, g.id)


def test_notes(arina):
    n = notes.save(arina, "Встреча", "Итоги")
    notes.save(arina, "Встреча 2", "Новое", n.id)
    assert notes.to_dict(n)["title"] == "Встреча 2"
    with pytest.raises(ValueError):
        notes.save(arina, "", "")
    with pytest.raises(LookupError):
        notes.save(user("dima"), "x", "y", n.id)
    notes.delete(arina, n.id)
    with pytest.raises(LookupError):
        notes.delete(arina, n.id)


def test_mentor_questions(arina):
    ivan = user("ivan")
    with pytest.raises(ValueError):
        mentor_questions.ask(arina, "?")
    with pytest.raises(PermissionDenied):
        mentor_questions.ask(ivan, "Вопрос без наставника")
    q = mentor_questions.ask(arina, "Как получить пропуск на парковку?")
    assert q in mentor_questions.open_for(ivan)
    with pytest.raises(PermissionDenied):
        mentor_questions.answer(user("maria"), q.id, "ответ")
    with pytest.raises(ValueError):
        mentor_questions.answer(ivan, q.id, "  ")
    mentor_questions.answer(ivan, q.id, "На ресепшене, 2 этаж", publish_to_faq=True)
    with pytest.raises(ValueError):
        mentor_questions.answer(ivan, q.id, "ещё")
    with pytest.raises(LookupError):
        mentor_questions.answer(ivan, 999, "x")
    assert mentor_questions.answered_for(ivan)[0].id == q.id
    assert KBTopic.query.filter_by(source="faq").count() == 1
    assert mentor_questions.of_intern(arina)[0].status == "answered"


def test_permissions_view_user(arina):
    assert can_view_user(arina, arina)
    assert can_view_user(user("ivan"), arina) and can_view_user(user("maria"), arina)
    assert not can_view_user(user("dima"), arina)


# ------------------------------------------------------------------ ИИ-крон
def test_cron_nudges(arina):
    sent = cron.daily_nudges()
    assert sent == 2  # двое стажёров
    assert cron.daily_nudges() == 0  # дедупликация в течение дня
    msg = ChatMessage.query.filter_by(user_id=arina.id).order_by(ChatMessage.id.desc()).first()
    assert msg.payload["meta"]["nudge"]
    later = arina.start_date + timedelta(days=9)
    assert cron.daily_nudges(later) == 2
    assert "прошлого этапа" in ChatMessage.query.filter_by(user_id=arina.id).order_by(ChatMessage.id.desc()).first().text
    assert cron.run_all()["nudges"] == 0


def test_cron_respects_flag(app):
    from app.analytics import set_flag
    set_flag("proactive_nudges", False)
    assert cron.daily_nudges() == 0
    with pytest.raises(KeyError):
        set_flag("unknown", True)


def test_intern_gets_same_access_as_team(arina):
    ivan = user("ivan")
    for code in ("stash", "jira", "confluence", "grafana", "redash"):
        res = access.get_resource(code)
        assert access.allowed_levels(arina, res) == access.allowed_levels(ivan, res)
    with pytest.raises(PermissionDenied, match="дежурных SRE"):
        access.validate(ivan, "prod-db", "read")  # ограничение по ресурсу, а не по статусу стажёра
    req = access.create_request(arina, "strategy", "read", "Задача FE-123: экран планов запусков")
    assert req.approver.login == "maria"
