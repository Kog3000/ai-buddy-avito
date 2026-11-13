"""Имитация корпоративного сервиса заявок на доступ.

Жизненный цикл: pending_approval → granted | rejected; pending_approval → cancelled (стажёром).
Согласующий зависит от ресурса: политика (автоматически) или владелец проекта.
Стажёр получает те же доступы, что и остальные участники команды (ограничение — по ресурсу, а не по статусу стажёра).
"""
from __future__ import annotations

from datetime import timedelta

from flask import current_app

from ..analytics import track
from ..models import AccessRequest, RequestEvent, Resource, User, db, now
from ..permissions import PermissionDenied
from ..seed_data import LEVEL_LABELS
from . import checklist
from .notify import notify, post_to_chat, resolve

LEVEL_RANK = {"none": 0, "read": 1, "edit": 2, "admin": 3}
STATUS_LABELS = {
    "pending_approval": "На согласовании",
    "granted": "Доступ выдан",
    "rejected": "Отклонена",
    "cancelled": "Отменена",
}
ACTIVE = {"pending_approval", "granted"}



class DuplicateRequest(Exception):
    def __init__(self, existing: AccessRequest):
        super().__init__("duplicate")
        self.existing = existing


def resources() -> list[Resource]:
    return Resource.query.order_by(Resource.id).all()


def get_resource(code: str) -> Resource:
    res = Resource.query.filter_by(code=code).first()
    if res is None:
        raise LookupError(code)
    return res


def allowed_levels(user: User, res: Resource) -> list[str]:
    """Владелец проекта получает любой уровень; остальные участники команды (включая стажёров) — до member_max."""
    levels = res.level_list()
    if res.owner_id == user.id:
        return levels
    cap = LEVEL_RANK[res.intern_max_level]
    return [lvl for lvl in levels if LEVEL_RANK[lvl] <= cap]


def recommended_level(user: User, res: Resource) -> str | None:
    levels = allowed_levels(user, res)
    if not levels:
        return None
    if "edit" in levels and res.code in {"stash", "jira"}:
        return "edit"
    return levels[0]


def _approver(user: User, res: Resource) -> tuple[User | None, str]:
    if res.approver_rule == "policy":
        return None, "Автоматически по политике доступа"
    owner = res.owner or user.lead
    return owner, f"{owner.full_name}, владелец проекта" if owner else "Владелец проекта"


def _event(req: AccessRequest, status: str, actor: str, note: str = "") -> None:
    db.session.add(RequestEvent(request_type="access", request_id=req.id, status=status, actor=actor, note=note))


def validate(user: User, code: str, level: str) -> Resource:
    res = get_resource(code)
    if level not in res.level_list():
        raise ValueError(f"Для ресурса «{res.name}» нет уровня «{level}»")
    if res.owner_id != user.id:
        if res.intern_max_level == "none":
            raise PermissionDenied(f"Доступ к ресурсу «{res.name}» участникам команды не выдаётся — он есть только у дежурных SRE")
        if LEVEL_RANK[level] > LEVEL_RANK[res.intern_max_level]:
            raise PermissionDenied(
                f"Участникам команды доступен максимум уровень «{LEVEL_LABELS[res.intern_max_level]}» для «{res.name}» — "
                f"администрирование выдаётся владельцам проектов")
    return res


def active_request(user: User, res: Resource) -> AccessRequest | None:
    return (AccessRequest.query.filter(AccessRequest.user_id == user.id, AccessRequest.resource_id == res.id,
                                       AccessRequest.status.in_(ACTIVE))
            .order_by(AccessRequest.id.desc()).first())


def create_request(user: User, code: str, level: str, justification: str, project: str = "") -> AccessRequest:
    res = validate(user, code, level)
    justification = (justification or "").strip()
    if len(justification) < 5:
        raise ValueError("Укажите обоснование запроса (не короче 5 символов)")
    existing = active_request(user, res)
    if existing and LEVEL_RANK[existing.level] >= LEVEL_RANK[level]:
        raise DuplicateRequest(existing)
    approver, label = _approver(user, res)
    req = AccessRequest(user_id=user.id, resource_id=res.id, level=level, justification=justification,
                        project=project or res.name, approver_id=approver.id if approver else None,
                        approver_label=label, status="pending_approval")
    db.session.add(req)
    db.session.flush()
    _event(req, "created", user.full_name, "Заявка создана через AI-buddy")
    _event(req, "pending_approval", "Сервис доступов", label)
    db.session.commit()
    if approver:
        notify(approver, f"{user.full_name} запрашивает доступ «{res.name}» ({LEVEL_LABELS[level]})", "/approvals",
               dedup_key=f"access:{req.id}")
    track(user, "access_request_created", resource=code, level=level)
    return req


