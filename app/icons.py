"""Встраивание SVG-иконок из static/icons в разметку.

Иконки вставляются inline, чтобы наследовать цвет текста (например, активный пункт меню).
Чёрные и серые обводки/заливки заменяются на currentColor; фирменные цвета (иконка AI-buddy) сохраняются.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

ICONS_DIR = Path(__file__).parent / "static" / "icons"
NEUTRAL = re.compile(r'(stroke|fill)="(black|#000000|#5B5B5B|#8C8C8C|#9E1203|white)"', re.IGNORECASE)


@lru_cache(maxsize=64)
def _load(name: str, keep_colors: bool) -> str:
    svg = (ICONS_DIR / f"{name}.svg").read_text(encoding="utf-8")
    svg = re.sub(r'\sstyle="[^"]*"', "", svg)  # inline-стили из Figma перебивают currentColor
    if not keep_colors:
        svg = NEUTRAL.sub(r'\1="currentColor"', svg)
    return svg.strip()


def svg_icon(name: str, size: int | float | None = None, cls: str = "", keep_colors: bool = False) -> Markup:
    svg = _load(name, keep_colors)
    attrs = ' aria-hidden="true" focusable="false"'
    if cls:
        attrs += f' class="{cls}"'
    if size:
        svg = re.sub(r'\swidth="[^"]*"', f' width="{size}"', svg, count=1)
        svg = re.sub(r'\sheight="[^"]*"', f' height="{size}"', svg, count=1)
    return Markup(svg.replace("<svg", "<svg" + attrs, 1))


def available() -> set[str]:
    return {p.stem for p in ICONS_DIR.glob("*.svg")}
