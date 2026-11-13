"""Куратор и кабинеты, встречи с подтверждением, каналы, вложения, уведомления, поиск по темам, профиль."""
import io
from datetime import date, datetime, timedelta

import pytest

from app.models import Meeting, Notification, db
from app.permissions import PermissionDenied
from app.services import calendar, kb, messenger, notify, users

from .conftest import user


# ------------------------------------------------------------------ куратор стажировки
def test_admin_creates_intern_and_assigns(app):
    elena, ivan, maria = user("elena"), user("ivan"), user("maria")
    start = date.today() + timedelta(days=3)
    new, password = users.create_user(elena, "Пётр Смирнов", "Petr.Smirnov@avito.ru", "intern",
                                      position="Стажёр-аналитик", start_date=start, mentor_id=ivan.id,
                                      lead_id=maria.id)
    assert new.login == "petr.smirnov" and len(password) >= 8
    assert new.team == ivan.team and new.mentor_id == ivan.id
    assert not messenger.missing_required(new)  # сразу добавлен в обязательные каналы
    assert Notification.query.filter_by(user_id=ivan.id, dedup_key=f"assign:{new.id}:{ivan.id}").first()
    same, _ = users.create_user(elena, "Пётр Другой", "petr.smirnov@avito.com", "mentor", team="Команда X")
    assert same.login == "petr.smirnov2" and same.team == "Команда X"
    assert users.to_dict(new)["mentor"] == ivan.full_name
    # переназначение наставника уведомляет стажёра
    users.assign(elena, new.id, same.id, maria.id)
    assert any("Твой наставник" in n.text for n in Notification.query.filter_by(user_id=new.id))
    assert users.list_users(elena)[0].role == "admin"


@pytest.mark.parametrize("kwargs,error", [
    ({"full_name": "Одно"}, "имя и фамилию"),
    ({"email": "bad"}, "e-mail"),
    ({"email": "example@mail.ru"}, "уже есть"),
    ({"role": "admin"}, "роль"),
    ({"mentor_id": None}, "наставника"),
    ({"start_date": None}, "дату начала"),
    ({"end_date": date(2000, 1, 1)}, "раньше"),
])
def test_admin_create_validation(app, kwargs, error):
    base = {"full_name": "Анна Новая", "email": "anna@avito.ru", "role": "intern", "start_date": date.today(),
            "mentor_id": user("ivan").id, "lead_id": user("maria").id}
    base.update(kwargs)
    with pytest.raises(ValueError, match=error):
        users.create_user(user("elena"), **base)


def test_admin_only_and_account_management(app):
    elena, arina = user("elena"), user("arina")
    with pytest.raises(PermissionDenied):
        users.list_users(user("ivan"))
    with pytest.raises(ValueError, match="Наставник"):
        users.assign(elena, arina.id, user("maria").id, user("maria").id)
    with pytest.raises(LookupError):
        users.assign(elena, user("ivan").id, user("ivan").id, user("maria").id)
    with pytest.raises(PermissionDenied):
        users.set_active(elena, elena.id, False)
    with pytest.raises(LookupError):
        users.reset_password(elena, 999)
    with pytest.raises(LookupError):
        users.set_active(elena, 999, True)
    assert users.set_active(elena, arina.id, False).active is False


def test_admin_api_and_login_with_temp_password(login, client):
    login("elena")
    page = client.get("/admin/users").get_data(as_text=True)
    assert "Создать кабинет" in page and "Арина Язева" in page
    assert len(client.get("/api/admin/users").get_json()["users"]) == 5
    ivan, maria = user("ivan"), user("maria")
    res = client.post("/api/admin/users", json={
        "full_name": "Ольга Новикова", "email": "olga@avito.ru", "role": "intern", "start_date": date.today().isoformat(),
        "mentor_id": str(ivan.id), "lead_id": str(maria.id), "join_chats": False})
    assert res.status_code == 201
    created, password = res.get_json()["user"], res.get_json()["password"]
    assert client.post(f"/api/admin/users/{created['id']}/assign",
                       json={"mentor_id": ivan.id, "lead_id": maria.id}).status_code == 200
    new_pass = client.post(f"/api/admin/users/{created['id']}/password").get_json()["password"]
    assert new_pass != password
    assert client.post(f"/api/admin/users/{created['id']}/active", json={"active": False}).get_json()["active"] is False
    client.post("/logout")
    resp = client.post("/login", data={"login": "olga", "password": new_pass})
    assert resp.status_code == 200 and "отключ" in resp.get_data(as_text=True).lower()
    login("elena")
    client.post(f"/api/admin/users/{created['id']}/active", json={"active": True})
    client.post("/logout")
    assert client.post("/login", data={"login": "olga@avito.ru", "password": new_pass}).location == "/home"
    assert client.get("/api/admin/users").status_code == 403


