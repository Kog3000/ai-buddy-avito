"""Обработка русского текста: токенизация, стемминг, разбор дат."""
from __future__ import annotations

import re
from datetime import date, timedelta
from functools import lru_cache

import snowballstemmer

_stemmer = snowballstemmer.stemmer("russian")
_WORD = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)

STOPWORDS = {
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то", "все", "она", "так", "его",
    "но", "да", "ты", "к", "у", "же", "вы", "за", "бы", "по", "только", "ее", "мне", "было", "вот", "от",
    "меня", "еще", "нет", "о", "из", "ему", "теперь", "когда", "даже", "ну", "ли", "если", "уже", "или",
    "ни", "быть", "был", "него", "до", "вас", "нибудь", "опять", "уж", "вам", "ведь", "там", "потом",
    "себя", "ничего", "ей", "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы", "тебя",
    "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего", "раз", "тоже", "себе", "под", "будет",
    "ж", "тогда", "кто", "этот", "того", "потому", "этого", "какой", "совсем", "ним", "здесь", "этом",
    "один", "почти", "мой", "тем", "чтобы", "нее", "сейчас", "были", "куда", "зачем", "всех", "никогда",
    "можно", "при", "наконец", "два", "об", "другой", "хоть", "после", "над", "больше", "тот", "через",
    "эти", "нас", "про", "всего", "них", "какая", "много", "разве", "три", "эту", "моя", "впрочем",
    "хорошо", "свою", "этой", "перед", "иногда", "лучше", "чуть", "том", "нельзя", "такой", "им", "более",
    "всегда", "конечно", "всю", "между", "это", "мою", "наш", "наша", "нашей", "нашей", "мне", "пожалуйста",
    "подскажи", "подскажите", "скажи", "расскажи", "хочу", "нужно", "нужен", "нужна", "привет",
}


def normalize(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


@lru_cache(maxsize=20000)
def stem(word: str) -> str:
    return _stemmer.stemWord(word)


def tokens(text: str, keep_stopwords: bool = False) -> list[str]:
    words = _WORD.findall(normalize(text))
    return [stem(w) for w in words if keep_stopwords or w not in STOPWORDS]


def has_any(text: str, stems: list[str]) -> bool:
    """Есть ли в тексте слово, начинающееся с одного из префиксов (по нормализованному тексту)."""
    words = _WORD.findall(normalize(text))
    return any(w.startswith(s) for w in words for s in stems)


def has_phrase(text: str, phrases: list[str]) -> bool:
    t = normalize(text)
    return any(p in t for p in phrases)


# ---------------------------------------------------------------- даты
MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3, "пятниц": 4, "суббот": 5, "воскресен": 6,
}
WEEKDAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTH_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
             "октября", "ноября", "декабря"]


def _month_from_word(word: str) -> int | None:
    word = normalize(word)
    if word.startswith("мая") or word == "май":
        return 5
    for prefix, num in MONTHS.items():
        if prefix != "ма" and word.startswith(prefix):
            return num
    return None


def _make_date(day: int, month: int, today: date) -> date | None:
    year = today.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    if candidate < today - timedelta(days=31):
        candidate = date(year + 1, month, day)
    return candidate


def _single_dates(text: str, today: date) -> list[date]:
    t = normalize(text)
    found: list[tuple[int, date]] = []
    for m in re.finditer(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b", t):
        day, month = int(m.group(1)), int(m.group(2))
        if m.group(3):
            year = int(m.group(3))
            year = year + 2000 if year < 100 else year
            try:
                found.append((m.start(), date(year, month, day)))
            except ValueError:
                pass
        else:
            d = _make_date(day, month, today)
            if d:
                found.append((m.start(), d))
    for m in re.finditer(r"\b(\d{1,2})(?:\s*(?:-|по|и|—)\s*(\d{1,2}))?\s+([а-я]+)", t):
        month = _month_from_word(m.group(3))
        if not month:
            continue
        d = _make_date(int(m.group(1)), month, today)
        if d:
            found.append((m.start(), d))
        if m.group(2):
            d2 = _make_date(int(m.group(2)), month, today)
            if d2:
                found.append((m.start(2), d2))
    for word, offset in (("послезавтра", 2), ("завтра", 1), ("сегодня", 0)):
        idx = t.find(word)
        if idx >= 0:
            found.append((idx, today + timedelta(days=offset)))
            if word == "послезавтра":
                t = t.replace("послезавтра", " " * len("послезавтра"))
    for prefix, wd in WEEKDAYS.items():
        m = re.search(r"\b(следующ[а-я]*\s+)?(в\s+|во\s+)?" + prefix + r"[а-я]*", t)
        if m:
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            if m.group(1) and delta < 7:
                delta += 7
            found.append((m.start(), today + timedelta(days=delta)))
    found.sort(key=lambda x: x[0])
    result: list[date] = []
    for _, d in found:
        if d not in result:
            result.append(d)
    return result


def parse_dates(text: str, today: date | None = None) -> tuple[date, date] | None:
    """Возвращает диапазон дат (с, по) из фразы на русском или None."""
    today = today or date.today()
    dates = _single_dates(text, today)
    if not dates:
        return None
    if len(dates) == 1:
        return dates[0], dates[0]
    start, end = dates[0], dates[1]
    if end < start:
        start, end = end, start
    return start, end


def fmt_date(d: date) -> str:
    return f"{WEEKDAY_SHORT[d.weekday()]}, {d.day} {MONTH_GEN[d.month - 1]}"


def fmt_range(start: date, end: date) -> str:
    if start == end:
        return fmt_date(start)
    return f"{fmt_date(start)} — {fmt_date(end)}"


def workdays_between(start: date, end: date) -> int:
    days = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days
