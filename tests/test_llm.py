"""LLM-режим: ответы по материалам базы знаний и деградация в офлайн при сбоях."""
import pytest
import requests

from app.agent import llm
from app.agent.agent import Agent
from app.analytics import set_flag


class FakeResp:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._payload


@pytest.fixture()
def llm_on(app):
    app.config.update(LLM_PROVIDER="openai", LLM_API_KEY="test-key", LLM_BASE_URL="http://llm.local/v1")
    return app


def test_disabled_by_default(app):
    assert llm.enabled() is False


def test_llm_answer_uses_context(llm_on, arina, monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, auth=headers["Authorization"], messages=json["messages"])
        return FakeResp(payload={"choices": [{"message": {"content": "Документы — в **Confluence**."}}]})

    monkeypatch.setattr(requests, "post", fake_post)
    reply = Agent(arina).handle_message("Где хранятся документы нашей команды?")
    assert reply.text == "Документы — в **Confluence**." and reply.meta["source"] == "llm"
    assert captured["url"] == "http://llm.local/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"
    system = "\n".join(m["content"] for m in captured["messages"] if m["role"] == "system")
    assert "Иван Иванов" in system and "Где хранятся документы команды" in system
    assert "Шкала оценки стажёров" not in system  # недоступные материалы не попадают в контекст


@pytest.mark.parametrize("response", [
    FakeResp(status=500),
    FakeResp(bad_json=True),
    FakeResp(payload={"choices": []}),
    FakeResp(payload={"choices": [{"message": {"content": "  "}}]}),
])
def test_llm_failures_fall_back_to_offline(llm_on, arina, monkeypatch, response):
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    reply = Agent(arina).handle_message("Где хранятся документы нашей команды?")
    assert reply.meta["source"] == "kb" and "llm_fallback" in reply.meta
    assert "Confluence" in reply.text


def test_llm_network_error(llm_on, arina, monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "hi"}])


def test_llm_flag_off(llm_on):
    set_flag("llm_answers", False)
    assert llm.enabled() is False
