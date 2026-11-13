"""Кабинеты пользователей: создаёт куратор стажировки (админ), профиль редактирует сам пользователь."""
from __future__ import annotations

import re
import secrets
from datetime import date

from werkzeug.security import generate_password_hash

from ..analytics import track
from ..models import ROLE_LABELS, User, db
from ..permissions import PermissionDenied, require
from . import checklist, messenger
from .notify import notify, post_to_chat

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE = re.compile(r"^[+\d][\d\s()-]{5,20}$")


def _admin(user: User) -> None:
    require(user.role == "admin", "Действие доступно только куратору стажировки")


def _temp_password() -> str:
    return secrets.token_urlsafe(8)


def _login_from_email(email: str) -> str:
    base = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0].lower()) or "user"
    login, n = base, 1
    while User.query.filter_by(login=login).first():
        n += 1
        login = f"{base}{n}"
    return login


def list_users(admin: User) -> list[User]:
    _admin(admin)
    order = {"admin": 0, "lead": 1, "mentor": 2, "intern": 3}
    return sorted(User.query.all(), key=lambda u: (order.get(u.role, 9), u.full_name))


def _check_staff(user_id: int | None, role: str, label: str) -> User | None:
    if not user_id:
        return None
    staff = db.session.get(User, user_id)
    if staff is None or staff.role != role:
        raise ValueError(f"{label} не найден")
    return staff


def create_user(admin: User, full_name: str, email: str, role: str, team: str = "", position: str = "",
                program: str = "", start_date: date | None = None, end_date: date | None = None,
                mentor_id: int | None = None, lead_id: int | None = None, join_chats: bool = True) -> tuple[User, str]:
    _admin(admin)
    full_name, email = (full_name or "").strip(), (email or "").strip().lower()
    if len(full_name.split()) < 2:
        raise ValueError("Укажите имя и фамилию")
    if not EMAIL.match(email):
        raise ValueError("Некорректный e-mail")
    if User.query.filter(db.func.lower(User.email) == email).first():
        raise ValueError("Пользователь с таким e-mail уже есть")
    if role not in ROLE_LABELS or role == "admin":
        raise ValueError("Недопустимая роль")
    mentor = _check_staff(mentor_id, "mentor", "Наставник")
    lead = _check_staff(lead_id, "lead", "Руководитель")
    if role == "intern":
        if not (mentor and lead):
            raise ValueError("Стажёру нужно назначить наставника и руководителя")
        if not start_date:
            raise ValueError("Укажите дату начала стажировки")
        if end_date and end_date < start_date:
            raise ValueError("Дата окончания раньше даты начала")
    password = _temp_password()
    user = User(login=_login_from_email(email), password_hash=generate_password_hash(password), full_name=full_name,
                role=role, email=email, team=(team or "").strip() or (mentor.team if mentor else "—"),
                position=(position or "").strip(), program=(program or "").strip(), start_date=start_date,
                end_date=end_date, mentor_id=mentor.id if mentor else None, lead_id=lead.id if lead else None)
    db.session.add(user)
    db.session.commit()
    if role == "intern":
        checklist.get_checklist(user)  # создаёт чек-лист адаптации
        if join_chats:
            messenger.join(user)
        notify(mentor, f"Вам назначен стажёр: {full_name}", "/team", dedup_key=f"assign:{user.id}:{mentor.id}")
        notify(lead, f"В команду добавлен стажёр: {full_name}", "/team", dedup_key=f"assign:{user.id}:{lead.id}")
    track(admin, "user_created", role=role)
    return user, password


def assign(admin: User, intern_id: int, mentor_id: int, lead_id: int) -> User:
    _admin(admin)
    intern = db.session.get(User, intern_id)
    if intern is None or intern.role != "intern":
        raise LookupError(intern_id)
    mentor = _check_staff(mentor_id, "mentor", "Наставник")
    lead = _check_staff(lead_id, "lead", "Руководитель")
    changed = intern.mentor_id != mentor.id
    intern.mentor_id, intern.lead_id = mentor.id, lead.id
    db.session.commit()
    if changed:
        notify(mentor, f"Вам назначен стажёр: {intern.full_name}", "/team")
        notify(intern, f"Твой наставник — {mentor.full_name}", "/mentor")
        post_to_chat(intern, f"Тебе назначен наставник: **{mentor.full_name}**. Можно запланировать знакомство.",
                     suggestions=["Запланируй встречу с наставником"])
    return intern


def reset_password(admin: User, user_id: int) -> str:
    _admin(admin)
    user = db.session.get(User, user_id)
    if user is None:
        raise LookupError(user_id)
    password = _temp_password()
    user.password_hash = generate_password_hash(password)
    db.session.commit()
    return password


def set_active(admin: User, user_id: int, active: bool) -> User:
    _admin(admin)
    user = db.session.get(User, user_id)
    if user is None:
        raise LookupError(user_id)
    if user.id == admin.id:
        raise PermissionDenied("Нельзя отключить свой кабинет")
    user.active = active
    db.session.commit()
    return user


def update_profile(user: User, full_name: str, phone: str, email: str) -> User:
    full_name, phone, email = (full_name or "").strip(), (phone or "").strip(), (email or "").strip().lower()
    if len(full_name.split()) < 2 or len(full_name) > 128:
        raise ValueError("Укажите имя и фамилию")
    if phone and not PHONE.match(phone):
        raise ValueError("Некорректный телефон")
    if not EMAIL.match(email):
        raise ValueError("Некорректный e-mail")
    other = User.query.filter(db.func.lower(User.email) == email, User.id != user.id).first()
    if other:
        raise ValueError("Этот e-mail уже занят")
    user.full_name, user.phone, user.email = full_name, phone, email
    db.session.commit()
    return user


def to_dict(u: User) -> dict:
    return {"id": u.id, "name": u.full_name, "login": u.login, "email": u.email, "role": u.role,
            "role_label": u.role_label, "team": u.team, "position": u.position, "active": u.active,
            "mentor": u.mentor.full_name if u.mentor else "", "lead": u.lead.full_name if u.lead else "",
            "mentor_id": u.mentor_id, "lead_id": u.lead_id,
            "start": u.start_date.strftime("%d.%m.%Y") if u.start_date else ""}
