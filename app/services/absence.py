"""Имитация корпоративного сервиса отсутствий (отгул, отпуск, работа из дома).

Тестовые правила прототипа (НЕ регламент Авито):
- отгул — не больше 3 рабочих дней подряд, подаётся не позднее чем за 1 день;
- отпуск — не позднее чем за 14 дней;
- согласует руководитель команды; наставник получает уведомление.
Больничные не обрабатываются (чувствительные персональные данные вне области прототипа).
"""
from __future__ import annotations

from datetime import date, datetime, time

from ..analytics import track
from ..models import AbsenceRequest, Meeting, RequestEvent, User, db, now
from ..permissions import PermissionDenied
from ..textutil import fmt_range, workdays_between
from .notify import notify, post_to_chat, resolve

KIND_LABELS = {"day_off": "Отгул", "vacation": "Отпуск", "remote": "Работа из дома"}
STATUS_LABELS = {
    "pending_approval": "На согласовании у руководителя",
    "approved": "Согласовано",
    "rejected": "Отклонено",
    "cancelled": "Отменено",
}
POLICY = {
    "day_off": {"max_workdays": 3, "notice_days": 1},
    "vacation": {"max_workdays": 28, "notice_days": 14},
    "remote": {"max_workdays": 5, "notice_days": 0},
}


def policy_text(kind: str = "day_off") -> str:
    rule = POLICY[kind]
    notice = (f"не позднее чем за {rule['notice_days']} дн." if rule["notice_days"] else "в любой момент")
    return (f"{KIND_LABELS[kind]}: заявка подаётся {notice}, максимум {rule['max_workdays']} раб. дн. подряд, "
            f"согласует руководитель. Это тестовое правило прототипа, не регламент Авито.")


def validate(user: User, kind: str, date_from: date, date_to: date, today: date | None = None) -> None:
    today = today or date.today()
    if kind not in POLICY:
        raise ValueError("Неизвестный тип отсутствия")
    if user.lead is None:
        raise PermissionDenied("Не найден руководитель для согласования")
    if date_to < date_from:
        raise ValueError("Дата окончания раньше даты начала")
    if date_from < today:
        raise ValueError("Нельзя оформить отсутствие задним числом")
    rule = POLICY[kind]
    if (date_from - today).days < rule["notice_days"]:
        raise ValueError(f"{KIND_LABELS[kind]} нужно оформлять не позднее чем за {rule['notice_days']} дн.")
    days = workdays_between(date_from, date_to)
    if days == 0:
        raise ValueError("Выбранные даты приходятся на выходные — оформлять отсутствие не нужно")
    if days > rule["max_workdays"]:
        raise ValueError(f"{KIND_LABELS[kind]} — не больше {rule['max_workdays']} раб. дн. подряд")
    overlap = AbsenceRequest.query.filter(
        AbsenceRequest.user_id == user.id,
        AbsenceRequest.status.in_(["pending_approval", "approved"]),
        AbsenceRequest.date_from <= date_to, AbsenceRequest.date_to >= date_from).first()
    if overlap:
        raise ValueError(f"На эти даты уже есть заявка №O-{overlap.id} ({STATUS_LABELS[overlap.status].lower()})")


def _event(req: AbsenceRequest, status: str, actor: str, note: str = "") -> None:
    db.session.add(RequestEvent(request_type="absence", request_id=req.id, status=status, actor=actor, note=note))


def create_request(user: User, kind: str, date_from: date, date_to: date, comment: str = "",
                   notify_mentor: bool = True) -> AbsenceRequest:
    validate(user, kind, date_from, date_to)
    req = AbsenceRequest(user_id=user.id, kind=kind, date_from=date_from, date_to=date_to, comment=comment,
                         approver_id=user.lead_id, notify_mentor=notify_mentor)
    db.session.add(req)
    db.session.flush()
    _event(req, "created", user.full_name, "Заявка создана через AI-buddy")
    _event(req, "pending_approval", "Сервис отсутствий", user.lead.full_name)
    db.session.commit()
    period = fmt_range(date_from, date_to)
    notify(user.lead, f"{user.full_name}: {KIND_LABELS[kind].lower()} {period} — нужно согласование", "/approvals",
           dedup_key=f"absence:{req.id}")
    if notify_mentor and user.mentor:
        notify(user.mentor, f"{user.full_name} оформил(а) {KIND_LABELS[kind].lower()}: {period}", "/team",
               dedup_key=f"absence-info:{req.id}")
    track(user, "absence_request_created", kind=kind, days=workdays_between(date_from, date_to))
    return req


