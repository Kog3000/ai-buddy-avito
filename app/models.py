"""Модели данных. Схема совместима с PostgreSQL (по умолчанию для локального запуска — SQLite)."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import JSON

db = SQLAlchemy()


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


ROLE_INTERN = "intern"
ROLE_MENTOR = "mentor"
ROLE_LEAD = "lead"
ROLE_ADMIN = "admin"
ROLE_LABELS = {
    ROLE_INTERN: "Стажёр",
    ROLE_MENTOR: "Наставник",
    ROLE_LEAD: "Руководитель",
    ROLE_ADMIN: "Куратор стажировки",
}


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    login = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    full_name = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(16), nullable=False)
    team = db.Column(db.String(128), nullable=False)
    position = db.Column(db.String(128), nullable=False, default="")
    phone = db.Column(db.String(32), default="")
    email = db.Column(db.String(128), default="")
    program = db.Column(db.String(128), default="")
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    mentor_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    lead_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now)

    mentor = db.relationship("User", foreign_keys=[mentor_id], remote_side=[id])
    lead = db.relationship("User", foreign_keys=[lead_id], remote_side=[id])

    @property
    def initials(self) -> str:
        parts = self.full_name.split()
        return "".join(p[0] for p in parts[:2]).upper()

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0]

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    def stage_number(self, today: date | None = None) -> int:
        """Текущий этап адаптации (1..5) по дням от начала стажировки: неделя 1, неделя 2, месяц 1,
        месяцы 2–3, после 3 месяцев — как в чек-листе стажёра."""
        from .seed_data import STAGE_STARTS
        today = today or date.today()
        if not self.start_date:
            return 1
        days = (today - self.start_date).days
        return max(stage for stage, start in STAGE_STARTS.items() if days >= start or stage == 1)


class KBCategory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(64), unique=True, nullable=False)
    title = db.Column(db.String(128), nullable=False)
    hours = db.Column(db.Integer, nullable=False, default=1)
    order = db.Column(db.Integer, default=0)
    topics = db.relationship("KBTopic", backref="category", order_by="KBTopic.order")


class KBTopic(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(96), unique=True, nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("kb_category.id"), nullable=False)
    title = db.Column(db.String(256), nullable=False)
    body = db.Column(db.Text, nullable=False)
    minutes = db.Column(db.Integer, default=30)
    # all — видно всем; mentor — наставникам и руководителям; lead — только руководителям
    audience = db.Column(db.String(16), default="all")
    team = db.Column(db.String(128))  # если задано — материал конкретной команды
    tags = db.Column(db.String(256), default="")
    order = db.Column(db.Integer, default=0)
    source = db.Column(db.String(32), default="kb")  # kb | faq (ответ наставника)


class TopicProgress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    topic_id = db.Column(db.Integer, db.ForeignKey("kb_topic.id"), nullable=False)
    done_at = db.Column(db.DateTime, default=now)
    __table_args__ = (db.UniqueConstraint("user_id", "topic_id"),)


class ChecklistTask(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    week = db.Column(db.Integer, nullable=False)
    order = db.Column(db.Integer, default=0)
    title = db.Column(db.String(256), nullable=False)
    description = db.Column(db.Text, default="")
    owner = db.Column(db.String(64), default="Стажёр")
    kb_topic_slug = db.Column(db.String(96))
    chat_prompt = db.Column(db.String(256))
    auto_key = db.Column(db.String(64))  # задача закрывается автоматически по событию


class UserTask(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    task_id = db.Column(db.Integer, db.ForeignKey("checklist_task.id"), nullable=False)
    status = db.Column(db.String(16), default="todo")  # todo | in_progress | done
    completed_at = db.Column(db.DateTime)
    source = db.Column(db.String(16))  # manual | auto
    task = db.relationship("ChecklistTask")
    __table_args__ = (db.UniqueConstraint("user_id", "task_id"),)


class Resource(db.Model):
    """Каталог ресурсов в имитации сервиса доступов."""

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.String(256), default="")
    aliases = db.Column(db.String(256), default="")
    levels = db.Column(db.String(64), default="read,edit")
    # Максимальный уровень для участника команды (стажёр получает те же права, что и команда);
    # none — ресурс участникам команды не выдаётся. Владельцы проектов не ограничены.
    intern_max_level = db.Column(db.String(16), default="edit")
    base = db.Column(db.Boolean, default=False)  # входит в «основные сервисы» чек-листа
    approver_rule = db.Column(db.String(32), default="owner")  # policy | owner
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    owner = db.relationship("User")

    def level_list(self) -> list[str]:
        return [lvl for lvl in self.levels.split(",") if lvl]


class AccessRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    resource_id = db.Column(db.Integer, db.ForeignKey("resource.id"), nullable=False)
    project = db.Column(db.String(128), default="")
    level = db.Column(db.String(16), nullable=False)
    justification = db.Column(db.Text, default="")
    status = db.Column(db.String(24), default="pending_approval")
    approver_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    approver_label = db.Column(db.String(128), default="")
    decision_comment = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now)
    user = db.relationship("User", foreign_keys=[user_id])
    approver = db.relationship("User", foreign_keys=[approver_id])
    resource = db.relationship("Resource")


class AbsenceRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    kind = db.Column(db.String(24), nullable=False)  # day_off | vacation | remote
    date_from = db.Column(db.Date, nullable=False)
    date_to = db.Column(db.Date, nullable=False)
    comment = db.Column(db.Text, default="")
    status = db.Column(db.String(24), default="pending_approval")
    approver_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    decision_comment = db.Column(db.Text, default="")
    notify_mentor = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now)
    user = db.relationship("User", foreign_keys=[user_id])
    approver = db.relationship("User", foreign_keys=[approver_id])


class RequestEvent(db.Model):
    """История статусов заявки (для таймлайна)."""

    id = db.Column(db.Integer, primary_key=True)
    request_type = db.Column(db.String(16), nullable=False)  # access | absence
    request_id = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(24), nullable=False)
    note = db.Column(db.String(256), default="")
    actor = db.Column(db.String(128), default="")
    at = db.Column(db.DateTime, default=now)


class Meeting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256), nullable=False)
    kind = db.Column(db.String(24), default="mentor")  # mentor | training | team | absence
    # pending — стажёр предложил время, ждёт подтверждения наставника; accepted; declined
    status = db.Column(db.String(16), default="accepted")
    decision_comment = db.Column(db.String(256), default="")
    organizer_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    attendee_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    start = db.Column(db.DateTime, nullable=False)
    end = db.Column(db.DateTime, nullable=False)
    agenda = db.Column(db.Text, default="")
    organizer = db.relationship("User", foreign_keys=[organizer_id])
    attendee = db.relationship("User", foreign_keys=[attendee_id])


class Note(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(256), nullable=False)
    body = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=now)
    updated_at = db.Column(db.DateTime, default=now)


class Goal(db.Model):
    """Трекер прогресса: цели, задачи и ожидания на период адаптации."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    kind = db.Column(db.String(16), default="goal")  # goal | expectation
    title = db.Column(db.String(256), nullable=False)
    metric = db.Column(db.String(256), default="")
    due = db.Column(db.Date)
    status = db.Column(db.String(16), default="active")  # active | done
    agreed_by_lead = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now)


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    role = db.Column(db.String(16), nullable=False)  # user | assistant
    text = db.Column(db.Text, default="")
    payload = db.Column(JSON, default=dict)  # cards, suggestions, sources, meta
    feedback = db.Column(db.Integer)  # +1 / -1
    created_at = db.Column(db.DateTime, default=now)

    def to_dict(self) -> dict:
        payload = self.payload or {}
        return {
            "id": self.id,
            "role": self.role,
            "text": self.text,
            "cards": payload.get("cards", []),
            "suggestions": payload.get("suggestions", []),
            "sources": payload.get("sources", []),
            "meta": payload.get("meta", {}),
            "attachments": payload.get("attachments", []),
            "feedback": self.feedback,
            "time": self.created_at.strftime("%H:%M"),
        }


