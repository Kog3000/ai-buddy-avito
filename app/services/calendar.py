"""Имитация корпоративного календаря: события, свободное время, встречи стажёра с наставником.

Встречу 1:1 стажёр предлагает (статус pending), наставник подтверждает или отклоняет на экране «Встречи».
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..analytics import track
from ..models import AbsenceRequest, Meeting, User, db
from ..permissions import PermissionDenied
from ..textutil import MONTH_GEN, WEEKDAY_SHORT
from . import checklist
from .notify import notify, post_to_chat, resolve

MONTH_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "нояб", "дек"]
WORK_START, WORK_END = 10, 18
KIND_LABELS = {"mentor": "Встреча с наставником", "training": "Обучение", "team": "Командная встреча",
               "absence": "Отсутствие"}
STATUS_LABELS = {"pending": "Ждёт подтверждения наставника", "accepted": "Запланирована", "declined": "Отклонена"}
ACTIVE = ("pending", "accepted")


def _busy(user_ids: list[int], start: datetime, end: datetime) -> bool:
    return Meeting.query.filter(
        db.or_(Meeting.organizer_id.in_(user_ids), Meeting.attendee_id.in_(user_ids)),
        Meeting.status.in_(ACTIVE), Meeting.start < end, Meeting.end > start).first() is not None


def _away_days(user: User) -> list[AbsenceRequest]:
    return AbsenceRequest.query.filter(AbsenceRequest.user_id == user.id,
                                       AbsenceRequest.status.in_(["pending_approval", "approved"])).all()


def upcoming(user: User, limit: int = 10, now_dt: datetime | None = None) -> list[Meeting]:
    now_dt = now_dt or datetime.now()
    return (Meeting.query.filter(db.or_(Meeting.organizer_id == user.id, Meeting.attendee_id == user.id),
                                 Meeting.end >= now_dt, Meeting.kind != "absence", Meeting.status.in_(ACTIVE))
            .order_by(Meeting.start).limit(limit).all())


def day_slots(user: User, other: User, day: date, duration_min: int = 60,
              now_dt: datetime | None = None) -> list[datetime]:
    """Все свободные слоты дня, общие для двух участников."""
    now_dt = now_dt or datetime.now()
    if day.weekday() >= 5 or any(a.date_from <= day <= a.date_to for a in _away_days(user)):
        return []
    slots = []
    for hour in range(WORK_START, WORK_END):
        start = datetime.combine(day, time(hour, 0))
        end = start + timedelta(minutes=duration_min)
        if start < now_dt + timedelta(hours=2) or end.hour > WORK_END:
            continue
        if not _busy([user.id, other.id], start, end):
            slots.append(start)
    return slots


def free_slots(user: User, other: User, days: int = 5, duration_min: int = 60, limit: int = 4,
               now_dt: datetime | None = None) -> list[datetime]:
    """Короткий список вариантов для чата: первый свободный слот в каждый из ближайших рабочих дней."""
    now_dt = now_dt or datetime.now()
    slots: list[datetime] = []
    day = now_dt.date()
    for _ in range(days + 7):
        if len(slots) >= limit:
            break
        found = day_slots(user, other, day, duration_min, now_dt)
        if found:
            slots.append(found[0])
        day += timedelta(days=1)
    return slots


def availability(user: User, other: User, days: int = 21, now_dt: datetime | None = None) -> list[dict]:
    """Календарь на несколько недель вперёд: для каждого дня — свободные слоты (для экрана выбора времени)."""
    now_dt = now_dt or datetime.now()
    result = []
    for offset in range(days):
        day = now_dt.date() + timedelta(days=offset)
        slots = day_slots(user, other, day, now_dt=now_dt)
        result.append({"date": day.isoformat(), "day": day.day, "weekday": day.weekday(),
                       "label": f"{WEEKDAY_SHORT[day.weekday()]}, {day.day} {MONTH_GEN[day.month - 1]}",
                       "month": MONTH_GEN[day.month - 1],
                       "slots": [{"start": s.isoformat(), "label": f"{s:%H:%M}"} for s in slots]})
    return result


def _validate_slot(intern: User, mentor: User, start: datetime, duration_min: int, now_dt: datetime) -> datetime:
    end = start + timedelta(minutes=duration_min)
    if start < now_dt:
        raise ValueError("Нельзя назначить встречу в прошлом")
    if start.weekday() >= 5 or start.hour < WORK_START or end.hour > WORK_END or (end.hour == WORK_END and end.minute):
        raise ValueError("Встречи назначаются в рабочее время: будни с 10:00 до 18:00")
    if _busy([intern.id, mentor.id], start, end):
        raise ValueError("Это время уже занято — выбери другой слот")
    return end


def request_mentor_meeting(intern: User, start: datetime, title: str = "Встреча 1:1 с наставником",
                           agenda: str = "", duration_min: int = 60, now_dt: datetime | None = None) -> Meeting:
    """Стажёр предлагает время; встреча ждёт подтверждения наставника и уже занимает слот."""
    now_dt = now_dt or datetime.now()
    mentor = intern.mentor
    if intern.role != "intern" or mentor is None:
        raise PermissionDenied("Наставник не назначен")
    end = _validate_slot(intern, mentor, start, duration_min, now_dt)
    meeting = Meeting(title=title.strip() or "Встреча 1:1 с наставником", kind="mentor", status="pending",
                      organizer_id=intern.id, attendee_id=mentor.id, start=start, end=end, agenda=agenda)
    db.session.add(meeting)
    db.session.commit()
    notify(mentor, f"{intern.full_name} предлагает встречу «{meeting.title}» — {fmt_dt(start)}", "/meetings",
           dedup_key=f"meeting:{meeting.id}")
    track(intern, "mentor_meeting_requested")
    return meeting


# обратная совместимость для имени из первой версии
book_mentor_meeting = request_mentor_meeting


def pending_for(mentor: User) -> list[Meeting]:
    return (Meeting.query.filter_by(attendee_id=mentor.id, status="pending", kind="mentor")
            .filter(Meeting.end >= datetime.now()).order_by(Meeting.start).all())


def decide(mentor: User, meeting_id: int, accept: bool, comment: str = "") -> Meeting:
    meeting = db.session.get(Meeting, meeting_id)
    if meeting is None or meeting.kind != "mentor":
        raise LookupError(meeting_id)
    if meeting.attendee_id != mentor.id:
        raise PermissionDenied("Подтвердить встречу может только приглашённый наставник")
    if meeting.status != "pending":
        raise ValueError("Встреча уже обработана")
    meeting.status = "accepted" if accept else "declined"
    meeting.decision_comment = (comment or "").strip()
    db.session.commit()
    intern = meeting.organizer
    resolve(f"meeting:{meeting.id}")
    when = fmt_dt(meeting.start)
    if accept:
        text = f"{mentor.full_name} подтвердил(а) встречу «{meeting.title}» — {when}."
        checklist.auto_complete(intern, "mentor_meeting_booked")
    else:
        text = f"{mentor.full_name} не может встретиться {when}."
        if meeting.decision_comment:
            text += f" Комментарий: {meeting.decision_comment}"
        text += " Выбери другое время."
    if meeting.decision_comment and accept:
        text += f" Комментарий: {meeting.decision_comment}"
    notify(intern, text, "/mentor", dedup_key=f"meeting-result:{meeting.id}")
    post_to_chat(intern, text, suggestions=["Что дальше?"] if accept else ["Запланируй встречу с наставником"])
    track(mentor, "mentor_meeting_" + meeting.status)
    return meeting


def cancel(intern: User, meeting_id: int) -> Meeting:
    meeting = db.session.get(Meeting, meeting_id)
    if meeting is None or meeting.organizer_id != intern.id or meeting.kind != "mentor":
        raise LookupError(meeting_id)
    if meeting.status != "pending":
        raise ValueError("Отменить можно только неподтверждённую встречу")
    meeting.status = "declined"
    meeting.decision_comment = "Отменена стажёром"
    db.session.commit()
    resolve(f"meeting:{meeting.id}")
    return meeting


def fmt_dt(dt: datetime) -> str:
    return f"{WEEKDAY_SHORT[dt.weekday()]}, {dt.day} {MONTH_SHORT[dt.month - 1]}, {dt:%H:%M}"


def relative_label(dt: datetime, today: date | None = None) -> str:
    today = today or date.today()
    delta = (dt.date() - today).days
    if delta == 0:
        return f"Сегодня, {dt:%H:%M}"
    if delta == 1:
        return f"Завтра, {dt:%H:%M}"
    return f"{WEEKDAY_SHORT[dt.weekday()]}, {dt.day} {MONTH_SHORT[dt.month - 1]}, {dt:%H:%M}"


def title_for(m: Meeting, viewer: User | None) -> str:
    """Подпись события с точки зрения смотрящего: наставник видит «Встреча с <стажёр>»."""
    if m.kind != "mentor" or viewer is None:
        return KIND_LABELS.get(m.kind, m.title) if m.kind == "mentor" else m.title
    other = m.organizer if m.attendee_id == viewer.id else m.attendee
    if viewer.role == "intern":
        return "Встреча с наставником"
    return f"Встреча с {other.full_name}" if other else m.title


def to_dict(m: Meeting, viewer: User | None = None) -> dict:
    return {"id": m.id, "title": m.title, "kind": m.kind, "kind_label": KIND_LABELS.get(m.kind, m.kind),
            "label": title_for(m, viewer), "status": m.status, "status_label": STATUS_LABELS.get(m.status, ""),
            "start": m.start.isoformat(), "when": f"{fmt_dt(m.start)} - {m.end:%H:%M}",
            "relative": relative_label(m.start), "agenda": m.agenda, "comment": m.decision_comment,
            "with": (m.organizer.full_name if viewer and m.attendee_id == viewer.id else
                     m.attendee.full_name if m.attendee else "")}
