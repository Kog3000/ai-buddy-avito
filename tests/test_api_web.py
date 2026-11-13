"""HTTP: страницы, права доступа, CSRF, REST API."""
from datetime import datetime, time

import pytest

from app import create_app
from app.chat import _run
from app.config import TestConfig
from app.models import ChatJob, ChatMessage, db

from .conftest import next_monday

INTERN_PAGES = ["/home", "/chat", "/checklist", "/kb", "/kb/access", "/kb/topic/access-vpn", "/progress",
                "/mentor", "/profile", "/access", "/access?tab=chats", "/access?tab=absence", "/access?tab=bogus", "/help"]


def test_login_logout(client):
    assert client.get("/home").status_code == 302
    page = client.get("/login")
    assert "Тестовые аккаунты" in page.get_data(as_text=True)
    bad = client.post("/login", data={"login": "arina", "password": "wrong"})
    assert "Неверный" in bad.get_data(as_text=True)
    by_email = client.post("/login", data={"login": "Example@mail.ru", "password": "demo123"})
    assert by_email.location == "/home"  # вход по e-mail, как на макете
    client.post("/logout")
    ok = client.post("/login?next=/kb", data={"login": "arina", "password": "demo123"})
    assert ok.location == "/kb"
    evil = client.post("/login?next=//evil.com", data={"login": "arina", "password": "demo123"})
    assert evil.location == "/home"
    assert client.get("/").location == "/home"
    client.post("/logout")
    assert client.get("/api/me").status_code == 401


@pytest.mark.parametrize("path", INTERN_PAGES)
def test_intern_pages_render(login, client, path):
    login("arina")
    assert client.get(path).status_code == 200


def test_intern_cannot_open_staff_pages(login, client):
    login("arina")
    for path in ["/team", "/team/questions", "/approvals", "/metrics", "/team/intern/1"]:
        assert client.get(path).status_code == 403
    assert client.get("/api/metrics").status_code == 403
    assert client.get("/kb/topic/mentor-intern-assessment").status_code == 404
    assert client.get("/kb/nope").status_code == 404
    assert client.get("/no-such-page").status_code == 404


@pytest.mark.parametrize("name,paths", [
    ("ivan", ["/team", "/team/questions", "/approvals", "/metrics", "/kb", "/chat", "/profile"]),
    ("maria", ["/team", "/team/questions", "/approvals", "/metrics"]),
    ("elena", ["/admin/users", "/metrics", "/kb", "/profile"]),
])
def test_staff_pages_render(login, client, name, paths):
    login(name)
    for path in paths:
        assert client.get(path).status_code == 200, path
    assert client.get("/home").status_code == 403


def test_redirects_by_role(login, client):
    login("ivan")
    assert client.get("/").location == "/team"
    login("elena")
    assert client.get("/").location == "/admin/users"
    login("arina")
    assert client.get("/").location == "/home"


def test_mentor_sees_only_own_interns(login, client):
    from .conftest import user
    login("ivan")
    arina_id = user("arina").id
    assert client.get(f"/team/intern/{arina_id}").status_code == 200
    assert client.get(f"/api/interns/{arina_id}").get_json()["checklist"]["total"] == 28
    login("maria")
    assert client.get(f"/api/interns/{arina_id}").status_code == 200
    login("dima")
    assert client.get(f"/api/goals?user_id={arina_id}").status_code == 403
    assert client.get("/api/goals?user_id=999").status_code == 404