# ------------------------------------------------------------------ встречи
def _first_slot(intern, mentor):
    return calendar.free_slots(intern, mentor, limit=1)[0]


def test_meeting_request_decide_cancel(app):
    arina, ivan = user("arina"), user("ivan")
    slot = _first_slot(arina, ivan)
    m = calendar.request_mentor_meeting(arina, slot, "1:1", "вопросы")
    assert m.status == "pending"
    assert calendar.to_dict(m, ivan)["label"] == "Встреча с Арина Язева"
    assert calendar.to_dict(m, arina)["label"] == "Встреча с наставником"
    assert m in calendar.pending_for(ivan)
    with pytest.raises(ValueError):  # слот уже занят приглашением
        calendar.request_mentor_meeting(arina, slot)
    with pytest.raises(PermissionDenied):
        calendar.decide(user("maria"), m.id, True)
    calendar.decide(ivan, m.id, False, "Давай в другой день")
    assert m.status == "declined" and m.decision_comment == "Давай в другой день"
    assert Notification.query.filter_by(user_id=arina.id).filter(Notification.text.like("%не может%")).first()
    with pytest.raises(ValueError):
        calendar.decide(ivan, m.id, True)
    m2 = calendar.request_mentor_meeting(arina, slot)  # слот снова свободен
    with pytest.raises(LookupError):  # чужое приглашение не раскрываем
        calendar.cancel(user("dima"), m2.id)
    assert calendar.cancel(arina, m2.id).status == "declined"
    with pytest.raises(LookupError):
        calendar.decide(ivan, 9999, True)


def test_availability_calendar(app):
    arina, ivan = user("arina"), user("ivan")
    days = calendar.availability(arina, ivan, days=14)
    assert len(days) == 14
    weekend = [d for d in days if d["weekday"] >= 5]
    assert weekend and all(not d["slots"] for d in weekend)
    assert any(d["slots"] for d in days)
    assert calendar.relative_label(datetime.now()).startswith("Сегодня")


def test_meeting_pages_and_api(login, client):
    login("arina")
    days = client.get("/api/meetings/availability?days=10").get_json()["days"]
    start = next(d for d in days if d["slots"])["slots"][0]["start"]
    mid = client.post("/api/meetings", json={"start": start, "title": "Разбор задачи"}).get_json()["id"]
    assert "Ждёт подтверждения" in client.get("/mentor").get_data(as_text=True)
    login("ivan")
    page = client.get("/meetings").get_data(as_text=True)
    assert "Встреча с Арина Язева" in page and "Разбор задачи" in page
    assert client.post(f"/api/meetings/{mid}/decide", json={"accept": True}).get_json()["status"] == "accepted"
    assert "Встреча с Арина Язева" in client.get("/profile").get_data(as_text=True)
    login("arina")
    assert "Встреча с наставником" in client.get("/profile").get_data(as_text=True)
    assert client.post(f"/api/meetings/{mid}/cancel").status_code == 400  # подтверждённую отменяют в календаре
    login("maria")
    assert client.get("/meetings").status_code == 403


def test_mentor_assistant_chat(login, chat, client):
    login("ivan")
    hello = chat("Привет")
    assert "Сводка по стажёрам" in hello["suggestions"]
    assert "Дмитрий Ким" in chat("Сводка по стажёрам")["text"]
    agenda = chat("Подготовь повестку 1:1 с Дмитрий Ким")["text"]
    assert "Повестка 1:1" in agenda and "Следующие шаги" in agenda
    assert "С кем" in chat("Подготовь повестку встречи")["text"]
    pending = chat("Встречи на подтверждении")
    assert pending["cards"] and pending["cards"][0]["actions"][0]["action"] == "meeting_decide"
    params = pending["cards"][0]["actions"][0]["params"]
    done = chat(action="meeting_decide", params=params)
    assert done["text"].startswith("Подтвердил")
    assert "нет" in chat("Встречи на подтверждении")["text"]
    login("dima")  # в демо-данных приглашение на подтверждении — от Дмитрия
    assert any("подтвердил" in n["text"] for n in client.get("/api/me").get_json()["notifications"])


