"""Авторизация (имитация корпоративного SSO) и CSRF-защита."""
from __future__ import annotations

import secrets
from functools import wraps

from flask import (Blueprint, abort, current_app, has_request_context, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash

from .analytics import track
from .models import User, db

bp = Blueprint("auth", __name__)

TEST_ACCOUNTS = [
    ("arina", "Стажёр (основной сценарий)"),
    ("ivan", "Наставник"),
    ("maria", "Руководитель команды"),
    ("elena", "Куратор стажировки (создаёт кабинеты)"),
    ("dima", "Второй стажёр (проверка прав)"),
]


def current_user() -> User | None:
    """Пользователь текущего запроса (кэшируется на объекте запроса, а не в g:
    app context может переживать несколько запросов)."""
    if not has_request_context():
        return None
    if not hasattr(request, "_ai_buddy_user"):
        uid = session.get("user_id")
        user = db.session.get(User, uid) if uid else None
        request._ai_buddy_user = user if user is not None and user.active else None
    return request._ai_buddy_user


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf() -> None:
    if not current_app.config.get("WTF_CSRF_ENABLED", True):
        return
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if not token or token != session.get("csrf"):
            abort(400, description="CSRF token missing or invalid")


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            if request.path.startswith("/api") or request.path.startswith("/mcp"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("auth.login", next=request.path))
        check_csrf()
        return fn(*args, **kwargs)

    return wrapper


def roles_required(*roles):
    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapper(*args, **kwargs):
            if current_user().role not in roles:
                if request.path.startswith("/api"):
                    return jsonify({"error": "forbidden"}), 403
                abort(403)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def home_for(user: User) -> str:
    if user.role == "intern":
        return url_for("web.home")
    if user.role == "admin":
        return url_for("web.admin_users")
    return url_for("web.team")


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        check_csrf()
        ident = request.form.get("login", "").strip().lower()
        user = User.query.filter(db.or_(User.login == ident, db.func.lower(User.email) == ident)).first()
        if user and not user.active:
            error = "Кабинет отключён — обратитесь к куратору стажировки"
        elif user and check_password_hash(user.password_hash, request.form.get("password", "")):
            session.clear()
            session["user_id"] = user.id
            track(user, "login")
            target = request.args.get("next") or ""
            return redirect(target if target.startswith("/") and not target.startswith("//") else home_for(user))
        else:
            error = "Неверный e-mail или пароль"
    return render_template("login.html", error=error, accounts=TEST_ACCOUNTS)


@bp.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    session.clear()
    return redirect(url_for("auth.login"))
