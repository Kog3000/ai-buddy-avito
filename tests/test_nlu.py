import pytest

from app.agent.nlu import detect, detect_level, detect_resources


@pytest.mark.parametrize("text,intent", [
    ("Привет!", "greeting"),
    ("спасибо", "thanks"),
    ("Как оформить отгул?", "absence"),
    ("Хочу взять отпуск в декабре", "absence"),
    ("Можно поработать из дома в пятницу?", "absence"),
    ("Можно ли мержить MR, если ревьюер в отпуске?", "kb"),
    ("Нужен доступ к Stash", "access"),
    ("Помоги настроить доступы к основным сервисам", "access"),
    ("Запланируй встречу с наставником", "meeting"),
    ("Что дальше?", "next_steps"),
    ("Мои заявки", "status"),
    ("Кто мой наставник?", "who_mentor"),
    ("кто мой руководитель", "who_lead"),
    ("Мои встречи", "schedule"),
    ("Я заболел", "sick"),
    ("Спроси наставника: как поднять стенд?", "escalate"),
    ("Добавь цель: закрыть 2 задачи", "goal"),
    ("Где хранятся документы нашей команды?", "kb"),
    ("Какие в компании ценности и правила?", "kb"),
])
def test_intents(app, text, intent):
    assert detect(text).name == intent


def test_slots(app):
    i = detect("Нужен доступ к Grafana на чтение")
    assert i.slots["resources"] == ["grafana"] and i.slots["level"] == "read"
    assert detect("Помоги настроить доступы к основным сервисам").slots["bundle"] is True
    assert detect("Спроси наставника: как поднять стенд?").slots["question"] == "как поднять стенд?"
    assert detect("Добавь цель: закрыть 2 задачи").slots["title"] == "закрыть 2 задачи"
    assert detect("отгул завтра").slots["dates"] is not None


def test_stateful_detection(app):
    assert detect("отмена", {"flow": "access"}).name == "cancel"
    assert detect("отмена").name == "kb"
    assert detect("в пятницу", {"flow": "absence", "stage": "dates", "kind": "day_off"}).name == "absence"
    assert detect("Jira", {"flow": "access", "stage": "resource"}).slots["resources"] == ["jira"]
    assert detect("Выучить TypeScript", {"flow": "goal", "stage": "title"}).slots["title"] == "Выучить TypeScript"


def test_resources_and_levels(app):
    assert detect_resources("доступ к редашу") == ["redash"]
    assert "prod-db" in detect_resources("нужна продовая бд")
    assert set(detect_resources("гитлаб и джира")) == {"stash", "jira"}
    assert detect_level("права админа") == "admin"
    assert detect_level("на редактирование") == "edit"
    assert detect_level("просто доступ") is None


@pytest.mark.parametrize("text,intent,category", [
    ("Нужен доступ к Jira", "access", "org"),
    ("Где взять ноутбук?", "kb", "org"),
    ("Как приоритизировать задачи из бэклога?", "kb", "tasks"),
    ("Как устроено code review?", "kb", "tasks"),
    ("Какой стек у команды, React?", "kb", "professional"),
    ("Что такое ИПР?", "kb", "growth"),
    ("Как дать обратную связь по онбордингу?", "kb", "feedback"),
    ("Привет", "greeting", "other"),
    ("Добавь цель: выучить TS", "goal", "growth"),
])
def test_question_categories(text, intent, category):
    from app.agent.nlu import categorize
    assert categorize(text, intent) == category
