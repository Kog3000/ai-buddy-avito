"""Чат: приём сообщений, асинхронная обработка агентом, история."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, current_app

from .agent.agent import Agent
from .models import Attachment, ChatJob, ChatMessage, User, db
from .permissions import PermissionDenied, can_view_user
from .services import access

log = logging.getLogger(__name__)
_executor: ThreadPoolExecutor | None = None

WELCOME = ("Привет, {name}! Я AI-buddy — помогу с адаптацией: найду материалы в базе знаний, оформлю заявку "
           "на доступ или отгул, запланирую встречу с наставником и подскажу следующий шаг.")
WELCOME_STAFF = ("Привет, {name}! Я AI-buddy для наставников: соберу сводку по твоим стажёрам, подготовлю повестку 1:1 "
                 "из их вопросов, покажу встречи на подтверждении и найду материалы для наставников.")
MAX_FILE = 5 * 1024 * 1024
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp", "pdf", "txt", "md", "log", "csv", "json", "docx", "xlsx", "pptx", "zip"}
IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def _pool(app: Flask) -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=app.config["JOB_WORKERS"], thread_name_prefix="chat-job")
    return _executor


def history(user: User, after_id: int = 0, limit: int = 60) -> list[dict]:
    access.sync(user)  # имитация событий сервиса доступов (в проде — консьюмер Kafka)
    if not ChatMessage.query.filter_by(user_id=user.id).first():
        if user.role in {"mentor", "lead"}:
            from .agent.agent import STAFF_SUGGESTIONS
            text, suggestions = WELCOME_STAFF.format(name=user.first_name), STAFF_SUGGESTIONS
        else:
            text = WELCOME.format(name=user.first_name)
            suggestions = ["Что дальше?", "Где хранятся документы нашей команды?", "Как оформить отгул?"]
        db.session.add(ChatMessage(user_id=user.id, role="assistant", text=text, payload={"suggestions": suggestions}))
        db.session.commit()
    query = ChatMessage.query.filter(ChatMessage.user_id == user.id, ChatMessage.id > after_id)
    rows = query.order_by(ChatMessage.id.desc()).limit(limit).all()
    return [m.to_dict() for m in reversed(rows)]


def save_attachment(user: User, filename: str, mime: str, data: bytes) -> Attachment:
    name = (filename or "file").replace("\\", "/").split("/")[-1][:200]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_EXT:
        raise ValueError("Этот тип файла не поддерживается. Можно: изображения, PDF, текст, документы Office, ZIP")
    if not data:
        raise ValueError("Пустой файл")
    if len(data) > MAX_FILE:
        raise ValueError("Файл больше 5 МБ")
    mime = IMAGE_MIME.get(ext, "application/octet-stream")  # тип определяем сами, не доверяя клиенту
    att = Attachment(user_id=user.id, filename=name, mime=mime, size=len(data), data=data)
    db.session.add(att)
    db.session.flush()
    return att


def get_attachment(viewer: User, attachment_id: int) -> Attachment:
    att = db.session.get(Attachment, attachment_id)
    if att is None:
        raise LookupError(attachment_id)
    owner = db.session.get(User, att.user_id)
    if not can_view_user(viewer, owner):
        raise PermissionDenied("Нет доступа к файлу")
    return att


def submit(user: User, text: str = "", action: str | None = None, params: dict | None = None,
           label: str | None = None, files: list | None = None) -> tuple[ChatMessage, ChatJob]:
    shown = (label or text or "").strip()
    attachments = [save_attachment(user, f.filename, f.mimetype, f.read()) for f in (files or [])[:1]]
    user_msg = ChatMessage(user_id=user.id, role="user", text=shown[:2000],
                           payload={"meta": {"action": action} if action else {},
                                    "attachments": [a.to_dict() for a in attachments]})
    job = ChatJob(user_id=user.id)
    db.session.add_all([user_msg, job])
    db.session.flush()
    for a in attachments:
        a.message_id = user_msg.id
    db.session.commit()
    app = current_app._get_current_object()
    args = (app, job.id, user.id, text[:2000], action, params or {}, [a.to_dict() for a in attachments])
    if app.config["SYNC_JOBS"]:
        _run(*args)
    else:
        _pool(app).submit(_run, *args)
    return user_msg, job


def _run(app: Flask, job_id: str, user_id: int, text: str, action: str | None, params: dict,
         attachments: list | None = None) -> None:
    with app.app_context():
        job = db.session.get(ChatJob, job_id)
        user = db.session.get(User, user_id)
        try:
            agent = Agent(user)
            reply = agent.handle_action(action, params) if action else agent.handle_message(text, attachments)
            msg = ChatMessage(user_id=user_id, role="assistant", text=reply.text,
                              payload={"cards": reply.cards, "suggestions": reply.suggestions,
                                       "sources": reply.sources, "meta": reply.meta})
            db.session.add(msg)
            db.session.flush()
            job.status, job.message_id = "done", msg.id
            db.session.commit()
        except Exception as exc:  # noqa: BLE001 — любой сбой должен вернуться пользователю как статус
            log.exception("chat job failed")
            db.session.rollback()
            job = db.session.get(ChatJob, job_id)
            job.status, job.error = "error", exc.__class__.__name__
            db.session.commit()
        finally:
            db.session.remove()


def job_status(user: User, job_id: str) -> dict:
    job = db.session.get(ChatJob, job_id)
    if job is None or job.user_id != user.id:
        raise LookupError(job_id)
    data = {"id": job.id, "status": job.status, "error": job.error}
    if job.message_id:
        data["message"] = db.session.get(ChatMessage, job.message_id).to_dict()
    return data


def set_feedback(user: User, message_id: int, value: int) -> None:
    msg = db.session.get(ChatMessage, message_id)
    if msg is None or msg.user_id != user.id or msg.role != "assistant":
        raise LookupError(message_id)
    msg.feedback = 1 if value > 0 else -1
    db.session.commit()
    from .analytics import track
    track(user, "answer_feedback", value=msg.feedback, source=(msg.payload or {}).get("meta", {}).get("source"))