def test_csrf_enforced():
    app = create_app(type("CsrfCfg", (TestConfig,), {"WTF_CSRF_ENABLED": True}))
    with app.app_context():
        c = app.test_client()
        c.get("/login")
        with c.session_transaction() as s:
            token = s["csrf"]
        assert c.post("/login", data={"login": "arina", "password": "demo123"}).status_code == 400
        assert c.post("/login", data={"login": "arina", "password": "demo123", "csrf_token": token}).status_code == 302
        assert c.post("/api/chat/send", json={"text": "привет"}).status_code == 400
        c.get("/home")  # после входа сессия новая — токен выдаётся заново
        with c.session_transaction() as s:
            token = s["csrf"]
        ok = c.post("/api/chat/send", json={"text": "привет"}, headers={"X-CSRF-Token": token})
        assert ok.status_code == 202
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_chat_api_validation(login, client):
    login("arina")
    assert client.post("/api/chat/send", json={}).status_code == 400
    assert client.post("/api/chat/send", json={"action": 5}).status_code == 400
    assert client.get("/api/chat/jobs/unknown").status_code == 404
    first = client.get("/api/chat/messages").get_json()["messages"]
    assert first[0]["role"] == "assistant"  # приветствие
    mid = first[0]["id"]
    assert client.post(f"/api/chat/messages/{mid}/feedback", json={"value": -1}).status_code == 200
    assert db.session.get(ChatMessage, mid).feedback == -1
    assert client.get(f"/api/chat/messages?after={mid}").get_json()["messages"] == []
    login("dima")
    assert client.post(f"/api/chat/messages/{mid}/feedback", json={"value": 1}).status_code == 404


def test_chat_job_failure_is_reported(app, arina, monkeypatch):
    job = ChatJob(user_id=arina.id)
    db.session.add(job)
    db.session.commit()

    def boom(self, text):
        raise RuntimeError("agent crashed")
    monkeypatch.setattr("app.agent.agent.Agent.handle_message", boom)
    _run(app, job.id, arina.id, "привет", None, {})
    db.session.expire_all()
    assert db.session.get(ChatJob, job.id).status == "error"


def test_async_jobs_use_thread_pool(app, login, client):
    app.config["SYNC_JOBS"] = False
    login("arina")
    resp = client.post("/api/chat/send", json={"text": "Что дальше?"})
    assert resp.status_code == 202
    job_id = resp.get_json()["job_id"]
    import time as _t
    for _ in range(50):
        data = client.get(f"/api/chat/jobs/{job_id}").get_json()
        if data["status"] != "pending":
            break
        _t.sleep(0.05)
    assert data["status"] in {"done", "error"}


def test_checklist_and_kb_api(login, client):
    login("arina")
    data = client.post("/api/checklist/4", json={"done": True}).get_json()
    assert data["done"] == 4
    assert client.post("/api/checklist/999", json={"done": True}).status_code == 404
    res = client.get("/api/kb/search?q=code review").get_json()["results"]
    assert res[0]["slug"] == "proc-code-review"
    assert client.post("/api/kb/topics/proc-code-review/done", json={"done": True}).get_json()["progress"]["done"] == 3
    assert client.post("/api/kb/topics/mentor-handbook/done").status_code == 404


def test_requests_and_approvals_api(login, client, chat):
    login("arina")
    chat("Нужен доступ к Jira")
    chat(action="access_confirm", params={"resource": "jira", "level": "edit", "justification": "Задачи"})
    reqs = client.get("/api/requests").get_json()
    rid = reqs["access"][0]["id"]
    assert client.post("/api/approvals/access/1", json={"approve": True}).status_code == 403
    assert client.post("/api/requests/other/1/cancel").status_code == 404
    login("maria")
    assert client.post("/api/approvals/bogus/1", json={}).status_code == 404
    assert client.post(f"/api/approvals/access/{rid}", json={"approve": False, "comment": "позже"}).get_json()["status"] == "rejected"
    assert client.post(f"/api/approvals/access/{rid}", json={"approve": True}).status_code == 400
    login("arina")
    chat(action="access_confirm", params={"resource": "jira", "level": "read", "justification": "Только смотреть"})
    rid2 = client.get("/api/requests").get_json()["access"][0]["id"]
    assert client.post(f"/api/requests/access/{rid2}/cancel").get_json()["status"] == "cancelled"
    assert client.get("/requests").location == "/access"
    page = client.get("/access").get_data(as_text=True)
    assert "Jira" in page and "Запросить" in page


