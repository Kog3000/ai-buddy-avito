"""Оркестратор AI-buddy.

Принципы (см. specs/agent/spec.md):
- все обращения к корпоративным сервисам идут через MCP-инструменты от имени текущего пользователя;
- любое изменяющее действие выполняется только после явного подтверждения в карточке;
- в контекст ответа (в т.ч. LLM) попадают только материалы, доступные пользователю;
- при сбое LLM ответ формируется офлайн (retrieval), интерфейс не блокируется.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..analytics import flag, track
from ..mcp.registry import ToolError
from ..mcp.server import MCPClient
from ..models import ChatMessage, ConversationState, User, db
from ..permissions import PermissionDenied
from ..seed_data import LEVEL_LABELS
from ..services import absence as absence_svc
from ..services import access as access_svc
from ..textutil import fmt_range, workdays_between
from . import llm, nlu
from .prompts import SYSTEM_PROMPT, build_context, build_materials

log = logging.getLogger(__name__)

KB_MIN_SCORE = 4.5
STAFF_SUGGESTIONS = ["Сводка по стажёрам", "Встречи на подтверждении", "Памятка наставника"]
DISCLAIMER = "Материалы базы знаний прототипа тестовые и не являются регламентами Авито."


@dataclass
class Reply:
    text: str
    cards: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def _state(user: User) -> ConversationState:
    row = db.session.get(ConversationState, user.id)
    if row is None:
        row = ConversationState(user_id=user.id, data="{}")
        db.session.add(row)
    return row


def _set_state(user: User, value: dict | None) -> None:
    _state(user).set(value or {})
    db.session.commit()


def _source(slug: str, title: str) -> dict:
    return {"slug": slug, "title": title, "url": f"/kb/topic/{slug}"}


def _cancel_action() -> dict:
    return {"label": "Отмена", "action": "cancel", "params": {}, "style": "secondary"}


class Agent:
    def __init__(self, user: User):
        self.user = user
        self.mcp = MCPClient(user)

    # ---------------------------------------------------------------- точки входа
    def handle_message(self, text: str, attachments: list[dict] | None = None) -> Reply:
        state = _state(self.user).get()
        attachments = attachments or []
        if attachments and not text.strip():
            return self._on_file_only(attachments)
        intent = nlu.detect(text, state, staff=self.user.role in {"mentor", "lead"})
        handler = getattr(self, f"_on_{intent.name}")
        try:
            reply = handler(text, intent.slots, state)
        except PermissionDenied as exc:
            reply = Reply(f"Это действие мне недоступно: {exc}.", suggestions=["Что дальше?"])
        except ToolError as exc:
            reply = Reply(f"Сервис вернул ошибку: {exc}", suggestions=["Что дальше?"])
        if attachments:
            self._offer_file_escalation(reply, text, attachments[0])
        reply.meta.setdefault("intent", intent.name)
        reply.meta["tools"] = self.mcp.calls
        reply.meta["category"] = nlu.categorize(text, intent.name)
        track(self.user, "chat_message", intent=intent.name, source=reply.meta.get("source", intent.name),
              category=reply.meta["category"])
        return reply

    def handle_action(self, action: str, params: dict) -> Reply:
        handler = getattr(self, f"_act_{action}", None)
        if handler is None:
            return Reply("Не понял действие — попробуй ещё раз.")
        try:
            reply = handler(params or {})
        except PermissionDenied as exc:
            reply = Reply(f"Это действие мне недоступно: {exc}.", meta={"error": True})
        except ToolError as exc:
            reply = Reply(f"Не получилось: {exc}", suggestions=["Что дальше?"], meta={"error": True})
        except (ValueError, KeyError) as exc:
            reply = Reply(f"Проверь введённые данные: {exc}", meta={"error": True})
        reply.meta.setdefault("action", action)
        reply.meta["tools"] = self.mcp.calls
        track(self.user, "chat_action", action=action, ok=not reply.meta.get("error"))
        return reply

    # ---------------------------------------------------------------- простые намерения
    def _on_greeting(self, text, slots, state) -> Reply:
        if self.user.role in {"mentor", "lead"}:
            return Reply(f"Привет, {self.user.first_name}! Подготовлю сводку по стажёрам, повестку 1:1 и найду материалы "
                         f"для наставников.", suggestions=STAFF_SUGGESTIONS)
        return Reply(f"Привет, {self.user.first_name}! Я AI-buddy. Помогу найти материалы, оформить заявки "
                     f"и подскажу следующий шаг адаптации.",
                     suggestions=["Что дальше?", "Где хранятся документы нашей команды?", "Нужен доступ к Stash"])

    def _on_thanks(self, text, slots, state) -> Reply:
        return Reply("Обращайся! Если что-то понадобится — я здесь.", suggestions=["Что дальше?"])

    def _on_cancel(self, text, slots, state) -> Reply:
        _set_state(self.user, None)
        return Reply("Хорошо, отменил. Чем ещё помочь?", suggestions=["Что дальше?"])

    def _act_cancel(self, params) -> Reply:
        return self._on_cancel("", {}, {})

    def _on_sick(self, text, slots, state) -> Reply:
        return Reply("Выздоравливай! Сообщи руководителю и наставнику, что заболел(а). Больничный оформляется через "
                     "HR — я не обрабатываю медицинские данные.",
                     sources=[_source("faq-sick", "Если заболел")], suggestions=["К кому обращаться с вопросами?"])

    def _on_who_mentor(self, text, slots, state) -> Reply:
        profile = self.mcp.call("profile_get")
        if not profile["mentor"]:
            return Reply("Наставник пока не назначен — уточни у руководителя.")
        mentor = self.user.mentor
        events = [e for e in self.mcp.call("calendar_upcoming") if e["kind"] == "mentor"]
        text = f"Твой наставник — **{mentor.full_name}**, {mentor.position} ({mentor.team})."
        if events:
            text += f" Ближайшая встреча: «{events[0]['title']}», {events[0]['when']}."
        return Reply(text, suggestions=["Запланируй встречу с наставником", "Как работать с наставником?"])

    def _on_who_lead(self, text, slots, state) -> Reply:
        lead = self.user.lead
        if lead is None:
            return Reply("Руководитель не указан в профиле.")
        return Reply(f"Твой руководитель — **{lead.full_name}**, {lead.position}. С ним(ней) согласуются цели "
                     f"адаптации и отсутствия.", suggestions=["Как поставить цели на адаптацию?"])

    def _on_schedule(self, text, slots, state) -> Reply:
        events = self.mcp.call("calendar_upcoming")
        if not events:
            return Reply("Ближайших событий нет.", suggestions=["Запланируй встречу с наставником"])
        lines = "\n".join(f"- {e['when']} — {e['title']}" for e in events[:5])
        return Reply(f"Ближайшие события:\n{lines}", suggestions=["Запланируй встречу с наставником"])

    # ---------------------------------------------------------------- этапы адаптации
    def _on_next_steps(self, text, slots, state) -> Reply:
        if self.user.role != "intern":
            return self._on_overview(text, slots, state)
        data = self.mcp.call("checklist_get")
        steps = self.mcp.call("checklist_next_steps", limit=3)
        stage = data["weeks"][data["current_week"] - 1]
        if not steps:
            return Reply("Все задачи чек-листа выполнены — отличная работа! Обсуди итоги с наставником.")
        items = [{"title": s["title"], "week": s["week"], "period": s["period"], "task_id": s["id"],
                  "prompt": s["chat_prompt"],
                  "link": f"/kb/topic/{s['kb_topic']}" if s["kb_topic"] else None, "auto": s["auto"]}
                 for s in steps]
        text = (f"Сейчас этап «{stage['period']}. {stage['title']}». Выполнено {data['done']} из {data['total']} задач "
                f"({data['percent']}%). Следующие шаги:")
        suggestions = [s["chat_prompt"] for s in steps if s["chat_prompt"]][:3]
        return Reply(text, cards=[{"type": "steps", "title": "Следующие шаги", "items": items}],
                     suggestions=suggestions)

    def _act_task_done(self, params) -> Reply:
        task_id = int(params["task_id"])
        self.mcp.call("checklist_set_task", task_id=task_id, done=True)
        data = self.mcp.call("checklist_get")
        return Reply(f"Отметил задачу в чек-листе. Прогресс: {data['done']} из {data['total']} ({data['percent']}%).",
                     suggestions=["Что дальше?"])

    # ---------------------------------------------------------------- база знаний
    def _related_task_card(self, slug: str) -> list:
        if self.user.role != "intern":
            return []
        for step in self.mcp.call("checklist_next_steps", limit=28):
            if step["kb_topic"] == slug and not step["auto"]:
                return [{"type": "confirm", "title": "Задача из чек-листа",
                         "rows": [["Задача", step["title"]], ["Этап", f"{step['period']}. {step['week_title']}"]],
                         "actions": [{"label": "Отметить выполненной", "action": "task_done",
                                      "params": {"task_id": step["id"]}, "style": "primary"}]}]
        return []

    def _on_kb(self, text, slots, state) -> Reply:
        if state.get("flow"):
            _set_state(self.user, None)
        hits = self.mcp.call("kb_search", query=text, limit=3)
        good = [h for h in hits if h["score"] >= KB_MIN_SCORE]
        if not good:
            return self._not_found(text)
        top = good[0]
        topics = [self.mcp.call("kb_get_topic", slug=h["slug"]) for h in good[:2]]
        sources = [_source(h["slug"], h["title"]) for h in good]
        suggestions = [h["title"] for h in good[1:3]]
        meta = {"source": "kb", "score": top["score"]}
        answer = None
        if llm.enabled():
            try:
                answer = self._llm_answer(text, topics)
                meta["source"] = "llm"
            except llm.LLMError as exc:
                log.warning("LLM fallback: %s", exc)
                meta["llm_fallback"] = str(exc)
        if answer is None:
            answer = f"**{topics[0]['title']}**\n\n{topics[0]['body']}"
            if len(good) > 1:
                answer += f"\n\nТакже может пригодиться: «{good[1]['title']}»."
        return Reply(answer, cards=self._related_task_card(top["slug"]), sources=sources,
                     suggestions=suggestions or ["Что дальше?"], meta=meta)

    def _llm_answer(self, question: str, topics: list[dict]) -> str:
        profile = self.mcp.call("profile_get")
        steps = self.mcp.call("checklist_next_steps", limit=3) if self.user.role == "intern" else []
        history = (ChatMessage.query.filter_by(user_id=self.user.id).order_by(ChatMessage.id.desc()).limit(6).all())
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "system", "content": "Контекст стажёра: " + build_context(profile, steps)},
                    {"role": "system", "content": "Материалы базы знаний:\n" + build_materials(topics)}]
        for msg in reversed(history):
            if msg.text and msg.text != question:
                messages.append({"role": msg.role, "content": msg.text[:800]})
        messages.append({"role": "user", "content": question})
        return llm.chat(messages)

    def _not_found(self, question: str) -> Reply:
        meta = {"source": "not_found"}
        if self.user.role != "intern" or not flag("mentor_escalation") or not self.user.mentor:
            return Reply("Не нашёл ответа в базе знаний. Попробуй переформулировать вопрос.", meta=meta)
        mentor = self.user.mentor
        return Reply(
            "В базе знаний нет точного ответа на этот вопрос. Могу передать его наставнику — "
            f"{mentor.first_name} ответит, а ответ придёт сюда в чат.",
            cards=[self._escalate_card(question)], suggestions=["Что дальше?"], meta=meta)

    def _escalate_card(self, question: str, attachment: dict | None = None) -> dict:
        mentor = self.user.mentor
        params = {"attachment_id": attachment["id"]} if attachment else {}
        rows = [["Файл", attachment["name"]]] if attachment else []
        return {"type": "confirm", "title": f"Вопрос наставнику: {mentor.full_name}", "rows": rows,
                "fields": [{"name": "question", "label": "Вопрос", "type": "textarea", "value": question}],
                "actions": [{"label": "Передать наставнику", "action": "escalate_confirm", "params": params,
                             "style": "primary"}, _cancel_action()]}

    def _on_file_only(self, attachments: list[dict]) -> Reply:
        file = attachments[0]
        track(self.user, "chat_message", intent="file", source="file", category="other")
        if self.user.role != "intern" or not self.user.mentor:
            return Reply(f"Файл «{file['name']}» сохранён в чате.")
        return Reply(f"Получил файл «{file['name']}». Я не разбираю содержимое вложений, но могу передать его "
                     f"наставнику вместе с вопросом — опиши, что нужно.",
                     cards=[self._escalate_card("", file)], meta={"source": "file"})

    def _offer_file_escalation(self, reply: Reply, text: str, file: dict) -> None:
        if self.user.role != "intern" or not self.user.mentor:
            return
        for card in reply.cards:
            if any(a.get("action") == "escalate_confirm" for a in card.get("actions", [])):
                card["rows"] = [["Файл", file["name"]]]
                card["actions"][0]["params"] = {"attachment_id": file["id"]}
                return
        reply.text += f"\n\nК сообщению приложен файл «{file['name']}» — если нужно, передам его наставнику."
        reply.cards.append(self._escalate_card(text, file))

    def _on_escalate(self, text, slots, state) -> Reply:
        if self.user.role != "intern" or not self.user.mentor:
            return Reply("Передать вопрос наставнику можно только из аккаунта стажёра.")
        question = slots.get("question") or ""
        return Reply("Проверь формулировку — и я передам вопрос.", cards=[self._escalate_card(question)])

    def _act_escalate_confirm(self, params) -> Reply:
        args = {"question": str(params.get("question", ""))}
        if params.get("attachment_id"):
            args["attachment_id"] = int(params["attachment_id"])
        result = self.mcp.call("mentor_ask", **args)
        return Reply(f"Передал вопрос наставнику ({result['mentor']}). Пришлю ответ сюда, как только он появится.",
                     suggestions=["Что дальше?"], meta={"source": "escalated"})

    # ---------------------------------------------------------------- заявки: статус
    def _on_status(self, text, slots, state) -> Reply:
        acc = self.mcp.call("access_my_requests")
        abs_ = self.mcp.call("absence_my_requests")
        if not acc and not abs_:
            return Reply("У тебя пока нет заявок.", suggestions=["Помоги настроить доступы к основным сервисам"])
        cards = []
        for r in acc[:4]:
            cards.append({"type": "request_status", "title": f"Заявка №A-{r['id']}: {r['resource']}",
                          "status": r["status"], "status_label": r["status_label"],
                          "rows": [["Уровень", r["level_label"]], ["Согласующий", r["approver"]]], "link": "/access"})
        for r in abs_[:3]:
            cards.append({"type": "request_status", "title": f"Заявка №O-{r['id']}: {r['kind_label']}",
                          "status": r["status"], "status_label": r["status_label"],
                          "rows": [["Даты", r["period"]], ["Согласующий", r["approver"]]], "link": "/access?tab=absence"})
        pending = sum(1 for r in acc + abs_ if r["status"] == "pending_approval")
        return Reply(f"Заявок всего: {len(acc) + len(abs_)}, на согласовании: {pending}.", cards=cards,
                     suggestions=["Что дальше?"])

    # ---------------------------------------------------------------- сценарий: доступы
    def _default_justification(self) -> str:
        base = f"Стажировка в команде «{self.user.team}»"
        if self.user.role != "intern":
            return base
        from ..seed_data import CHECKLIST
        period, title, _ = CHECKLIST[self.user.stage_number()]
        return f"{base}: задачи этапа «{period}. {title}»"

    def _on_access(self, text, slots, state) -> Reply:
        resources = self.mcp.call("access_list_resources")
        by_code = {r["code"]: r for r in resources}
        codes = slots.get("resources") or []
        if slots.get("bundle") or len(codes) > 1:
            if not codes:
                codes = [r["code"] for r in resources if r["base"]]
            return self._access_bundle([by_code[c] for c in codes])
        if not codes:
            _set_state(self.user, {"flow": "access", "stage": "resource"})
            names = [r["name"].split(":")[0] for r in resources if r["levels"]][:6]
            return Reply("Доступы запрашиваются через сервис заявок: нужно указать ресурс, уровень прав и "
                         "обоснование — заявку я заполню за тебя. К какому сервису нужен доступ?",
                         sources=[_source("access-how-to-request", "Как запросить доступ к сервису")],
                         suggestions=[f"Доступ к {n}" for n in names[:4]] + ["Основные сервисы"])
        return self._access_single(by_code[codes[0]], slots.get("level"))

    def _access_single(self, res: dict, level: str | None) -> Reply:
        if res["active_request"]:
            req = res["active_request"]
            _set_state(self.user, None)
            return Reply(f"По «{res['name']}» уже есть заявка — {req['status_label'].lower()}.",
                         cards=[{"type": "request_status", "title": f"Заявка №A-{req['id']}: {req['resource']}",
                                 "status": req["status"], "status_label": req["status_label"],
                                 "rows": [["Уровень", req["level_label"]], ["Согласующий", req["approver"]]],
                                 "link": "/access"}], suggestions=["Статус моих заявок"])
        if not res["levels"]:
            _set_state(self.user, None)
            return Reply(f"Доступ к «{res['name']}» участникам команды не выдаётся — он есть только у дежурных SRE. "
                         f"Если данные нужны для задачи — обсуди с наставником, как получить их другим способом.",
                         suggestions=["Спроси наставника: нужен ли мне доступ к " + res["name"]],
                         meta={"denied": True})
        note = ""
        if level and level not in res["levels"]:
            note = (f"Уровень «{LEVEL_LABELS[level]}» выдаётся только владельцам проектов — предлагаю "
                    f"«{LEVEL_LABELS[res['recommended']]}». ")
            level = None
        level = level or res["recommended"]
        resource = access_svc.get_resource(res["code"])
        confidential = res["code"] == "strategy"
        if confidential:
            note += ("Это конфиденциальные данные: доступ выдаётся, если их требует задача. Укажи в обосновании, "
                     "для какой задачи они нужны. ")
        approver = ("Автоматически по политике" if resource.approver_rule == "policy"
                    else access_svc._approver(self.user, resource)[1])
        _set_state(self.user, {"flow": "access", "stage": "confirm", "resource": res["code"]})
        card = {"type": "confirm", "title": f"Заявка на доступ: {res['name']}",
                "rows": [["Ресурс", res["name"]], ["Согласующий", approver]],
                "fields": [
                    {"name": "level", "label": "Уровень прав", "type": "select", "value": level,
                     "options": [[lv, LEVEL_LABELS[lv]] for lv in res["levels"]]},
                    {"name": "project", "label": "Проект", "type": "text", "value": res["name"]},
                    {"name": "justification", "label": "Обоснование", "type": "textarea",
                     "value": "" if confidential else self._default_justification()}],
                "actions": [{"label": "Отправить заявку", "action": "access_confirm",
                             "params": {"resource": res["code"]}, "style": "primary"}, _cancel_action()],
                "note": "Заявка уйдёт в сервис доступов только после подтверждения."}
        return Reply(note + "Подготовил заявку — проверь и отправь.", cards=[card])

    def _access_bundle(self, items: list[dict]) -> Reply:
        todo = [r for r in items if r["levels"] and not r["active_request"]]
        skipped = [r for r in items if r not in todo]
        if not todo:
            _set_state(self.user, None)
            return Reply("По всем этим сервисам заявки уже есть или доступ стажёрам не выдаётся.",
                         suggestions=["Статус моих заявок"])
        _set_state(self.user, {"flow": "access", "stage": "confirm_bundle"})
        rows = [[r["name"], LEVEL_LABELS[r["recommended"]]] for r in todo]
        card = {"type": "confirm", "title": f"Заявки на доступ: {len(todo)} шт.", "rows": rows,
                "fields": [{"name": "justification", "label": "Обоснование", "type": "textarea",
                            "value": self._default_justification()}],
                "actions": [{"label": "Отправить все заявки", "action": "access_confirm_bundle",
                             "params": {"items": [{"resource": r["code"], "level": r["recommended"]} for r in todo]},
                             "style": "primary"}, _cancel_action()],
                "note": "Уровни выбраны минимально достаточные для задач стажёра. Нужен другой — запроси ресурс отдельно."}
        text = "Подготовил заявки на основные сервисы — они нужны в первую неделю."
        if skipped:
            text += " Пропустил: " + ", ".join(r["name"] for r in skipped) + " — по ним заявка уже есть."
        return Reply(text, cards=[card], sources=[_source("access-base-services", "Основные сервисы на первой неделе")])

    def _access_result_text(self, req: dict) -> str:
        if req["approver"].startswith("Автоматически"):
            return "одобряется автоматически по политике — обычно в течение пары минут"
        return f"согласует {req['approver']}"

    def _act_access_confirm(self, params) -> Reply:
        result = self.mcp.call("access_create_request", resource=params["resource"], level=params["level"],
                               justification=params.get("justification", ""), project=params.get("project", ""))
        _set_state(self.user, None)
        req = result["request"]
        card = {"type": "request_status", "title": f"Заявка №A-{req['id']}: {req['resource']}",
                "status": req["status"], "status_label": req["status_label"],
                "rows": [["Уровень", req["level_label"]], ["Согласующий", req["approver"]]], "link": "/access"}
        if result["duplicate"]:
            return Reply("Такая заявка уже есть — новую не создавал.", cards=[card], suggestions=["Статус моих заявок"])
        return Reply(f"Заявка отправлена в сервис доступов. Её {self._access_result_text(req)}. "
                     f"Напишу, когда статус изменится.", cards=[card], suggestions=["Статус моих заявок", "Что дальше?"],
                     meta={"source": "action", "request_id": req["id"]})

    def _act_access_confirm_bundle(self, params) -> Reply:
        cards, errors = [], []
        for item in params.get("items", []):
            try:
                result = self.mcp.call("access_create_request", resource=item["resource"], level=item["level"],
                                       justification=params.get("justification", ""))
            except (ToolError, PermissionDenied) as exc:
                errors.append(f"{item['resource']}: {exc}")
                continue
            req = result["request"]
            cards.append({"type": "request_status", "title": f"Заявка №A-{req['id']}: {req['resource']}",
                          "status": req["status"], "status_label": req["status_label"],
                          "rows": [["Уровень", req["level_label"]], ["Согласующий", req["approver"]]],
                          "link": "/access"})
        _set_state(self.user, None)
        text = f"Отправил заявок: {len(cards)}. Когда все основные доступы выдадут, задача в чек-листе закроется сама."
        if errors:
            text += " Не получилось: " + "; ".join(errors)
        return Reply(text, cards=cards, suggestions=["Статус моих заявок", "Что дальше?"], meta={"source": "action"})

    # ---------------------------------------------------------------- сценарий: отгул
    def _on_absence(self, text, slots, state) -> Reply:
        kind = slots.get("kind", "day_off")
        policy = self.mcp.call("absence_policy", kind=kind)
        sources = [_source("faq-day-off" if kind == "day_off" else "faq-vacation",
                           "Как оформить отгул" if kind == "day_off" else "Как оформить отпуск")]
        if self.user.role != "intern" or not flag("absence_requests"):
            return Reply(f"{policy['text']} Оформить заявку можно в сервисе отсутствий.", sources=sources)
        dates = slots.get("dates")
        if not dates:
            _set_state(self.user, {"flow": "absence", "stage": "dates", "kind": kind})
            label = absence_svc.KIND_LABELS[kind].lower()
            return Reply(
                f"Подскажу порядок и помогу подготовить запрос. {policy['text']}\n\n"
                f"Наставнику не нужно писать отдельно — я отправлю ему уведомление. На какую дату оформить {label}?",
                sources=sources, suggestions=["Завтра", "В пятницу", "В понедельник", "С 1 по 2 декабря"])
        start, end = dates
        try:
            absence_svc.validate(self.user, kind, start, end)
        except ValueError as exc:
            _set_state(self.user, {"flow": "absence", "stage": "dates", "kind": kind})
            return Reply(f"Не получится оформить на эти даты: {exc}. Выбери другие даты.", sources=sources,
                         suggestions=["В пятницу", "В понедельник", "Отмена"], meta={"error": True})
        _set_state(self.user, {"flow": "absence", "stage": "confirm", "kind": kind})
        days = workdays_between(start, end)
        card = {"type": "confirm", "title": f"Заявка: {absence_svc.KIND_LABELS[kind]}",
                "rows": [["Даты", fmt_range(start, end)], ["Рабочих дней", str(days)],
                         ["Согласующий", f"{self.user.lead.full_name}, руководитель"]],
                "fields": [{"name": "comment", "label": "Комментарий (необязательно)", "type": "text", "value": ""},
                           {"name": "notify_mentor", "label": f"Уведомить наставника ({self.user.mentor.full_name})",
                            "type": "checkbox", "value": True}],
                "actions": [{"label": "Отправить заявку", "action": "absence_confirm",
                             "params": {"kind": kind, "date_from": start.isoformat(), "date_to": end.isoformat()},
                             "style": "primary"}, _cancel_action()],
                "note": "Заявка уйдёт в сервис отсутствий только после подтверждения."}
        return Reply("Подготовил заявку — проверь и отправь.", cards=[card], sources=sources)

    def _act_absence_confirm(self, params) -> Reply:
        req = self.mcp.call("absence_create_request", kind=params["kind"], date_from=params["date_from"],
                            date_to=params["date_to"], comment=str(params.get("comment", "")),
                            notify_mentor=bool(params.get("notify_mentor", True)))
        _set_state(self.user, None)
        card = {"type": "request_status", "title": f"Заявка №O-{req['id']}: {req['kind_label']}",
                "status": req["status"], "status_label": req["status_label"],
                "rows": [["Даты", req["period"]], ["Согласующий", req["approver"]]], "link": "/access?tab=absence"}
        text = f"Заявка отправлена руководителю ({req['approver']}). Сообщу о решении здесь."
        if params.get("notify_mentor", True):
            text += " Наставник получил уведомление."
        text += " Если отгул влияет на сроки задач — обсуди перенос с наставником лично."
        return Reply(text, cards=[card], suggestions=["Статус моих заявок", "Что дальше?"], meta={"source": "action"})

    # ---------------------------------------------------------------- сценарий: встреча с наставником
    def _agenda(self) -> str:
        since = datetime.now() - timedelta(days=7)
        rows = (ChatMessage.query.filter(ChatMessage.user_id == self.user.id, ChatMessage.role == "user",
                                         ChatMessage.created_at >= since)
                .order_by(ChatMessage.id.desc()).limit(20).all())
        questions = [m.text.strip() for m in rows if m.text.strip().endswith("?")][:3]
        return "\n".join(f"- {q}" for q in questions) if questions else "- Вопросы по первым задачам"

    def _on_meeting(self, text, slots, state) -> Reply:
        if self.user.role != "intern" or not self.user.mentor:
            return Reply("Назначить встречу с наставником можно из аккаунта стажёра.")
        slots_ = self.mcp.call("calendar_free_slots", days=5)
        if not slots_:
            return Reply("Не нашёл общих свободных слотов на ближайшую неделю. Напиши наставнику напрямую.")
        _set_state(self.user, {"flow": "meeting", "stage": "slot"})
        card = {"type": "slots", "title": f"Свободное время у тебя и {self.user.mentor.full_name}",
                "slots": [{"label": s["label"], "start": s["start"]} for s in slots_], "action": "meeting_pick"}
        return Reply("Нашёл общие свободные слоты в календаре. Выбери удобный:", cards=[card])

    def _act_meeting_pick(self, params) -> Reply:
        start = datetime.fromisoformat(params["start"])
        from ..services.calendar import fmt_dt
        _set_state(self.user, {"flow": "meeting", "stage": "confirm"})
        card = {"type": "confirm", "title": "Встреча с наставником",
                "rows": [["Когда", f"{fmt_dt(start)} - {(start + timedelta(hours=1)):%H:%M}"],
                         ["Участник", self.user.mentor.full_name]],
                "fields": [{"name": "title", "label": "Тема", "type": "text", "value": "Встреча 1:1 с наставником"},
                           {"name": "agenda", "label": "Повестка (собрал из твоих вопросов за неделю)",
                            "type": "textarea", "value": self._agenda()}],
                "actions": [{"label": "Запланировать", "action": "meeting_confirm", "params": {"start": params["start"]},
                             "style": "primary"}, _cancel_action()],
                "note": "Наставник получит приглашение и подтвердит время."}
        return Reply("Проверь детали встречи:", cards=[card])

    def _act_meeting_confirm(self, params) -> Reply:
        meeting = self.mcp.call("calendar_request_mentor_meeting", start=params["start"],
                                title=str(params.get("title") or "Встреча 1:1 с наставником"),
                                agenda=str(params.get("agenda", "")))
        _set_state(self.user, None)
        card = {"type": "request_status", "title": meeting["title"], "status": "pending_approval",
                "status_label": meeting["status_label"], "rows": [["Когда", meeting["when"]],
                                                                  ["Участник", self.user.mentor.full_name]],
                "link": "/mentor", "meeting_id": meeting["id"]}
        return Reply("Отправил приглашение наставнику. Когда он подтвердит время, встреча появится в календаре — "
                     "я напишу.", cards=[card], suggestions=["Что дальше?"], meta={"source": "action"})

    # ---------------------------------------------------------------- трекер целей
    def _on_goal(self, text, slots, state) -> Reply:
        if self.user.role != "intern":
            return Reply("Трекер целей ведут стажёры.")
        title = slots.get("title")
        if not title:
            _set_state(self.user, {"flow": "goal", "stage": "title"})
            return Reply("Цели адаптации согласуются с руководителем с учётом OKR команды. Напиши цель одной фразой — "
                         "например: «Закрыть 2 задачи из бэклога до конца месяца».",
                         sources=[_source("growth-goals", "Цели на период адаптации")],
                         suggestions=["Закрыть 2 задачи из бэклога до конца месяца", "Отмена"])
        _set_state(self.user, {"flow": "goal", "stage": "confirm"})
        card = {"type": "confirm", "title": "Новая цель в трекере",
                "fields": [{"name": "title", "label": "Цель", "type": "text", "value": title},
                           {"name": "metric", "label": "Как поймём, что готово", "type": "text", "value": ""}],
                "actions": [{"label": "Добавить", "action": "goal_confirm", "params": {}, "style": "primary"},
                            _cancel_action()],
                "note": "Руководитель получит уведомление, чтобы согласовать цель."}
        return Reply("Добавлю в трекер прогресса:", cards=[card])

    def _act_goal_confirm(self, params) -> Reply:
        goal = self.mcp.call("goals_add", title=str(params.get("title", "")), metric=str(params.get("metric", "")))
        _set_state(self.user, None)
        return Reply(f"Добавил цель «{goal['title']}» в трекер. Руководитель увидит её и согласует.",
                     suggestions=["Что дальше?"], meta={"source": "action"})

    # ---------------------------------------------------------------- чаты и каналы
    def _on_chats(self, text, slots, state) -> Reply:
        chats = self.mcp.call("messenger_list_chats")
        missing = [c for c in chats if c["required"] and not c["joined"]]
        if not missing:
            joined = ", ".join(c["name"] for c in chats if c["joined"])
            return Reply(f"Ты уже во всех обязательных каналах: {joined}.", suggestions=["Что дальше?"],
                         sources=[_source("comm-channels", "Внутренние каналы коммуникации")])
        card = {"type": "confirm", "title": f"Добавить в каналы: {len(missing)}",
                "rows": [[c["name"], c["description"]] for c in missing],
                "actions": [{"label": "Добавить во все", "action": "chats_confirm",
                             "params": {"codes": [c["code"] for c in missing]}, "style": "primary"}, _cancel_action()],
                "note": "Каналы подобраны по твоей команде и потоку стажировки."}
        return Reply("Вот обязательные каналы, в которых тебя ещё нет:", cards=[card],
                     sources=[_source("comm-channels", "Внутренние каналы коммуникации")])

    def _act_chats_confirm(self, params) -> Reply:
        result = self.mcp.call("messenger_join_chats", codes=list(params.get("codes") or []))
        added = result["added"]
        text = ("Добавил в каналы: " + ", ".join(added) + "." if added else "Ты уже во всех этих каналах.")
        return Reply(text, suggestions=["Что дальше?"], meta={"source": "action"})

    # ---------------------------------------------------------------- помощник наставника
    def _on_overview(self, text, slots, state) -> Reply:
        if self.user.role not in {"mentor", "lead"}:
            return self._on_kb(text, slots, state)
        interns = self.mcp.call("mentor_interns_overview")
        if not interns:
            return Reply("За тобой пока не закреплены стажёры.")
        lines = []
        for i in interns:
            line = f"- **{i['name']}** — {i['stage']}, {i['done']} из {i['total']} задач ({i['percent']}%)."
            if i["overdue"]:
                line += f" Хвосты прошлых этапов: {', '.join(i['overdue'])}."
            if i["open_questions"]:
                line += f" Вопросов без ответа: {i['open_questions']}."
            if i["pending_meetings"]:
                line += f" Встреч на подтверждении: {i['pending_meetings']}."
            lines.append(line)
        suggestions = [f"Подготовь повестку 1:1 с {i['name']}" for i in interns[:2]] + ["Встречи на подтверждении"]
        return Reply("Сводка по стажёрам:\n" + "\n".join(lines), suggestions=suggestions)

    def _find_intern(self, text: str) -> dict | None:
        interns = self.mcp.call("mentor_interns_overview")
        low = text.lower()
        for i in interns:
            first, last = i["name"].lower().split()[:2]
            if first[:4] in low or last[:4] in low:
                return i
        return interns[0] if len(interns) == 1 else None

    def _on_prep_1on1(self, text, slots, state) -> Reply:
        if self.user.role not in {"mentor", "lead"}:
            return self._on_meeting(text, slots, state)
        intern = self._find_intern(text)
        if intern is None:
            interns = self.mcp.call("mentor_interns_overview")
            return Reply("С кем готовим встречу?", suggestions=[f"Подготовь повестку 1:1 с {i['name']}" for i in interns])
        ctx = self.mcp.call("mentor_intern_context", intern_id=intern["id"])
        parts = [f"Повестка 1:1 с **{ctx['name']}** (прогресс {ctx['percent']}%):"]
        if ctx["open_questions"]:
            parts.append("Вопросы без ответа:\n" + "\n".join(f"- {q}" for q in ctx["open_questions"]))
        if ctx["questions"]:
            parts.append("Что спрашивал(а) у AI-buddy:\n" + "\n".join(f"- {q}" for q in ctx["questions"]))
        parts.append("Следующие шаги чек-листа:\n" + "\n".join(f"- {s}" for s in ctx["next"]))
        if ctx["goals"]:
            parts.append("Цели в трекере:\n" + "\n".join(f"- {g}" for g in ctx["goals"]))
        parts.append("Обратная связь: что получается, что мешает, нужна ли помощь с задачами.")
        return Reply("\n\n".join(parts), sources=[_source("mentor-handbook", "Памятка наставника")],
                     suggestions=["Сводка по стажёрам", "Встречи на подтверждении"])

    def _on_meeting_requests(self, text, slots, state) -> Reply:
        if self.user.role != "mentor":
            return self._on_schedule(text, slots, state)
        pending = self.mcp.call("calendar_pending_requests")
        if not pending:
            return Reply("Новых предложений встреч от стажёров нет.", suggestions=["Сводка по стажёрам"])
        cards = [{"type": "confirm", "title": f"{m['label']}: {m['title']}",
                  "rows": [["Когда", m["when"]], ["Повестка", m["agenda"] or "—"]],
                  "actions": [{"label": "Подтвердить", "action": "meeting_decide",
                               "params": {"meeting_id": m["id"], "accept": True}, "style": "primary"},
                              {"label": "Отклонить", "action": "meeting_decide",
                               "params": {"meeting_id": m["id"], "accept": False}, "style": "secondary"}]}
                 for m in pending[:5]]
        return Reply(f"Встреч на подтверждении: {len(pending)}.", cards=cards)

    def _act_meeting_decide(self, params) -> Reply:
        m = self.mcp.call("calendar_decide_meeting", meeting_id=int(params["meeting_id"]),
                          accept=bool(params.get("accept")), comment=str(params.get("comment", "")))
        verb = "Подтвердил" if m["status"] == "accepted" else "Отклонил"
        return Reply(f"{verb} встречу «{m['title']}» ({m['when']}). Стажёр получил уведомление.",
                     suggestions=["Встречи на подтверждении"], meta={"source": "action"})

