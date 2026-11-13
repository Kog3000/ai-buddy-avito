from datetime import date, timedelta

import pytest

from app import create_app
from app.config import TestConfig
from app.models import User, db


@pytest.fixture()
def app():
    application = create_app(TestConfig)
    with application.app_context():
        yield application
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


@pytest.fixture()
def client(app):
    return app.test_client()


def user(login: str) -> User:
    return User.query.filter_by(login=login).first()


@pytest.fixture()
def arina(app):
    return user("arina")


@pytest.fixture()
def login(client):
    def _login(name: str):
        client.post("/logout")
        resp = client.post("/login", data={"login": name, "password": "demo123"})
        assert resp.status_code == 302
        return client
    return _login


@pytest.fixture()
def chat(client):
    """Отправляет сообщение/действие в чат и возвращает ответ ассистента."""
    def _send(text: str = "", action: str | None = None, params: dict | None = None):
        payload = {"text": text}
        if action:
            payload = {"action": action, "params": params or {}, "label": action}
        resp = client.post("/api/chat/send", json=payload)
        assert resp.status_code == 202, resp.get_json()
        job = client.get(f"/api/chat/jobs/{resp.get_json()['job_id']}").get_json()
        assert job["status"] == "done", job
        return job["message"]
    return _send


def next_monday(offset_weeks: int = 1) -> date:
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7) + timedelta(weeks=offset_weeks - 1)
