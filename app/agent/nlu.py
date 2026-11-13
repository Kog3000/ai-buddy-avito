"""Определение намерения и извлечение параметров из сообщения стажёра (детерминированно, без LLM)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Resource
from ..textutil import has_any, has_phrase, normalize, parse_dates

INTENTS = ("cancel", "sick", "escalate", "status", "absence", "access", "chats", "meeting", "goal", "next_steps",
           "who_mentor", "who_lead", "schedule", "overview", "prep_1on1", "meeting_requests", "greeting", "thanks",
           "kb")


@dataclass
class Intent:
    name: str
    slots: dict = field(default_factory=dict)


LEVEL_WORDS = {
    "admin": ["админ", "administr", "admin"],
    "edit": ["редакт", "запис", "измен", "edit", "write", "push", "пуш", "разработ"],
    "read": ["чтени", "читат", "просмотр", "read", "посмотр"],
}


def detect_level(text: str) -> str | None:
    for level, stems in LEVEL_WORDS.items():
        if has_any(text, stems):
            return level
    return None


def detect_resources(text: str) -> list[str]:
    t = normalize(text)
    words = re.findall(r"[a-zа-я0-9-]+", t)
    found = []
    for res in Resource.query.order_by(Resource.id).all():
        aliases = [a for a in res.aliases.split() if a] + [res.code]
        for alias in aliases:
            hit = any(w.startswith(alias) for w in words) if len(alias) >= 4 else alias in words
            if hit:
                found.append(res.code)
                break
    return found


def detect_absence_kind(text: str) -> str:
    if has_any(text, ["отпуск"]):
        return "vacation"
    if has_phrase(text, ["из дома", "удален", "удалён", "удаленк", "удалённ"]):
        return "remote"
    return "day_off"


def extract_after_colon(text: str) -> str:
    return text.split(":", 1)[1].strip() if ":" in text else ""


def detect(text: str, state: dict | None = None, staff: bool = False) -> Intent:
    state = state or {}
    flow = state.get("flow")
    t = normalize(text).strip()

    if flow and (t in {"отмена", "отменить", "стоп", "не надо", "нет", "передумал", "передумала"}
                 or has_phrase(t, ["отмени заявку", "не нужно уже"])):
        return Intent("cancel")
    if flow == "absence" and state.get("stage") == "dates":
        dates = parse_dates(text)
        if dates:
            return Intent("absence", {"dates": dates, "kind": state.get("kind", "day_off")})
    if flow == "access" and state.get("stage") == "resource":
        res = detect_resources(text)
        if res:
            return Intent("access", {"resources": res, "level": detect_level(text)})
    if flow == "goal" and state.get("stage") == "title" and len(t) > 3 and "?" not in t:
        return Intent("goal", {"title": text.strip()})

    if staff:
        if has_phrase(t, ["сводк", "как дела у стаж", "мои стажёры", "мои стажеры", "что со стаж", "прогресс стаж"]):
            return Intent("overview")
        if has_any(t, ["подготов", "повестк"]) and has_phrase(t, ["1:1", "1-1", "встреч", "повестк"]):
            return Intent("prep_1on1")
        if has_phrase(t, ["на подтверждении", "запросы встреч", "предложения встреч", "заявки на встреч"]):
            return Intent("meeting_requests")
    if has_any(t, ["больнич", "заболел", "болею", "температур"]):
        return Intent("sick")
    if has_phrase(t, ["спроси наставника", "спросить наставника", "передай наставнику", "вопрос наставнику",
                      "передать наставнику", "спроси у наставника"]):
        return Intent("escalate", {"question": extract_after_colon(text)})
    if has_phrase(t, ["мои заявки", "статус заяв", "статус доступ", "что с заявк", "что с доступ",
                      "одобрили ли", "статус моих"]):
        return Intent("status")
    wants_vacation = has_any(t, ["отпуск"]) and has_any(t, ["оформ", "взя", "возьм", "хоч", "уйт", "запрос",
                                                              "планир", "нуж", "сходит", "поех"])
    if wants_vacation or has_any(t, ["отгул", "отпрос", "dayoff"]) or has_phrase(
            t, ["не смогу прийти", "не приду", "не выйду", "взять выходной", "работать из дома", "поработать из дома"]):
        return Intent("absence", {"kind": detect_absence_kind(t), "dates": parse_dates(text)})
    if has_any(t, ["чат", "канал"]) and has_any(t, ["добав", "вступ", "подпиш", "подключ"]):
        return Intent("chats")
    resources = detect_resources(text)
    wants_access = has_any(t, ["доступ", "прав", "выдай", "подключ", "открой", "запрос"])
    if wants_access and (resources or has_any(t, ["доступ"])):
        return Intent("access", {"resources": resources, "level": detect_level(text),
                                 "bundle": has_any(t, ["основн", "базов", "все"]) and not resources})
    if has_phrase(t, ["запланир", "назнач", "созвон", "забронир", "встретиться", "1:1", "1-1", "one-on-one"]) and \
            has_any(t, ["встреч", "наставник", "созвон", "1"]):
        return Intent("meeting")
    if has_any(t, ["цел", "трекер"]) and has_any(t, ["добав", "запиш", "заполн", "внес", "постав"]):
        return Intent("goal", {"title": extract_after_colon(text)})
    if has_phrase(t, ["что дальше", "следующ", "что делать", "что мне делать", "мой прогресс", "мои задачи",
                      "чек-лист", "чеклист", "на какой я неделе", "какая у меня неделя", "какой этап", "на каком я этапе", "план адаптации"]):
        return Intent("next_steps")
    if has_phrase(t, ["мой наставник", "моего наставника", "мой бадди", "кто наставник"]):
        return Intent("who_mentor")
    if has_phrase(t, ["мой руководитель", "моего руководителя", "кто руководитель", "мой лид", "кто лид"]):
        return Intent("who_lead")
    if has_phrase(t, ["мои встречи", "расписание", "когда встреча", "ближайшие встречи", "ближайшие события",
                      "что у меня сегодня", "что у меня завтра"]):
        return Intent("schedule")
    if re.fullmatch(r"(привет|здравствуй|здравствуйте|добрый день|доброе утро|хай|hi|hello)[!. ]*", t):
        return Intent("greeting")
    if re.fullmatch(r"(спасибо|спс|благодарю|супер|отлично|понятно|ок|окей)[!. ]*", t):
        return Intent("thanks")
    return Intent("kb")


# Темы обращений к наставнику (статистика Авито): по ним считаем, какую нагрузку снимает AI-buddy
CATEGORIES = {
    "org": "Организационные (доступы, оборудование, документы)",
    "tasks": "По задачам (формулировка, ревью, приоритизация)",
    "professional": "Профессиональные (инструменты, технологии)",
    "growth": "Развитие (ИПР, матрица компетенций)",
    "feedback": "Обратная связь",
    "other": "Другое",
}
_CAT_WORDS = [
    ("feedback", ["обратн", "фидбек", "feedback", "опрос"]),
    ("growth", ["ипр", "развит", "компетенц", "матриц", "цел", "экватор", "выпускн", "оффер", "карьер", "трекер"]),
    ("tasks", ["задач", "ревью", "review", "приоритет", "приоритиз", "формулир", "тикет", "бэклог", "mr", "спринт",
               "дедлайн", "срок", "оценк"]),
    ("org", ["доступ", "прав", "оборудован", "ноутбук", "техник", "документ", "отгул", "отпуск", "пропуск", "vpn",
             "впн", "заявк", "офис", "график", "зарплат", "выплат", "больнич", "встреч", "каналы", "чат"]),
    ("professional", ["react", "typescript", "git", "stash", "grafana", "redash", "sentry", "kafka", "ci", "тест",
                      "стек", "код", "api", "архитектур", "инструмент", "технолог", "развернуть", "локально", "ide",
                      "релиз", "мониторинг", "дизайн-систем"]),
]
_ORG_INTENTS = {"access", "absence", "meeting", "status", "sick", "schedule", "who_mentor", "who_lead", "chats",
                "meeting_requests"}


def categorize(text: str, intent: str = "kb") -> str:
    """Относит вопрос к одной из тем обращений к наставнику."""
    if intent in _ORG_INTENTS:
        return "org"
    if intent == "goal":
        return "growth"
    if intent in {"greeting", "thanks", "cancel", "overview", "prep_1on1"}:
        return "other"
    for category, stems in _CAT_WORDS:
        if has_any(text, stems):
            return category
    return "other"
