"""Заметки стажёра после встреч с наставником."""
from __future__ import annotations

from ..models import Note, User, db, now


def list_notes(user: User) -> list[Note]:
    return Note.query.filter_by(user_id=user.id).order_by(Note.created_at.desc(), Note.id.desc()).all()


def save(user: User, title: str, body: str, note_id: int | None = None) -> Note:
    title = (title or "").strip()
    if not title:
        raise ValueError("Укажите заголовок заметки")
    if note_id:
        note = db.session.get(Note, note_id)
        if note is None or note.user_id != user.id:
            raise LookupError(note_id)
    else:
        note = Note(user_id=user.id)
        db.session.add(note)
    note.title = title[:256]
    note.body = (body or "").strip()
    note.updated_at = now()
    db.session.commit()
    return note


def delete(user: User, note_id: int) -> None:
    note = db.session.get(Note, note_id)
    if note is None or note.user_id != user.id:
        raise LookupError(note_id)
    db.session.delete(note)
    db.session.commit()


def to_dict(note: Note) -> dict:
    return {"id": note.id, "title": note.title, "body": note.body,
            "updated": note.updated_at.strftime("%d.%m.%Y")}