class ChatJob(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    status = db.Column(db.String(16), default="pending")  # pending | done | error
    message_id = db.Column(db.Integer)
    error = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=now)


class ConversationState(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    data = db.Column(db.Text, default="{}")

    def get(self) -> dict:
        return json.loads(self.data or "{}")

    def set(self, value: dict) -> None:
        self.data = json.dumps(value, ensure_ascii=False, default=str)


class MentorQuestion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    intern_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    mentor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    question = db.Column(db.Text, nullable=False)
    attachment_id = db.Column(db.Integer, db.ForeignKey("attachment.id"))
    attachment = db.relationship("Attachment", foreign_keys=[attachment_id])
    answer = db.Column(db.Text)
    status = db.Column(db.String(16), default="open")  # open | answered
    publish_to_faq = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now)
    answered_at = db.Column(db.DateTime)
    intern = db.relationship("User", foreign_keys=[intern_id])
    mentor = db.relationship("User", foreign_keys=[mentor_id])


class Attachment(db.Model):
    """Файл, приложенный к сообщению в чате (хранится в БД, до 5 МБ)."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    message_id = db.Column(db.Integer, db.ForeignKey("chat_message.id"))
    filename = db.Column(db.String(256), nullable=False)
    mime = db.Column(db.String(128), default="application/octet-stream")
    size = db.Column(db.Integer, default=0)
    data = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(db.DateTime, default=now)

    def to_dict(self) -> dict:
        kb = max(1, round(self.size / 1024))
        return {"id": self.id, "name": self.filename, "size": f"{kb} КБ", "url": f"/api/attachments/{self.id}",
                "image": self.mime.startswith("image/")}


class ChatRoom(db.Model):
    """Каналы корпоративного мессенджера (имитация)."""

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.String(256), default="")
    scope = db.Column(db.String(16), default="company")  # company | team | program
    team = db.Column(db.String(128))
    required = db.Column(db.Boolean, default=True)


class ChatMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey("chat_room.id"), nullable=False)
    joined_at = db.Column(db.DateTime, default=now)
    __table_args__ = (db.UniqueConstraint("user_id", "room_id"),)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    text = db.Column(db.String(512), nullable=False)
    link = db.Column(db.String(256), default="")
    dedup_key = db.Column(db.String(128))
    read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now)


class AnalyticsEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    name = db.Column(db.String(64), nullable=False, index=True)
    props = db.Column(JSON, default=dict)
    at = db.Column(db.DateTime, default=now)


class FeatureFlag(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    enabled = db.Column(db.Boolean, default=True)
    description = db.Column(db.String(256), default="")