def decide(approver: User, request_id: int, approve: bool, comment: str = "") -> AbsenceRequest:
    req = db.session.get(AbsenceRequest, request_id)
    if req is None:
        raise LookupError(request_id)
    if req.approver_id != approver.id:
        raise PermissionDenied("Вы не являетесь согласующим этой заявки")
    if req.status != "pending_approval":
        raise ValueError("Заявка уже обработана")
    req.status = "approved" if approve else "rejected"
    req.decision_comment = comment
    req.updated_at = now()
    _event(req, req.status, approver.full_name, comment)
    if approve:
        db.session.add(Meeting(title=KIND_LABELS[req.kind], kind="absence", organizer_id=req.user_id,
                               attendee_id=req.user_id, start=datetime.combine(req.date_from, time(0, 0)),
                               end=datetime.combine(req.date_to, time(23, 59))))
    db.session.commit()
    track(req.user, "absence_request_" + req.status, kind=req.kind)
    verdict = "согласован" if approve else "не согласован"
    text = f"{KIND_LABELS[req.kind]} {fmt_range(req.date_from, req.date_to)} {verdict} руководителем."
    if comment:
        text += f" Комментарий: {comment}"
    resolve(f"absence:{req.id}")
    notify(req.user, text, "/access?tab=absence", dedup_key=f"absence-result:{req.id}")
    post_to_chat(req.user, text, cards=[request_card(req)], suggestions=["Статус моих заявок"])
    return req


def cancel(user: User, request_id: int) -> AbsenceRequest:
    req = db.session.get(AbsenceRequest, request_id)
    if req is None or req.user_id != user.id:
        raise LookupError(request_id)
    if req.status != "pending_approval":
        raise ValueError("Отменить можно только заявку на согласовании")
    req.status = "cancelled"
    req.updated_at = now()
    _event(req, "cancelled", user.full_name)
    db.session.commit()
    return req


def my_requests(user: User) -> list[AbsenceRequest]:
    return AbsenceRequest.query.filter_by(user_id=user.id).order_by(AbsenceRequest.id.desc()).all()


def for_approval(approver: User) -> list[AbsenceRequest]:
    return (AbsenceRequest.query.filter_by(approver_id=approver.id, status="pending_approval")
            .order_by(AbsenceRequest.id).all())


def to_dict(req: AbsenceRequest) -> dict:
    rows = (RequestEvent.query.filter_by(request_type="absence", request_id=req.id)
            .order_by(RequestEvent.id).all())
    return {
        "id": req.id, "type": "absence", "kind": req.kind, "kind_label": KIND_LABELS[req.kind],
        "period": fmt_range(req.date_from, req.date_to), "date_from": req.date_from.isoformat(),
        "date_to": req.date_to.isoformat(), "comment": req.comment, "status": req.status,
        "status_label": STATUS_LABELS[req.status], "approver": req.approver.full_name if req.approver else "",
        "decision_comment": req.decision_comment, "user": req.user.full_name,
        "created_at": req.created_at.strftime("%d.%m.%Y %H:%M"),
        "timeline": [{"status": r.status, "label": STATUS_LABELS.get(r.status, "Создана"), "actor": r.actor,
                      "note": r.note, "at": r.at.strftime("%d.%m %H:%M")} for r in rows],
    }


def request_card(req: AbsenceRequest) -> dict:
    data = to_dict(req)
    return {"type": "request_status", "title": f"Заявка №O-{req.id}: {data['kind_label']}",
            "status": req.status, "status_label": data["status_label"],
            "rows": [["Даты", data["period"]], ["Согласующий", data["approver"]],
                     ["Комментарий", data["comment"] or "—"]],
            "link": "/access?tab=absence"}
