"""REST API для интерфейса и MCP-эндпоинт."""
from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, Response, abort, current_app, jsonify, request
from werkzeug.exceptions import HTTPException

from . import chat, cron, metrics
from .analytics import all_flags, set_flag, track
from .auth import current_user, login_required, roles_required
from .mcp import server as mcp_server
from .mcp.registry import ToolError
from .models import User, db
from .permissions import PermissionDenied, can_view_user
from .services import absence, access, calendar, checklist, goals, kb, mentor_questions, messenger, notes, users
from .services.notify import mark_all_read, recent, unread

bp = Blueprint("api", __name__, url_prefix="/api")
mcp_bp = Blueprint("mcp", __name__)


def _json() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@bp.errorhandler(PermissionDenied)
def _forbidden(exc):
    return jsonify({"error": "forbidden", "message": str(exc)}), 403


@bp.errorhandler(LookupError)
def _not_found(exc):
    return jsonify({"error": "not_found"}), 404


@bp.errorhandler(ValueError)
@bp.errorhandler(ToolError)
def _bad_request(exc):
    return jsonify({"error": "bad_request", "message": str(exc)}), 400


@bp.errorhandler(HTTPException)
def _http(exc):
    return jsonify({"error": exc.name, "message": exc.description}), exc.code


# ------------------------------------------------------------------ профиль
@bp.get("/me")
@login_required
def me():
    u = current_user()
    return jsonify({"id": u.id, "name": u.full_name, "role": u.role, "team": u.team,
                    "week": u.stage_number() if u.role == "intern" else None,
                    "unread": len(unread(u)),
                    "notifications": [{"id": n.id, "text": n.text, "link": n.link, "read": n.read} for n in recent(u)]})


@bp.put("/profile")
@login_required
def profile_update():
    data = _json()
    u = users.update_profile(current_user(), data.get("full_name", ""), data.get("phone", ""), data.get("email", ""))
    return jsonify({"name": u.full_name, "phone": u.phone, "email": u.email, "initials": u.initials})


@bp.post("/notifications/read")
@login_required
def notifications_read():
    mark_all_read(current_user())
    return jsonify({"ok": True})


# ------------------------------------------------------------------ чат
@bp.get("/chat/messages")
@login_required
def chat_messages():
    after = request.args.get("after", 0, type=int)
    return jsonify({"messages": chat.history(current_user(), after_id=after)})


@bp.post("/chat/send")
@login_required
def chat_send():
    files = [f for f in request.files.getlist("file") if f and f.filename]
    data = request.form.to_dict() if request.mimetype == "multipart/form-data" else _json()
    text = str(data.get("text") or "").strip()
    action = data.get("action")
    if not text and not action and not files:
        raise ValueError("Пустое сообщение")
    if action is not None and not isinstance(action, str):
        raise ValueError("Некорректное действие")
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    user_msg, job = chat.submit(current_user(), text=text, action=action, params=params,
                                label=str(data.get("label") or ""), files=files)
    return jsonify({"user_message": user_msg.to_dict(), "job_id": job.id}), 202


@bp.get("/attachments/<int:attachment_id>")
@login_required
def attachment_get(attachment_id):
    att = chat.get_attachment(current_user(), attachment_id)
    inline = att.mime.startswith("image/")
    disposition = "inline" if inline else "attachment"
    from urllib.parse import quote
    resp = Response(att.data, mimetype=att.mime)
    resp.headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(att.filename)}"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.get("/chat/jobs/<job_id>")
@login_required
def chat_job(job_id):
    return jsonify(chat.job_status(current_user(), job_id))


@bp.post("/chat/messages/<int:message_id>/feedback")
@login_required
def chat_feedback(message_id):
    chat.set_feedback(current_user(), message_id, int(_json().get("value", 1)))
    return jsonify({"ok": True})


# ------------------------------------------------------------------ чек-лист и база знаний
@bp.get("/checklist")
@roles_required("intern")
def checklist_get():
    return jsonify(checklist.get_checklist(current_user()))


@bp.post("/checklist/<int:task_id>")
@roles_required("intern")
def checklist_set(task_id):
    checklist.set_status(current_user(), task_id, bool(_json().get("done")))
    return jsonify(checklist.get_checklist(current_user()))


@bp.get("/kb/search")
@login_required
def kb_search():
    q = request.args.get("q", "")
    found = kb.search_catalog(current_user(), q, limit=8)
    track(current_user(), "kb_search", q_len=len(q), hits=len(found["topics"]))
    return jsonify({"categories": found["categories"], "results": found["topics"]})


