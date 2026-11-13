"""Вопросы стажёра наставнику: эскалация из AI-buddy, ответ наставника, публикация в FAQ."""
from __future__ import annotations

from ..analytics import track
from ..models import MentorQuestion, User, db, now
from ..permissions import PermissionDenied
from . import kb
from .notify import notify, post_to_chat, resolve


def ask(intern: User, question: str, attachment_id: int | None = None) -> MentorQuestion:
    question = (question or "").strip()
    if len(question) < 3:
        raise ValueError("Вопрос слишком короткий")
    if intern.mentor is None:
        raise PermissionDenied("Наставник не назначен")
    q = MentorQuestion(intern_id=intern.id, mentor_id=intern.mentor_id, question=question, attachment_id=attachment_id)
    db.session.add(q)
    db.session.commit()
    notify(intern.mentor, f"Новый вопрос от {intern.full_name}: «{question[:80]}»", "/team/questions",
           dedup_key=f"question:{q.id}")
    track(intern, "mentor_question_escalated")
    return q


def open_for(mentor: User) -> list[MentorQuestion]:
    return (MentorQuestion.query.filter_by(mentor_id=mentor.id, status="open")
            .order_by(MentorQuestion.created_at).all())


def answered_for(mentor: User, limit: int = 10) -> list[MentorQuestion]:
    return (MentorQuestion.query.filter_by(mentor_id=mentor.id, status="answered")
            .order_by(MentorQuestion.answered_at.desc()).limit(limit).all())


def of_intern(intern: User) -> list[MentorQuestion]:
    return MentorQuestion.query.filter_by(intern_id=intern.id).order_by(MentorQuestion.id.desc()).all()


def answer(mentor: User, question_id: int, text: str, publish_to_faq: bool = False) -> MentorQuestion:
    q = db.session.get(MentorQuestion, question_id)
    if q is None:
        raise LookupError(question_id)
    if q.mentor_id != mentor.id:
        raise PermissionDenied("Ответить может только наставник стажёра")
    if q.status != "open":
        raise ValueError("На вопрос уже ответили")
    text = (text or "").strip()
    if not text:
        raise ValueError("Пустой ответ")
    q.answer = text
    q.status = "answered"
    q.answered_at = now()
    q.publish_to_faq = publish_to_faq
    db.session.commit()
    sources = []
    if publish_to_faq:
        topic = kb.add_faq(mentor, q.question, text, team=q.intern.team)
        sources = [{"slug": topic.slug, "title": topic.title}]
    resolve(f"question:{q.id}")
    notify(q.intern, f"{mentor.full_name} ответил(а) на твой вопрос «{q.question[:60]}»", "/chat",
           dedup_key=f"answer:{q.id}")
    post_to_chat(q.intern, f"{mentor.first_name} ответил(а) на твой вопрос «{q.question}»:\n\n{text}",
                 suggestions=["Спасибо!", "Что дальше?"], meta={"from_mentor": True, "sources": sources})
    track(mentor, "mentor_question_answered", published=publish_to_faq,
          minutes=round((q.answered_at - q.created_at).total_seconds() / 60, 1))
    return q
