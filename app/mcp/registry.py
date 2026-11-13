"""Реестр MCP-инструментов с проверкой ролей и валидацией аргументов по JSON Schema (подмножество)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..models import User
from ..permissions import ALL_ROLES, PermissionDenied


class ToolError(Exception):
    """Ошибка выполнения инструмента (бизнес-валидация, не найдено и т.п.)."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]
    roles: set[str] = field(default_factory=lambda: set(ALL_ROLES))
    mutating: bool = False  # изменяет данные во внешнем сервисе → требует подтверждения пользователя

    def describe(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"readOnlyHint": not self.mutating, "requiresConfirmation": self.mutating},
        }


TOOLS: dict[str, Tool] = {}

_TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list, "number": (int, float)}


def tool(name: str, description: str, properties: dict | None = None, required: list[str] | None = None,
         roles: set[str] | None = None, mutating: bool = False):
    schema = {"type": "object", "properties": properties or {}, "required": required or [],
              "additionalProperties": False}

    def decorator(fn):
        TOOLS[name] = Tool(name, description, schema, fn, set(roles or ALL_ROLES), mutating)
        return fn

    return decorator


def validate_args(schema: dict, args: dict) -> dict:
    if not isinstance(args, dict):
        raise ToolError("Аргументы должны быть объектом")
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args or args[key] in (None, ""):
            raise ToolError(f"Не заполнен обязательный параметр «{key}»")
    clean = {}
    for key, value in args.items():
        if key not in props:
            raise ToolError(f"Неизвестный параметр «{key}»")
        spec = props[key]
        expected = _TYPES.get(spec.get("type", "string"))
        if expected is int and isinstance(value, str) and value.isdigit():
            value = int(value)
        if expected and (not isinstance(value, expected) or (expected is int and isinstance(value, bool))):
            raise ToolError(f"Параметр «{key}» должен быть типа {spec.get('type')}")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolError(f"Параметр «{key}»: допустимые значения {spec['enum']}")
        clean[key] = value
    return clean


def list_tools(user: User) -> list[dict]:
    """Пользователь видит только инструменты, разрешённые его роли."""
    return [t.describe() for t in TOOLS.values() if user.role in t.roles]


def call_tool(user: User, name: str, args: dict | None = None) -> Any:
    t = TOOLS.get(name)
    if t is None:
        raise ToolError(f"Инструмент {name} не найден")
    if user.role not in t.roles:
        raise PermissionDenied(f"Инструмент {name} недоступен для роли «{user.role_label}»")
    clean = validate_args(t.input_schema, args or {})
    try:
        return t.handler(user, **clean)
    except PermissionDenied:
        raise
    except (ValueError, LookupError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Объект не найден"
        raise ToolError(message) from exc