@bp.post("/kb/topics/<slug>/done")
@login_required
def kb_done(slug):
    kb.mark_done(current_user(), slug, bool(_json().get("done", True)))
    return jsonify({"ok": True, "progress": kb.progress(current_user())})


# ------------------------------------------------------------------ заявки
@bp.get("/requests")
@login_required
def requests_list():
    u = current_user()
    access.sync(u)
    return jsonify({"access": [access.to_dict(r) for r in access.my_requests(u)],
                    "absence": [absence.to_dict(r) for r in absence.my_requests(u)]})


@bp.post("/requests/<kind>/<int:req_id>/cancel")
@login_required
def request_cancel(kind, req_id):
    svc = {"access": access, "absence": absence}.get(kind)
    if svc is None:
        abort(404)
    req = svc.cancel(current_user(), req_id)
    return jsonify(svc.to_dict(req))


@bp.post("/approvals/<kind>/<int:req_id>")
@roles_required("lead", "mentor")
def approval_decide(kind, req_id):
    data = _json()
    svc = {"access": access, "absence": absence}.get(kind)
    if svc is None:
        abort(404)
    req = svc.decide(current_user(), req_id, bool(data.get("approve")), str(data.get("comment") or ""))
    return jsonify(svc.to_dict(req))


# ------------------------------------------------------------------ наставник: встречи и заметки
@bp.get("/meetings/slots")
@roles_required("intern")
def meeting_slots():
    u = current_user()
    return jsonify({"slots": [{"start": s.isoformat(), "label": calendar.fmt_dt(s)}
                              for s in calendar.free_slots(u, u.mentor, limit=6)]})


@bp.get("/meetings/availability")
@roles_required("intern")
def meeting_availability():
    u = current_user()
    days = min(request.args.get("days", 21, type=int), 42)
    return jsonify({"days": calendar.availability(u, u.mentor, days)})


@bp.post("/meetings")
@roles_required("intern")
def meeting_book():
    data = _json()
    try:
        start = datetime.fromisoformat(str(data.get("start")))
    except ValueError as exc:
        raise ValueError("Выбери время встречи") from exc
    meeting = calendar.request_mentor_meeting(current_user(), start, str(data.get("title") or ""),
                                              str(data.get("agenda") or ""))
    return jsonify(calendar.to_dict(meeting, current_user())), 201


@bp.post("/meetings/<int:meeting_id>/decide")
@roles_required("mentor")
def meeting_decide(meeting_id):
    data = _json()
    m = calendar.decide(current_user(), meeting_id, bool(data.get("accept")), str(data.get("comment") or ""))
    return jsonify(calendar.to_dict(m, current_user()))


@bp.post("/meetings/<int:meeting_id>/cancel")
@roles_required("intern")
def meeting_cancel(meeting_id):
    return jsonify(calendar.to_dict(calendar.cancel(current_user(), meeting_id), current_user()))


# ------------------------------------------------------------------ мессенджер
@bp.get("/chats")
@login_required
def chats_list():
    return jsonify({"chats": messenger.status(current_user())})


@bp.post("/chats/join")
@login_required
def chats_join():
    codes = _json().get("codes")
    added = messenger.join(current_user(), list(codes) if isinstance(codes, list) else None)
    return jsonify({"added": added, "chats": messenger.status(current_user())})


# ------------------------------------------------------------------ куратор стажировки (админ)
@bp.get("/admin/users")
@roles_required("admin")
def admin_users():
    return jsonify({"users": [users.to_dict(u) for u in users.list_users(current_user())]})


@bp.post("/admin/users")
@roles_required("admin")
def admin_create_user():
    d = _json()
    parse = lambda v: date.fromisoformat(v) if v else None  # noqa: E731
    as_int = lambda v: int(v) if str(v or "").isdigit() else None  # noqa: E731
    user, password = users.create_user(
        current_user(), d.get("full_name", ""), d.get("email", ""), d.get("role", ""), d.get("team", ""),
        d.get("position", ""), d.get("program", ""), parse(d.get("start_date")), parse(d.get("end_date")),
        as_int(d.get("mentor_id")), as_int(d.get("lead_id")), bool(d.get("join_chats", True)))
    return jsonify({"user": users.to_dict(user), "password": password}), 201


