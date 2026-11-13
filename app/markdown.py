"""Минимальный безопасный рендер markdown (жирный, курсив, ссылки, списки) для материалов базы знаний."""
from __future__ import annotations

import re

from markupsafe import Markup, escape

_LINK = re.compile(r"\[([^\]]+)\]\((/[^)\s]*|https?://[^)\s]+)\)")


def _inline(text: str) -> str:
    html = str(escape(text))
    html = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html)
    html = re.sub(r"(?<![\w/])_(.+?)_(?!\w)", r"<i>\1</i>", html)
    return _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', html)


def render(text: str) -> Markup:
    out: list[str] = []
    list_tag: str | None = None
    for raw in (text or "").split("\n"):
        line = raw.strip()
        bullet = re.match(r"^[-•]\s+(.*)", line)
        number = re.match(r"^\d+\.\s+(.*)", line)
        tag = "ul" if bullet else "ol" if number else None
        if tag != list_tag and list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None
        if tag:
            if list_tag is None:
                out.append(f"<{tag}>")
                list_tag = tag
            out.append(f"<li>{_inline((bullet or number).group(1))}</li>")
        elif line:
            out.append(f"<p>{_inline(line)}</p>")
    if list_tag:
        out.append(f"</{list_tag}>")
    return Markup("".join(out))


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many