def test_lead_assistant_fallbacks(login, chat):
    login("maria")
    assert "Сводка" in chat("Сводка по стажёрам")["text"]
    assert chat("Встречи на подтверждении")["text"]


# ------------------------------------------------------------------ каналы
def test_chats_service_and_flow(login, chat, client):
    arina = user("arina")
    missing = messenger.missing_required(arina)
    assert len(missing) >= 3
    with pytest.raises(LookupError):
        messenger.join(arina, ["nope"])
    login("arina")
    msg = chat("Добавь меня во все нужные чаты")
    card = msg["cards"][0]
    assert card["actions"][0]["action"] == "chats_confirm"
    done = chat(action="chats_confirm", params=card["actions"][0]["params"])
    assert done["text"].startswith("Добавил в каналы")
    assert "уже во всех" in chat("Добавь меня в каналы команды")["text"]
    tasks = client.get("/api/checklist").get_json()["weeks"][0]["tasks"]
    assert next(t for t in tasks if "чаты" in t["title"])["done"]
    assert len(client.post("/api/chats/join", json={"codes": ["clubs"]}).get_json()["added"]) == 1
    assert all(c["joined"] for c in client.get("/api/chats").get_json()["chats"] if c["required"])


def test_chats_page_join_all(login, client):
    login("arina")
    assert "Добавить меня во все обязательные" in client.get("/access?tab=chats").get_data(as_text=True)
    added = client.post("/api/chats/join", json={}).get_json()["added"]
    assert added
    assert "Добавить меня во все обязательные" not in client.get("/access?tab=chats").get_data(as_text=True)


# ------------------------------------------------------------------ вложения
def test_chat_attachment_upload_and_access(login, client):
    login("arina")
    data = {"text": "Не собирается проект, вот лог", "file": (io.BytesIO(b"error: module not found"), "build.log")}
    resp = client.post("/api/chat/send", data=data, content_type="multipart/form-data")
    assert resp.status_code == 202
    att = resp.get_json()["user_message"]["attachments"][0]
    assert att["name"] == "build.log"
    job = client.get(f"/api/chat/jobs/{resp.get_json()['job_id']}").get_json()
    card = next(c for c in job["message"]["cards"] if c["actions"][0]["action"] == "escalate_confirm")
    assert card["actions"][0]["params"]["attachment_id"] == att["id"]
    got = client.get(att["url"])
    assert got.data == b"error: module not found" and "attachment" in got.headers["Content-Disposition"]
    # передаём вопрос с файлом наставнику
    params = {**card["actions"][0]["params"], "question": "Не собирается проект"}
    client.post("/api/chat/send", json={"action": "escalate_confirm", "params": params, "label": "x"})
    login("ivan")
    page = client.get("/team/questions").get_data(as_text=True)
    assert "build.log" in page
    assert client.get(att["url"]).status_code == 200  # наставник видит файл своего стажёра
    login("dima")
    assert client.get(att["url"]).status_code == 403
    assert client.get("/api/attachments/999").status_code == 404


def test_attachment_only_and_validation(login, client):
    login("arina")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    resp = client.post("/api/chat/send", data={"file": (io.BytesIO(png), "screen.png")}, content_type="multipart/form-data")
    job = client.get(f"/api/chat/jobs/{resp.get_json()['job_id']}").get_json()
    assert "screen.png" in job["message"]["text"]
    assert client.get(resp.get_json()["user_message"]["attachments"][0]["url"]).headers["Content-Disposition"].startswith("inline")
    bad = client.post("/api/chat/send", data={"file": (io.BytesIO(b"x"), "virus.exe")}, content_type="multipart/form-data")
    assert bad.status_code == 400
    big = client.post("/api/chat/send", data={"file": (io.BytesIO(b"0" * (5 * 1024 * 1024 + 1)), "big.txt")},
                      content_type="multipart/form-data")
    assert big.status_code in {400, 413}
    login("ivan")
    resp = client.post("/api/chat/send", data={"file": (io.BytesIO(b"x"), "n.txt")}, content_type="multipart/form-data")
    job = client.get(f"/api/chat/jobs/{resp.get_json()['job_id']}").get_json()
    assert "сохранён" in job["message"]["text"]