@bp.post("/admin/users/<int:user_id>/assign")
@roles_required("admin")
def admin_assign(user_id):
    d = _json()
    u = users.assign(current_user(), user_id, int(d.get("mentor_id") or 0), int(d.get("lead_id") or 0))
    return jsonify(users.to_dict(u))


@bp.post("/admin/users/<int:user_id>/password")
@roles_required("admin")
def admin_reset_password(user_id):
    return jsonify({"password": users.reset_password(current_user(), user_id)})


@bp.post("/admin/users/<int:user_id>/active")
@roles_required("admin")
def admin_set_active(user_id):
    return jsonify(users.to_dict(users.set_active(current_user(), user_id, bool(_json().get("active")))))


@bp.get("/notes")
@login_required
def notes_list():
    return jsonify({"notes": [notes.to_dict(n) for n in notes.list_notes(current_user())]})


@bp.post("/notes")
@login_required
def notes_create():
    data = _json()
    return jsonify(notes.to_dict(notes.save(current_user(), data.get("title", ""), data.get("body", "")))), 201


@bp.put("/notes/<int:note_id>")
@login_required
def notes_update(note_id):
    data = _json()
    return jsonify(notes.to_dict(notes.save(current_user(), data.get("title", ""), data.get("body", ""), note_id)))


@bp.delete("/notes/<int:note_id>")
@login_required
def notes_delete(note_id):
    notes.delete(current_user(), note_id)
    return jsonify({"ok": True})


# ------------------------------------------------------------------ трекер целей
@bp.get("/goals")
@login_required
def goals_list():
    owner_id = request.args.get("user_id", type=int) or current_user().id
    owner = db.session.get(User, owner_id)
    if owner is None:
        raise LookupError(owner_id)
    return jsonify({"goals": [goals.to_dict(g) for g in goals.list_goals(current_user(), owner)]})


@bp.post("/goals")
@roles_required("intern")
def goals_create():
    data = _json()
    due = date.fromisoformat(data["due"]) if data.get("due") else None
    goal = goals.add_goal(current_user(), data.get("title", ""), data.get("metric", ""), due,
                          data.get("kind", "goal"))
    return jsonify(goals.to_dict(goal)), 201


@bp.post("/goals/<int:goal_id>/status")
@roles_required("intern")
def goals_status(goal_id):
    return jsonify(goals.to_dict(goals.set_status(current_user(), goal_id, bool(_json().get("done")))))


@bp.post("/goals/<int:goal_id>/agree")
@roles_required("lead")
def goals_agree(goal_id):
    return jsonify(goals.to_dict(goals.agree(current_user(), goal_id)))


@bp.delete("/goals/<int:goal_id>")
@roles_required("intern")
def goals_delete(goal_id):
    goals.delete(current_user(), goal_id)
    return jsonify({"ok": True})


# ------------------------------------------------------------------ наставник/руководитель
@bp.post("/mentor/questions/<int:question_id>/answer")
@roles_required("mentor", "lead")
def mentor_answer(question_id):
    data = _json()
    q = mentor_questions.answer(current_user(), question_id, data.get("answer", ""), bool(data.get("publish")))
    return jsonify({"id": q.id, "status": q.status})


@bp.get("/interns/<int:user_id>")
@roles_required("mentor", "lead")
def intern_detail(user_id):
    intern = db.session.get(User, user_id)
    if intern is None or not can_view_user(current_user(), intern):
        raise PermissionDenied("Нет доступа к данным этого стажёра")
    return jsonify({"name": intern.full_name, "checklist": checklist.get_checklist(intern),
                    "goals": [goals.to_dict(g) for g in goals.list_goals(current_user(), intern)]})


@bp.get("/metrics")
@roles_required("mentor", "lead", "admin")
def metrics_get():
    return jsonify(metrics.compute(current_user()))


@bp.get("/flags")
@login_required
def flags_get():
    return jsonify({"flags": all_flags()})


@bp.post("/flags/<key>")
@roles_required("lead")
def flags_set(key):
    set_flag(key, bool(_json().get("enabled")))
    return jsonify({"flags": all_flags()})


# ------------------------------------------------------------------ ИИ-крон (вызов по расписанию)
@bp.post("/cron/run")
def cron_run():
    if request.headers.get("X-Cron-Token") != current_app.config["CRON_TOKEN"]:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(cron.run_all())


# ------------------------------------------------------------------ MCP
@mcp_bp.post("/mcp")
@login_required
def mcp_endpoint():
    return jsonify(mcp_server.handle(current_user(), request.get_json(silent=True) or {}))
