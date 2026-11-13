"""MCP-сервер (JSON-RPC 2.0 поверх HTTP): initialize, tools/list, tools/call.

Идентичность вызывающего берётся из сессии (в проде — из корпоративной авторизации),
поэтому агент не может выполнить действие от имени другого пользователя.
"""
from __future__ import annotations

import json

from ..models import User
from ..permissions import PermissionDenied
from . import tools  # noqa: F401  регистрирует инструменты
from .registry import ToolError, call_tool, list_tools

PROTOCOL_VERSION = "2025-06-18"


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(user: User, payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or "method" not in payload:
        return _error(payload.get("id") if isinstance(payload, dict) else None, -32600, "Invalid Request")
    req_id = payload.get("id")
    method = payload["method"]
    params = payload.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": "ai-buddy-corporate-services", "version": "0.1.0"},
            "capabilities": {"tools": {"listChanged": False}},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": list_tools(user)}}
    if method == "tools/call":
        name = params.get("name")
        try:
            result = call_tool(user, name, params.get("arguments") or {})
        except PermissionDenied as exc:
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "isError": True, "content": [{"type": "text", "text": f"Доступ запрещён: {exc}"}]}}
        except ToolError as exc:
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "isError": True, "content": [{"type": "text", "text": str(exc)}]}}
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
            "structuredContent": {"result": result},
        }}
    return _error(req_id, -32601, f"Method not found: {method}")


class MCPClient:
    """Клиент, через который агент вызывает инструменты (in-process транспорт к тому же серверу)."""

    def __init__(self, user: User):
        self.user = user
        self.calls: list[dict] = []

    def call(self, name: str, **arguments):
        response = handle(self.user, {"jsonrpc": "2.0", "id": len(self.calls) + 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": arguments}})
        result = response["result"]
        self.calls.append({"tool": name, "ok": not result["isError"]})
        if result["isError"]:
            text = result["content"][0]["text"]
            if text.startswith("Доступ запрещён"):
                raise PermissionDenied(text.split(": ", 1)[1])
            raise ToolError(text)
        return result["structuredContent"]["result"]