# ------------------------------------------------------------------ уведомления
def test_notifications_for_intern_and_resolution(login, chat, client):
    login("arina")
    chat(action="escalate_confirm", params={"question": "Где взять доступ к макетам?"})
    ivan = user("ivan")
    assert any(not n.read for n in notify.unread(ivan))
    login("ivan")
    qid = Notification.query.filter(Notification.user_id == ivan.id, Notification.text.like("%макетам%")) \
        .first().dedup_key.split(":")[1]
    client.post(f"/api/mentor/questions/{qid}/answer", json={"answer": "В Figma, спроси у дизайнера", "publish": False})
    assert not any("макетам" in n.text for n in notify.unread(ivan))  # ответ дан — уведомление закрыто
    login("arina")
    me = client.get("/api/me").get_json()
    assert me["unread"] >= 1 and any("ответил" in n["text"].lower() for n in me["notifications"])
    page = client.get("/home").get_data(as_text=True)
    assert 'class="count"' in page and "unread" in page
    client.post("/api/notifications/read")
    assert 'class="count"' not in client.get("/home").get_data(as_text=True)


def test_notify_recent_hides_nudges(app):
    arina = user("arina")
    notify.notify(arina, "Напоминание", dedup_key="nudge:1:x")
    notify.notify(arina, "Обычное")
    texts = [n.text for n in notify.recent(arina)]
    assert "Обычное" in texts and "Напоминание" not in texts
    assert notify.notify(arina, "Дубль", dedup_key="nudge:1:x") is None


# ------------------------------------------------------------------ поиск по темам, профиль, доступы
def test_kb_search_catalog(app):
    arina = user("arina")
    found = kb.search_catalog(arina, "отпуск")
    assert found["topics"] and all(t["slug"] for t in found["topics"])
    assert found["topics"][0]["match"] == "title"
    cats = kb.search_catalog(arina, "Безопас")["categories"]
    assert cats and cats[0]["count"] > 0
    assert kb.search_catalog(arina, "x") == {"categories": [], "topics": []}
    assert not any(t["slug"] == "mentor-intern-assessment" for t in kb.search_catalog(arina, "оценк")["topics"])


def test_kb_search_api(login, client):
    login("arina")
    data = client.get("/api/kb/search?q=код-ревью").get_json()
    assert "categories" in data and data["results"]


def test_profile_update(login, client):
    login("arina")
    resp = client.put("/api/profile", json={"full_name": "Арина Петрова", "phone": "+7 999 123-45-67",
                                            "email": "arina.p@avito.ru"})
    assert resp.get_json()["name"] == "Арина Петрова" and resp.get_json()["initials"] == "АП"
    assert "Арина Петрова" in client.get("/profile").get_data(as_text=True)
    assert client.put("/api/profile", json={"full_name": "А", "email": "a@b.ru"}).status_code == 400
    assert client.put("/api/profile", json={"full_name": "А Б", "phone": "abc", "email": "a@b.ru"}).status_code == 400
    assert client.put("/api/profile", json={"full_name": "А Б", "email": "nope"}).status_code == 400
    assert client.put("/api/profile", json={"full_name": "А Б", "email": user("ivan").email}).status_code == 400


def test_access_page_statuses(login, client, chat):
    login("arina")
    chat("Нужен доступ к Jira")
    chat(action="access_confirm", params={"resource": "jira", "level": "edit", "justification": "Задачи"})
    page = client.get("/access").get_data(as_text=True)
    assert "На согласовании" in page and "Стажёрам не выдаётся" in page
    assert "Отгулы, отпуска" in client.get("/access?tab=absence").get_data(as_text=True)
    login("ivan")
    assert client.get("/access").status_code == 403


def test_meeting_model_default_status(app):
    m = Meeting.query.filter_by(kind="team").first()
    assert m.status == "accepted"
    db.session.rollback()
