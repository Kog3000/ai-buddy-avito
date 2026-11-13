"""Сквозные сценарии AI-buddy через HTTP API (соответствуют specs/scenarios/*.md)."""
from app.models import AbsenceRequest, AccessRequest, MentorQuestion

from .conftest import next_monday


def test_kb_answer_with_sources_and_checklist_link(login, chat):
    login("arina")
    msg = chat("Где хранятся документы нашей команды?")
    assert "Confluence" in msg["text"]
    assert msg["sources"][0]["url"] == "/kb/topic/faq-documents-storage"
    assert msg["meta"]["source"] == "kb"
    msg = chat("Расскажи о структуре компании и подразделениях")
    card = msg["cards"][0]
    assert card["actions"][0]["action"] == "task_done"
    done = chat(action="task_done", params=card["actions"][0]["params"])
    assert "4 из 28" in done["text"]


def test_not_found_escalation_and_faq_reuse(login, chat, client):
    login("arina")
    msg = chat("Сколько стоит обед в столовой?")
    assert msg["meta"]["source"] == "not_found"
    assert msg["cards"][0]["actions"][0]["action"] == "escalate_confirm"
    res = chat(action="escalate_confirm", params={"question": "Сколько стоит обед в столовой?"})
    assert "Иван" in res["text"]
    q = MentorQuestion.query.filter_by(question="Сколько стоит обед в столовой?").first()
    login("ivan")
    r = client.post(f"/api/mentor/questions/{q.id}/answer", json={"answer": "Комплексный обед — 350 ₽", "publish": True})
    assert r.status_code == 200
    login("arina")
    history = client.get("/api/chat/messages").get_json()["messages"]
    assert "350" in history[-1]["text"] and history[-1]["meta"]["from_mentor"]
    again = chat("Сколько стоит обед в столовой?")
    assert again["meta"]["source"] == "kb" and "350" in again["text"]


def test_access_single_flow(login, chat):
    login("arina")
    msg = chat("Нужен доступ к Stash")
    card = msg["cards"][0]
    fields = {f["name"]: f for f in card["fields"]}
    assert fields["level"]["value"] == "edit"
    assert [o[0] for o in fields["level"]["options"]] == ["read", "edit"]
    assert "«Неделя 1. Знакомство»" in fields["justification"]["value"]
    params = {**card["actions"][0]["params"], "level": "edit", "justification": "Работа над задачами", "project": ""}
    res = chat(action="access_confirm", params=params)
    assert res["cards"][0]["status"] == "pending_approval"
    assert "Мария Смирнова" in res["text"]
    again = chat("Нужен доступ к Stash")
    assert "уже есть заявка" in again["text"]
    dup = chat(action="access_confirm", params=params)
    assert "уже есть" in dup["text"]


def test_access_level_not_allowed_and_denied_resource(login, chat):
    login("arina")
    msg = chat("Нужен доступ к Stash с правами админа")
    assert "только владельцам проектов" in msg["text"]
    assert {f["name"]: f for f in msg["cards"][0]["fields"]}["level"]["value"] == "edit"
    denied = chat("Нужен доступ к продовой бд")
    assert denied["meta"]["denied"] and not denied["cards"]
    bad = chat(action="access_confirm", params={"resource": "prod-db", "level": "read", "justification": "очень надо"})
    assert "недоступно" in bad["text"] and bad["meta"]["error"]
    assert AccessRequest.query.count() == 0


def test_access_ask_resource_then_answer(login, chat):
    login("arina")
    msg = chat("Как запросить доступ?")
    assert "К какому сервису" in msg["text"] and msg["suggestions"]
    msg = chat("Sentry")
    assert msg["cards"][0]["title"].endswith("Sentry: frontend")
    cancel = chat("отмена")
    assert "отменил" in cancel["text"]


def test_access_bundle_flow(login, chat):
    login("arina")
    msg = chat("Помоги настроить доступы к основным сервисам")
    card = msg["cards"][0]
    assert len(card["rows"]) == 4
    res = chat(action="access_confirm_bundle", params={**card["actions"][0]["params"], "justification": "Онбординг"})
    assert len(res["cards"]) == 4
    status = chat("Мои заявки")
    assert "Заявок всего: 4" in status["text"]
    again = chat("Помоги настроить доступы к основным сервисам")
    assert "уже есть" in again["text"]


def test_absence_flow_two_steps(login, chat, client):
    login("arina")
    msg = chat("Как оформить отгул?")
    assert "не позднее" in msg["text"] and "Завтра" in msg["suggestions"]
    monday = next_monday(2)
    msg = chat(monday.strftime("%d.%m"))
    card = msg["cards"][0]
    assert card["rows"][0][1].startswith("Пн")
    res = chat(action="absence_confirm", params={**card["actions"][0]["params"], "comment": "дела", "notify_mentor": True})
    assert "Наставник получил уведомление" in res["text"]
    req = AbsenceRequest.query.one()
    login("maria")
    assert client.post(f"/api/approvals/absence/{req.id}", json={"approve": True}).status_code == 200
    login("arina")
    assert "согласован" in client.get("/api/chat/messages").get_json()["messages"][-1]["text"]


def test_absence_invalid_date_and_flag_off(login, chat, client):
    login("arina")
    msg = chat("Хочу отгул сегодня")
    assert msg["meta"]["error"] and "Не получится" in msg["text"]
    login("maria")
    client.post("/api/flags/absence_requests", json={"enabled": False})
    login("arina")
    msg = chat("Хочу взять отгул завтра")
    assert not msg["cards"] and "сервисе отсутствий" in msg["text"]


