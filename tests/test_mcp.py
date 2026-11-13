import json

import pytest

from app.mcp.registry import ToolError, call_tool, list_tools, validate_args
from app.mcp.server import MCPClient, handle
from app.permissions import PermissionDenied

from .conftest import user


def rpc(u, method, params=None, rid=1):
    return handle(u, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})


def test_initialize_and_list_filtered_by_role(arina):
    init = rpc(arina, "initialize")["result"]
    assert init["capabilities"]["tools"] is not None
    intern_tools = {t["name"] for t in rpc(arina, "tools/list")["result"]["tools"]}
    maria_tools = {t["name"] for t in list_tools(user("maria"))}
    assert "access_create_request" in intern_tools and "absence_decide" not in intern_tools
    assert "absence_decide" in maria_tools and "calendar_book_mentor_meeting" not in maria_tools
    tool = next(t for t in rpc(arina, "tools/list")["result"]["tools"] if t["name"] == "access_create_request")
    assert tool["annotations"]["requiresConfirmation"] is True
    assert set(tool["inputSchema"]["required"]) == {"resource", "level", "justification"}


def test_tools_call_success_and_errors(arina):
    ok = rpc(arina, "tools/call", {"name": "kb_search", "arguments": {"query": "code review"}})["result"]
    assert not ok["isError"] and json.loads(ok["content"][0]["text"])[0]["slug"] == "proc-code-review"
    denied = rpc(arina, "tools/call", {"name": "absence_decide", "arguments": {"request_id": 1, "approve": True}})
    assert denied["result"]["isError"] and "Доступ запрещён" in denied["result"]["content"][0]["text"]
    bad = rpc(arina, "tools/call", {"name": "kb_search", "arguments": {}})
    assert bad["result"]["isError"] and "query" in bad["result"]["content"][0]["text"]
    missing = rpc(arina, "tools/call", {"name": "kb_get_topic", "arguments": {"slug": "mentor-intern-assessment"}})
    assert missing["result"]["isError"] and "не найден" in missing["result"]["content"][0]["text"]
    assert rpc(arina, "nope")["error"]["code"] == -32601
    assert handle(arina, {"id": 1})["error"]["code"] == -32600
    assert handle(arina, "garbage")["error"]["code"] == -32600


def test_validate_args():
    schema = {"properties": {"n": {"type": "integer"}, "kind": {"type": "string", "enum": ["a", "b"]},
                             "flag": {"type": "boolean"}}, "required": ["n"]}
    assert validate_args(schema, {"n": "5"}) == {"n": 5}
    for args in ({}, {"n": 1, "x": 1}, {"n": "abc"}, {"n": 1, "kind": "c"}, {"n": True}, {"n": 1, "flag": "yes"}):
        with pytest.raises(ToolError):
            validate_args(schema, args)
    with pytest.raises(ToolError):
        validate_args(schema, [])


def test_call_tool_unknown_and_client(arina):
    with pytest.raises(ToolError):
        call_tool(arina, "rm_rf", {})
    client = MCPClient(arina)
    assert client.call("profile_get")["mentor"] == "Иван Иванов"
    with pytest.raises(PermissionDenied):
        client.call("mentor_answer", question_id=1, answer="x")
    with pytest.raises(ToolError):
        client.call("absence_create_request", kind="day_off", date_from="not-a-date", date_to="x")
    assert [c["ok"] for c in client.calls] == [True, False, False]


def test_all_readonly_tools_callable(arina):
    c = MCPClient(arina)
    assert c.call("checklist_get")["total"] == 28
    assert len(c.call("checklist_next_steps", limit=2)) == 2
    assert c.call("access_list_resources")[0]["code"] == "vpn"
    assert c.call("access_my_requests") == []
    assert c.call("absence_my_requests") == []
    assert c.call("absence_policy", kind="vacation")["notice_days"] == 14
    assert c.call("calendar_upcoming")
    assert c.call("calendar_free_slots", days=3)
    assert c.call("goals_list") == []
    assert c.call("goals_add", title="Изучить стек")["title"] == "Изучить стек"
    assert c.call("checklist_set_task", task_id=4, done=True)["status"] == "done"
    assert c.call("kb_get_topic", slug="comm-channels")["title"]


def test_approver_tools(arina):
    req = MCPClient(arina).call("access_create_request", resource="stash", level="edit", justification="Задачи")
    maria = MCPClient(user("maria"))
    assert maria.call("access_decide", request_id=req["request"]["id"], approve=True)["status"] == "granted"
    q = MCPClient(arina).call("mentor_ask", question="Как устроены дежурства у нас?")
    assert MCPClient(user("ivan")).call("mentor_answer", question_id=q["id"], answer="Пока не дежуришь")["status"] == "answered"


def test_mcp_http_endpoint(login, client):
    assert client.post("/mcp", json={}).status_code == 401
    login("arina")
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert resp.status_code == 200 and resp.get_json()["id"] == 7