def test_meetings_notes_goals_api(login, client):
    login("arina")
    slots = client.get("/api/meetings/slots").get_json()["slots"]
    assert slots
    r = client.post("/api/meetings", json={"start": slots[0]["start"], "title": "1:1", "agenda": "вопросы"})
    assert r.status_code == 201
    assert client.post("/api/meetings", json={"start": slots[0]["start"]}).status_code == 400
    sat = datetime.combine(next_monday(2), time(9))
    assert client.post("/api/meetings", json={"start": sat.isoformat()}).status_code == 400
    note = client.post("/api/notes", json={"title": "1:1", "body": "итоги"}).get_json()
    assert client.put(f"/api/notes/{note['id']}", json={"title": "1:1 (ред.)", "body": "x"}).get_json()["title"] == "1:1 (ред.)"
    assert len(client.get("/api/notes").get_json()["notes"]) == 2
    assert client.post("/api/notes", json={"title": ""}).status_code == 400
    assert client.delete(f"/api/notes/{note['id']}").status_code == 200
    goal = client.post("/api/goals", json={"title": "Закрыть задачу", "metric": "1 MR", "due": "2026-12-01"}).get_json()
    assert goal["due"] == "01.12.2026"
    assert client.post(f"/api/goals/{goal['id']}/status", json={"done": True}).get_json()["status"] == "done"
    assert client.get("/api/goals").get_json()["goals"][0]["id"] == goal["id"]
    assert "Закрыть задачу" in client.get("/progress").get_data(as_text=True)
    login("maria")
    assert client.post(f"/api/goals/{goal['id']}/agree").get_json()["agreed"] is True
    assert "Закрыть задачу" in client.get("/team").get_data(as_text=True)
    login("arina")
    assert client.delete(f"/api/goals/{goal['id']}").status_code == 200


def test_mentor_question_pages(login, client, chat):
    login("arina")
    chat(action="escalate_confirm", params={"question": "Как попасть на внутренний митап?"})
    assert "Ждёт ответа" in client.get("/mentor").get_data(as_text=True)
    login("ivan")
    page = client.get("/team").get_data(as_text=True)
    assert "внутренний митап" in page
    assert "внутренний митап" in client.get("/team/questions").get_data(as_text=True)
    me = client.get("/api/me").get_json()
    assert me["notifications"]
    assert me["unread"] >= 1
    assert client.post("/api/notifications/read").status_code == 200
    me = client.get("/api/me").get_json()
    assert me["unread"] == 0 and all(n["read"] for n in me["notifications"])


def test_flags_and_metrics_api(login, client, chat):
    login("arina")
    chat("Где хранятся документы нашей команды?")
    chat("Абракадабра квазар?")
    m = client.get("/api/chat/messages").get_json()["messages"]
    client.post(f"/api/chat/messages/{m[-3]['id']}/feedback", json={"value": 1})
    assert client.post("/api/flags/llm_answers", json={"enabled": False}).status_code == 403
    login("ivan")
    metrics = client.get("/api/metrics").get_json()
    assert metrics["answered_by_bot"] == 1 and metrics["questions_total"] == 2
    assert metrics["deflection_rate"] == 50 and metrics["csat"] == 100
    login("maria")
    flags = client.post("/api/flags/llm_answers", json={"enabled": False}).get_json()["flags"]
    assert next(f for f in flags if f["key"] == "llm_answers")["enabled"] is False
    assert client.get("/api/flags").status_code == 200
    assert client.post("/api/flags/unknown", json={"enabled": True}).status_code == 404
    assert "Feature flags" in client.get("/metrics").get_data(as_text=True)


def test_cron_endpoint(client):
    assert client.post("/api/cron/run").status_code == 401
    r = client.post("/api/cron/run", headers={"X-Cron-Token": TestConfig.CRON_TOKEN})
    assert r.status_code == 200 and r.get_json()["nudges"] == 2


def test_cli_commands(app):
    runner = app.test_cli_runner()
    assert "seeded" in runner.invoke(args=["seed"]).output
    assert "nudges" in runner.invoke(args=["cron"]).output


def test_healthz(client):
    assert client.get("/healthz").get_json() == {"status": "ok"}