def _finish(req: AccessRequest, granted: bool, actor: str, comment: str = "") -> None:
    req.status = "granted" if granted else "rejected"
    req.decision_comment = comment
    req.updated_at = now()
    _event(req, req.status, actor, comment)
    db.session.commit()
    track(req.user, "access_request_" + req.status, resource=req.resource.code,
          minutes=round((req.updated_at - req.created_at).total_seconds() / 60, 1))
    level = LEVEL_LABELS[req.level]
    if granted:
        text = f"Готово: доступ «{req.resource.name}» ({level}) выдан."
    else:
        text = f"Заявка на доступ «{req.resource.name}» отклонена." + (f" Комментарий: {comment}" if comment else "")
    suggestions = ["Статус моих заявок", "Что дальше?"] if granted else ["Почему могли отклонить?", "Статус моих заявок"]
    resolve(f"access:{req.id}")
    notify(req.user, text, "/access", dedup_key=f"access-result:{req.id}")
    post_to_chat(req.user, text, cards=[request_card(req)], suggestions=suggestions)
    check_base_access(req.user)


def check_base_access(user: User) -> bool:
    base = Resource.query.filter_by(base=True).all()
    granted = {r.resource_id for r in AccessRequest.query.filter_by(user_id=user.id, status="granted")}
    if base and all(r.id in granted for r in base):
        task = checklist.auto_complete(user, "base_access_granted")
        if task:
            post_to_chat(user, f"Все основные доступы выданы — задача «{task.title}» отмечена в чек-листе.",
                         suggestions=["Что дальше?"])
        return True
    return False


def sync(user: User | None = None) -> int:
    """Имитация обработки заявок сервисом: политика одобряет заявки через заданную задержку."""
    delay = current_app.config["ACCESS_AUTO_APPROVE_SECONDS"]
    threshold = now() - timedelta(seconds=delay)
    query = AccessRequest.query.filter(AccessRequest.status == "pending_approval",
                                       AccessRequest.approver_id.is_(None),
                                       AccessRequest.created_at <= threshold)
    if user is not None:
        query = query.filter(AccessRequest.user_id == user.id)
    changed = 0
    for req in query.all():
        _finish(req, True, "Политика доступа", "Одобрено автоматически")
        changed += 1
    return changed


def decide(approver: User, request_id: int, approve: bool, comment: str = "") -> AccessRequest:
    req = db.session.get(AccessRequest, request_id)
    if req is None:
        raise LookupError(request_id)
    if req.approver_id != approver.id:
        raise PermissionDenied("Вы не являетесь согласующим этой заявки")
    if req.status != "pending_approval":
        raise ValueError("Заявка уже обработана")
    _finish(req, approve, approver.full_name, comment)
    return req


def cancel(user: User, request_id: int) -> AccessRequest:
    req = db.session.get(AccessRequest, request_id)
    if req is None or req.user_id != user.id:
        raise LookupError(request_id)
    if req.status != "pending_approval":
        raise ValueError("Отменить можно только заявку на согласовании")
    req.status = "cancelled"
    req.updated_at = now()
    _event(req, "cancelled", user.full_name)
    db.session.commit()
    return req


def my_requests(user: User) -> list[AccessRequest]:
    return AccessRequest.query.filter_by(user_id=user.id).order_by(AccessRequest.id.desc()).all()


def for_approval(approver: User) -> list[AccessRequest]:
    return (AccessRequest.query.filter_by(approver_id=approver.id, status="pending_approval")
            .order_by(AccessRequest.id).all())


def timeline(req: AccessRequest) -> list[dict]:
    rows = (RequestEvent.query.filter_by(request_type="access", request_id=req.id)
            .order_by(RequestEvent.id).all())
    return [{"status": r.status, "label": STATUS_LABELS.get(r.status, "Создана"), "actor": r.actor,
             "note": r.note, "at": r.at.strftime("%d.%m %H:%M")} for r in rows]


def to_dict(req: AccessRequest) -> dict:
    return {
        "id": req.id, "type": "access", "resource": req.resource.name, "resource_code": req.resource.code,
        "level": req.level, "level_label": LEVEL_LABELS[req.level], "project": req.project,
        "justification": req.justification, "status": req.status, "status_label": STATUS_LABELS[req.status],
        "approver": req.approver_label, "comment": req.decision_comment,
        "created_at": req.created_at.strftime("%d.%m.%Y %H:%M"), "user": req.user.full_name,
        "timeline": timeline(req),
    }


def request_card(req: AccessRequest) -> dict:
    data = to_dict(req)
    return {"type": "request_status", "title": f"Заявка №A-{req.id}: {data['resource']}",
            "status": data["status"], "status_label": data["status_label"],
            "rows": [["Уровень", data["level_label"]], ["Согласующий", data["approver"]],
                     ["Обоснование", data["justification"]]],
            "link": "/access"}