def test_sick_is_redirected(login, chat):
    login("arina")
    msg = chat("Я заболел, что делать?")
    assert "HR" in msg["text"] and not msg["cards"]


def test_meeting_flow(login, chat, client):
    login("arina")
    chat("Как устроено code review?")
    msg = chat("Запланируй встречу с наставником")
    slot = msg["cards"][0]["slots"][0]
    pick = chat(action="meeting_pick", params={"start": slot["start"]})
    agenda = {f["name"]: f for f in pick["cards"][0]["fields"]}["agenda"]["value"]
    assert "code review" in agenda
    res = chat(action="meeting_confirm", params={"start": slot["start"], "title": "1:1", "agenda": agenda})
    assert res["cards"][0]["status_label"] == "Ждёт подтверждения наставника"
    week1 = lambda: client.get("/api/checklist").get_json()["weeks"][0]["tasks"]  # noqa: E731
    task = lambda: next(t for t in week1() if t["title"].startswith("Назначить регулярные встречи 1:1"))  # noqa: E731
    assert not task()["done"]  # закроется после подтверждения наставником
    meeting_id = res["cards"][0]["meeting_id"]
    login("ivan")
    assert "Подтвердить" in client.get("/meetings").get_data(as_text=True)
    assert client.post(f"/api/meetings/{meeting_id}/decide", json={"accept": True}).get_json()["status"] == "accepted"
    login("arina")
    assert task()["done"] and task()["source"] == "auto"
    assert any("подтвердил" in n["text"] for n in client.get("/api/me").get_json()["notifications"])
    busy = chat(action="meeting_confirm", params={"start": slot["start"]})
    assert busy["meta"]["error"]


def test_goal_flow(login, chat):
    login("arina")
    msg = chat("Помоги заполнить трекер целей")
    assert "одной фразой" in msg["text"]
    msg = chat("Закрыть 2 задачи из бэклога до конца месяца")
    assert msg["cards"][0]["actions"][0]["action"] == "goal_confirm"
    res = chat(action="goal_confirm", params={"title": "Закрыть 2 задачи из бэклога", "metric": "2 MR"})
    assert "Добавил цель" in res["text"]
    quick = chat("Добавь цель: изучить TanStack Query")
    assert quick["cards"][0]["fields"][0]["value"] == "изучить TanStack Query"


def test_context_answers(login, chat):
    login("arina")
    assert "Иван Иванов" in chat("Кто мой наставник?")["text"]
    assert "Мария Смирнова" in chat("кто мой руководитель")["text"]
    assert "Командная встреча" in chat("Мои встречи")["text"]
    steps = chat("Что дальше?")
    assert "«Неделя 1. Знакомство»" in steps["text"] and steps["cards"][0]["type"] == "steps"
    assert chat("Привет")["suggestions"]
    assert "Обращайся" in chat("спасибо")["text"]
    assert "нет заявок" in chat("Мои заявки")["text"]


def test_staff_chat_limits(login, chat):
    login("ivan")
    assert "сводка по стажёрам" in chat("Что дальше?")["text"].lower()
    assert "аккаунта стажёра" in chat("Запланируй встречу с наставником")["text"]
    assert "стажёра" in chat("Спроси наставника: тест")["text"]
    assert chat("Добавь цель: тест")["text"].startswith("Трекер")
    msg = chat("шкала оценки стажёров на экваторе")
    assert msg["sources"][0]["slug"] == "mentor-intern-assessment"  # наставнику материал доступен
    assert "отгул" in chat("Как оформить отгул?")["text"].lower()
    unknown = chat("абракадабра квазар")
    assert unknown["meta"]["source"] == "not_found" and not unknown["cards"]


def test_intern_cannot_see_mentor_material_in_chat(login, chat):
    login("arina")
    msg = chat("шкала оценки стажёров на экваторе")
    assert all(s["slug"] != "mentor-intern-assessment" for s in msg["sources"])
    assert "4-балльной" not in msg["text"]


def test_unknown_action(login, chat):
    login("arina")
    assert "Не понял" in chat(action="hack_the_planet")["text"]
    assert chat(action="access_confirm", params={})["meta"]["error"]


def test_confidential_access_requires_task_justification(login, chat):
    login("arina")
    msg = chat("Нужен доступ к стратегии и финпоказателям")
    card = msg["cards"][0]
    assert "конфиденциальные" in msg["text"].lower()
    assert {f["name"]: f for f in card["fields"]}["justification"]["value"] == ""
    res = chat(action="access_confirm", params={"resource": "strategy", "level": "read",
                                                "justification": "Задача FE-123: экран планов запусков"})
    assert "Мария Смирнова" in res["text"]


def test_metrics_by_topic(login, chat, client):
    login("arina")
    chat("Как устроено code review?")
    chat("Нужен доступ к Jira")
    login("ivan")
    m = client.get("/api/metrics").get_json()
    cats = {c["key"]: c for c in m["categories"]}
    assert cats["tasks"]["bot"] == 1 and cats["org"]["bot"] == 1
    assert cats["tasks"]["mentor"] == 1  # вопрос Дмитрия про мерж без аппрува ждёт наставника
    assert m["baseline"][0]["now"] == "~2 недели" and m["mentor_hours_baseline"] == 5.0
    assert "Базовые показатели онбординга" in client.get("/metrics").get_data(as_text=True)
